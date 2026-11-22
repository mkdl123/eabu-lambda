"""Guest browsing (1.9): public /guest/... routes show the Home feed, Discovered and ads before
sign-up, without seller contact details, rate limited per IP address."""
import json
from decimal import Decimal
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from test_favourites import setup, listing, as_user
from test_discover import make_discovered
from test_rate_limit import make_rate_table
from test_security import load
from test_lifestyle import ZIP


@pytest.fixture(autouse=True)
def enforce(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "enforce")


def guest(resource, query, ip="203.0.113.7"):
    return {"httpMethod": "GET", "resource": resource, "queryStringParameters": query,
            "requestContext": {"identity": {"sourceIp": ip}}}


@mock_aws
def test_guest_feed_hides_contact_details_and_rounds_pins():
    tables, fn = setup()
    listing("eat", "provider", "seller1", title="Cakes", description="Call 610-555-1234 or jo@cakes.com, 2 dozen $30",
            email="jo@cakes.com", address="12 Main St, Ridley Park, PA",
            addressLat="39.880123", addressLng="-75.322987")
    listing("live", "provider", "seller2")
    res = fn.lambda_handler(guest("/guest/feed", {"zip": ZIP, "limit": "10"}), None)
    assert res["statusCode"] == 200
    items = json.loads(res["body"])["Items"]
    assert len(items) == 2 and all(i["contactHidden"] for i in items)
    cake = next(i for i in items if i["title"] == "Cakes")
    assert not {"phone", "email", "address"} & set(cake)
    assert cake["description"] == "Call [sign up to see] or [sign up to see], 2 dozen $30"
    assert (cake["addressLat"], cake["addressLng"]) == (39.88, -75.32)
    assert cake["userId"] == "seller1" and cake["category"] == "eat"


@mock_aws
def test_guest_feed_needs_a_zip_and_is_read_only():
    _, fn = setup()
    assert fn.lambda_handler(guest("/guest/feed", {}), None)["statusCode"] == 400
    assert fn.lambda_handler(guest("/guest/feed", {"zip": ZIP, "kind": "blocked"}), None)["statusCode"] == 400
    post = {**guest("/guest/feed", {"zip": ZIP}), "httpMethod": "POST", "body": "{}"}
    assert fn.lambda_handler(post, None)["statusCode"] == 404


@mock_aws
def test_member_routes_still_need_sign_in():
    _, fn = setup()
    event = {"httpMethod": "GET", "resource": "/favourites-feed/{userId}/{kind}",
             "pathParameters": {"userId": "ann", "kind": "seller"}, "queryStringParameters": {"zip": ZIP, "nearby": "1"}}
    assert fn.lambda_handler(event, None)["statusCode"] == 401
    signed_in = json.loads(fn.lambda_handler(as_user(event, "ann"), None)["body"])
    assert "contactHidden" not in json.dumps(signed_in)


@mock_aws
def test_guest_discovered_and_ads(monkeypatch):
    table = make_discovered()
    table.put_item(Item={"areaZip": "19060", "ID": 0, "zips": ["19060"]})
    table.put_item(Item={"areaZip": "19060", "ID": 2026112301, "headline": "Diner", "expiresAt": 10**10,
                         "weekOf": "2026-11-23", "category": "eat", "kind": "place"})
    fn = load("eabu-discovered")
    fn._cache = None
    started = []
    fn.lambda_client = SimpleNamespace(invoke=lambda **kw: started.append(json.loads(kw["Payload"])))
    res = fn.lambda_handler(guest("/guest/discovered", {"zip": "19033", "v": "2"}), None)
    assert res["statusCode"] == 200 and [i["headline"] for i in json.loads(res["body"])["Items"]] == ["Diner"]
    monkeypatch.setattr(fn, "GUEST_CAN_START_AREAS", False)
    assert json.loads(fn.lambda_handler(guest("/guest/discovered", {"zip": "59801"}), None)["body"])["Items"] == []
    assert started == []                                   # guests can be kept to existing areas
    monkeypatch.setattr(fn, "GUEST_CAN_START_AREAS", True)
    fn.lambda_handler(guest("/guest/discovered", {"zip": "59801"}), None)
    assert [p["areaZip"] for p in started] == ["59801"]   # default: a newcomer's ZIP gets searched

    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ads = ddb.create_table(TableName="eabu-advertisement", BillingMode="PAY_PER_REQUEST",
                           KeySchema=[{"AttributeName": "zipcode", "KeyType": "HASH"}, {"AttributeName": "ID", "KeyType": "RANGE"}],
                           AttributeDefinitions=[{"AttributeName": "zipcode", "AttributeType": "S"},
                                                 {"AttributeName": "ID", "AttributeType": "N"},
                                                 {"AttributeName": "status", "AttributeType": "S"}],
                           GlobalSecondaryIndexes=[{"IndexName": "status-index", "Projection": {"ProjectionType": "ALL"},
                                                    "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}]}])
    ads.put_item(Item={"zipcode": "19060", "ID": 1, "status": "active", "title": "Here", "phone": "1"})
    ads.put_item(Item={"zipcode": "19060", "ID": 2, "status": "pending", "title": "Not yet"})
    res = load("eabu-advertisement-get").lambda_handler(guest("/guest/ads", {"zip": "19060"}), None)
    assert [a["title"] for a in json.loads(res["body"])["Items"]] == ["Here"]
    assert "phone" not in res["body"]


@mock_aws
def test_guests_are_rate_limited_per_ip(monkeypatch):
    make_rate_table()
    _, fn = setup()
    monkeypatch.setenv("GUEST_LIMIT_PER_HOUR", "2")
    codes = [fn.lambda_handler(guest("/guest/feed", {"zip": ZIP}), None)["statusCode"] for _ in range(3)]
    assert codes == [200, 200, 429]
    assert fn.lambda_handler(guest("/guest/feed", {"zip": ZIP}, ip="198.51.100.9"), None)["statusCode"] == 200
