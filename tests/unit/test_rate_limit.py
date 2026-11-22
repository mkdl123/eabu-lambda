"""Per-user hourly read limit on the routes that show other people's posts (anti-scraping)."""
import json
from datetime import datetime, timezone

import boto3
from moto import mock_aws

from test_security import load

REGION = "us-east-1"
NOW = datetime(2026, 11, 22, 15, 30, tzinfo=timezone.utc)


def make_rate_table():
    boto3.resource("dynamodb", region_name=REGION).create_table(
        TableName="eabu-rate-limit", BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "key", "AttributeType": "S"}])


def signed_in(user):
    return {"httpMethod": "GET", "requestContext": {"authorizer": {"claims": {"cognito:username": user, "sub": user}}}}


@mock_aws
def test_counts_per_user_per_hour(monkeypatch):
    make_rate_table()
    from my_utils import helpers
    monkeypatch.setenv("READ_LIMIT_PER_HOUR", "3")
    assert [helpers.over_read_limit("Ann", NOW) for _ in range(4)] == [False, False, False, True]
    assert helpers.over_read_limit("bob", NOW) is False                      # each user has their own count
    assert helpers.over_read_limit("ann", NOW.replace(hour=16)) is False      # a new hour starts again
    row = boto3.resource("dynamodb", region_name=REGION).Table("eabu-rate-limit").get_item(
        Key={"key": "ann#2026112215"})["Item"]
    assert row["n"] == 4 and row["expiresAt"] > NOW.timestamp()               # ID is case-insensitive


@mock_aws
def test_fails_open_without_the_table():
    from my_utils import helpers
    assert helpers.over_read_limit("ann", NOW) is False
    assert helpers.over_read_limit(None, NOW) is False


@mock_aws
def test_decorator_answers_429_past_the_limit(monkeypatch):
    make_rate_table()
    from my_utils import helpers
    monkeypatch.setenv("READ_LIMIT_PER_HOUR", "2")
    calls = []

    @helpers.cors
    @helpers.rate_limited
    def handler(event, context):
        calls.append(1)
        return helpers._response(200, {"ok": True})

    codes = [handler(signed_in("ann"), None)["statusCode"] for _ in range(3)]
    assert codes == [200, 200, 429] and len(calls) == 2
    res = handler(signed_in("ann"), None)
    assert json.loads(res["body"])["code"] == "RATE_LIMITED"
    assert res["headers"]["Access-Control-Allow-Origin"] == "*"


def test_read_routes_are_limited():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "eabu-dish"
    for name in ["consumer-get-eat", "consumer-get-live", "consumer-get-move", "consumer-get-lifestyle",
                 "provider-get-eat", "provider-get-live", "provider-get-move", "provider-get-lifestyle",
                 "eabu-favourites", "eabu-discovered", "eabu-user", "eabu-get-user-ratings",
                 "eabu-advertisement-get"]:
        assert "@helpers.rate_limited\ndef lambda_handler(" in (root / name / "app.py").read_text(), name
