"""Address map pins on posts and the Home feed's ?map=1 view (app 1.5)."""
import pytest
from moto import mock_aws

from test_favourites import setup, listing, add, feed_call, enforce  # noqa: F401  (enforce: autouse fixture)
from test_lifestyle import body, ZIP

PIN = {"address": "1 Main St, Downingtown PA", "addressLat": 40.006512, "addressLng": -75.703271}


def ids_of(res):
    return sorted((i["userId"], i["category"]) for i in body(res)["Items"])


@mock_aws
def test_posts_save_the_address_pin_in_every_category():
    tables, _ = setup()
    for role in ("provider", "consumer"):
        for category in ("eat", "live", "move", "lifestyle"):
            item_id = listing(category, role, f"{role}-{category}", **PIN)
            item = tables[f"{category}-{role}"].get_item(Key={"zipcode": ZIP, "ID": item_id})["Item"]
            assert (item["addressLat"], item["addressLng"]) == ("40.006512", "-75.703271")


@mock_aws
@pytest.mark.parametrize("extra", [
    {"addressLat": 40.0, "addressLng": -75.7},                               # no address: no pin
    {"address": "1 Main St", "addressLat": 95, "addressLng": -75.7},         # out of range
    {"address": "1 Main St", "addressLat": "abc", "addressLng": -75.7},
    {"address": "1 Main St", "addressLat": 0, "addressLng": 0},              # failed lookup
    {"address": "1 Main St", "addressLat": 40.0},                            # half a pin
])
def test_bad_or_missing_pins_are_dropped_but_the_post_saves(extra):
    tables, _ = setup()
    item_id = listing("eat", "provider", "s1", **extra)
    item = tables["eat-provider"].get_item(Key={"zipcode": ZIP, "ID": item_id})["Item"]
    assert "addressLat" not in item and "addressLng" not in item


@mock_aws
def test_map_feed_returns_only_pinned_posts_unpaged():
    _, fn = setup()
    listing("eat", "provider", "s1", zip_code="19103", **PIN)                   # favourite, pinned
    listing("live", "provider", "s2", **PIN)                                    # pinned
    listing("move", "provider", "s3", address="No pin St")                      # address, no pin
    listing("lifestyle", "provider", "s4")                                      # no address
    listing("eat", "provider", "s5", zip_code="08820", **PIN)                   # too far
    listing("eat", "provider", "buyer", **PIN)                                  # own post
    add(fn, "buyer", "seller", "s1")

    res = feed_call(fn, "buyer", "seller", {"zip": ZIP, "nearby": "1", "map": "1", "limit": "1"})
    assert res["statusCode"] == 200
    assert ids_of(res) == [("s1", "eat"), ("s2", "live")], "limit is ignored for the map"
    items = body(res)["Items"]
    assert {i["userId"]: i["isFavourite"] for i in items} == {"s1": True, "s2": False}
    assert all((i["addressLat"], i["addressLng"]) == ("40.006512", "-75.703271") for i in items)
    assert "nextCursor" not in body(res)

    fav = feed_call(fn, "buyer", "seller", {"zip": ZIP, "map": "1"})
    assert ids_of(fav) == [("s1", "eat")], "favourites-only map"

    listed = feed_call(fn, "buyer", "seller", {"zip": ZIP, "nearby": "1"})
    assert len(body(listed)["Items"]) == 4, "the list view is unchanged"


@mock_aws
def test_map_feed_leaves_out_blocked_users():
    _, fn = setup()
    listing("eat", "provider", "s1", **PIN)
    listing("live", "provider", "s2", **PIN)
    add(fn, "buyer", "blocked", "s2")
    assert ids_of(feed_call(fn, "buyer", "seller", {"zip": ZIP, "nearby": "1", "map": "1"})) == [("s1", "eat")]


@mock_aws
def test_buyer_request_pins_show_on_the_sellers_map():
    _, fn = setup()
    listing("move", "consumer", "b1", **PIN)
    listing("eat", "consumer", "b2")
    assert ids_of(feed_call(fn, "seller", "buyer", {"zip": ZIP, "nearby": "1", "map": "1"})) == [("b1", "move")]
