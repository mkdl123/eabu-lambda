"""India (2.0): 6-digit PIN codes work wherever US ZIPs do, without ever matching a US ZIP."""
import json

import pytest
from moto import mock_aws

from my_utils import helpers
from test_favourites import setup, listing, as_user
from test_guest import guest

BANGALORE, KORAMANGALA, DELHI = "560001", "560034", "110001"


@pytest.fixture(autouse=True)
def enforce(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "enforce")


def test_pin_codes_are_postal_codes_of_their_own():
    assert helpers._norm_zip("560001") == "560001" and helpers._norm_zip(" 560 001 ") == "560001"
    assert helpers._norm_zip("056001") is None and helpers._norm_zip("5600011") is None
    assert helpers._norm_zip("19060") == "19060" and helpers._norm_zip("19060-1234") == "19060"
    assert helpers.country_of_zip(BANGALORE) == "IN" and helpers.country_of_zip("19060") == "US"
    assert helpers.is_known_zip(BANGALORE) and helpers.is_known_zip(DELHI)
    # Same city is near; another city or another country is not.
    assert helpers.zip_distance_miles(BANGALORE, KORAMANGALA) < 30
    assert helpers.zip_distance_miles(BANGALORE, DELHI) > 900
    assert helpers.zip_distance_miles(BANGALORE, "19060") > 8000


class FakeGeo:
    def __init__(self, results):
        self.results, self.calls = results, []

    def search_place_index_for_position(self, **kw):
        self.calls.append(kw)
        return {"Results": self.results}


def test_position_in_india_asks_amazon_for_the_pin(monkeypatch):
    geo = FakeGeo([{"Place": {"Country": "IND", "PostalCode": "560001"}}])
    monkeypatch.setattr(helpers, "_geo", geo)
    assert helpers.postal_code_at(12.9756, 77.6050) == "560001"
    assert geo.calls[0]["Position"] == [77.605, 12.9756]
    # Amazon has nothing: the nearest PIN centroid instead.
    monkeypatch.setattr(helpers, "_geo", FakeGeo([]))
    assert helpers.country_of_zip(helpers.postal_code_at(12.9756, 77.6050)) == "IN"
    # The US never asks Amazon.
    us = FakeGeo([])
    monkeypatch.setattr(helpers, "_geo", us)
    assert helpers.postal_code_at(39.92, -75.39) == "19063" and us.calls == []


@mock_aws
def test_guest_feed_in_india_shows_only_nearby_indian_posts():
    _, fn = setup()
    listing("eat", "provider", "dosa", zip_code=KORAMANGALA, title="Dosa batter")
    listing("eat", "provider", "delhi", zip_code=DELHI, title="Chole")
    listing("eat", "provider", "media", zip_code="19060", title="Cakes")
    res = fn.lambda_handler(guest("/guest/feed", {"zip": BANGALORE, "limit": "10"}), None)
    assert res["statusCode"] == 200
    assert [i["title"] for i in json.loads(res["body"])["Items"]] == ["Dosa batter"]
    us = fn.lambda_handler(guest("/guest/feed", {"zip": "19060", "limit": "10"}), None)
    assert [i["title"] for i in json.loads(us["body"])["Items"]] == ["Cakes"]


def test_discover_asks_for_india_by_pin_and_skips_us_only_services(monkeypatch):
    from datetime import date, datetime
    from test_security import load
    fn = load("eabu-discover")
    monday = date(2026, 11, 23)
    ask = fn.build_ask(BANGALORE, fn.topics_for_week(monday), [], monday)
    assert "near PIN code 560001, India" in ask and "ZIP" not in ask
    assert "near ZIP 19060" in fn.build_ask("19060", [], [], monday)
    assert "Zomato" in fn.SYSTEM
    called = []
    monkeypatch.setattr(fn.urllib.request, "urlopen", lambda *a, **k: called.append(a))
    monkeypatch.setattr(fn, "_ticketmaster_key", lambda: "key")
    assert fn.geocode("12 MG Road, Bengaluru", BANGALORE) == {}       # Census geocoder: US only
    assert fn.fetch_events(BANGALORE, datetime(2026, 11, 23)) == []   # Ticketmaster: US only
    assert called == []


def test_content_filter_knows_hindi_and_hinglish():
    from my_utils.content_filter import text_is_blocked
    for bad in ("call girl service in Andheri", "chudai videos", "रंडी", "सेक्सी वीडियो", "Ch0dna"):
        assert text_is_blocked(bad), bad
    for ok in ("Randi's bakery", "Lund University mugs", "Homemade dosa batter", "ताज़ा समोसे",
               "Holland bulbs", "हॉलैंड के फूल"):
        assert not text_is_blocked(ok), ok
