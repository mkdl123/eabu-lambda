"""App opens per ZIP/PIN (helpers.count_open, scripts/opens.py)."""
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from test_favourites import setup, as_user
from test_guest import guest
from test_rate_limit import make_rate_table
from test_lifestyle import ZIP

NOW = datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def enforce(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "enforce")


def rows():
    items = boto3.resource("dynamodb", region_name="us-east-1").Table("eabu-rate-limit").scan()["Items"]
    return {i["key"]: i for i in items}


def opens_row(day, zip_code):
    return rows().get(f"opens#{day}#{zip_code}")


@mock_aws
def test_counts_opens_and_people_per_day_and_zip():
    make_rate_table()
    from my_utils import helpers
    user = as_user({}, "Ann")
    helpers.count_open(user, "19060", NOW)
    helpers.count_open(user, "19060", NOW)                        # a refresh: one more open, same person
    helpers.count_open(as_user({}, "bob"), "19060", NOW)
    helpers.count_open(guest("/guest/feed", {}), "19060", NOW)
    helpers.count_open(guest("/guest/feed", {}, ip="198.51.100.1"), "560 001", NOW)
    row = opens_row("2026-11-26", "19060")
    assert (row["userOpens"], row["userPeople"], row["guestOpens"], row["guestPeople"]) == (3, 2, 1, 1)
    assert row["country"] == "US" and row["zip"] == "19060" and row["expiresAt"] > NOW.timestamp() + 300 * 86400
    assert opens_row("2026-11-26", "560001")["country"] == "IN"
    helpers.count_open(user, "19060", NOW.replace(day=27))        # a new day counts the person again
    assert opens_row("2026-11-27", "19060")["userPeople"] == 1
    # No User Id or IP address stored anywhere
    text = repr(rows())
    assert "ann" not in text.lower() and "198.51.100.1" not in text and "203.0.113.7" not in text


@mock_aws
def test_bad_zip_and_missing_table_never_fail():
    from my_utils import helpers
    helpers.count_open(as_user({}, "ann"), "19060", NOW)             # no table: just a warning
    make_rate_table()
    helpers.count_open(as_user({}, "ann"), "abc", NOW)
    assert not any(k.startswith("opens#") for k in rows())


@mock_aws
def test_feeds_count_only_the_first_page():
    make_rate_table()
    _, fn = setup()
    me = {"httpMethod": "GET", "resource": "/favourites-feed/{userId}/{kind}",
          "pathParameters": {"userId": "ann", "kind": "seller"}}
    def nearby(**q):
        return fn.lambda_handler(as_user({**me, "queryStringParameters": {"zip": ZIP, "nearby": "1", **q}}, "ann"), None)
    assert nearby(limit="10")["statusCode"] == 200
    assert nearby(map="1")["statusCode"] == 200                   # map view: not an open
    assert fn.lambda_handler(guest("/guest/feed", {"zip": ZIP, "limit": "10"}), None)["statusCode"] == 200
    assert fn.lambda_handler(guest("/guest/feed", {"zip": ZIP, "map": "1"}), None)["statusCode"] == 200
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = opens_row(day, ZIP)
    assert (row["userOpens"], row["guestOpens"]) == (1, 1)
    # someone else's feed is refused and not counted
    other = as_user({**me, "queryStringParameters": {"zip": ZIP, "nearby": "1"}}, "mallory")
    assert fn.lambda_handler(other, None)["statusCode"] == 403
    assert opens_row(day, ZIP)["userOpens"] == 1
