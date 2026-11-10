"""Favourite sellers/buyers, their feed, and ads within 25 miles of a ZIP."""
import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from test_security import load, post, jpeg_b64
from test_lifestyle import make_tables, body, ZIP, LOCATION


def as_user(event, user):
    return {**event, "requestContext": {"authorizer": {"claims": {"sub": user}}}}


@pytest.fixture(autouse=True)
def enforce(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "enforce")


def setup():
    tables = make_tables()
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(TableName="eabu-favourites", BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "userId", "KeyType": "HASH"},
                                {"AttributeName": "favKey", "KeyType": "RANGE"}],
                     AttributeDefinitions=[{"AttributeName": "userId", "AttributeType": "S"},
                                           {"AttributeName": "favKey", "AttributeType": "S"}])
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="eabu-identity-pictures")
    return tables, load("eabu-favourites")


def add(fn, user, kind, fav, caller=None):
    return fn.lambda_handler(as_user(post({"userId": user, "kind": kind, "favUserId": fav}), caller or user), None)


def get(fn, user, kind, caller=None, feed=False, zip_code=None):
    event = {"httpMethod": "GET", "pathParameters": {"userId": user, "kind": kind},
             "resource": "/favourites-feed/{userId}/{kind}" if feed else "/favourites/{userId}/{kind}"}
    if zip_code:
        event["queryStringParameters"] = {"zip": zip_code}
    return fn.lambda_handler(as_user(event, caller or user), None)


def remove(fn, user, kind, fav, caller=None):
    return fn.lambda_handler(as_user({"httpMethod": "DELETE", "pathParameters": {
        "userId": user, "kind": kind, "favUserId": fav}}, caller or user), None)


def listing(category, role, user, zip_code=ZIP, **extra):
    fields = {"zipcode": zip_code, "title": f"{category} by {user}", "description": "D", "userId": user, **LOCATION, **extra}
    if role == "provider":
        fields["phone"] = "6105550100"
    res = load(f"{role}-post-{category}").lambda_handler(as_user(post(fields), user), None)
    assert res["statusCode"] == 200
    return body(res)["ID"]


@mock_aws
def test_add_list_remove():
    _, fn = setup()
    assert add(fn, "buyer", "seller", "s1")["statusCode"] == 200
    assert add(fn, "buyer", "seller", "s1")["statusCode"] == 200, "saving twice is harmless"
    assert add(fn, "buyer", "seller", "s2")["statusCode"] == 200
    assert add(fn, "buyer", "buyer", "b1")["statusCode"] == 200
    favs = lambda kind: sorted(i["favUserId"] for i in body(get(fn, "buyer", kind))["Items"])
    assert favs("seller") == ["s1", "s2"] and favs("buyer") == ["b1"], "kinds are kept apart"

    assert remove(fn, "buyer", "seller", "s1")["statusCode"] == 200
    assert favs("seller") == ["s2"]
    assert remove(fn, "buyer", "seller", "s1")["statusCode"] == 200, "removing twice is harmless"


@mock_aws
def test_favourites_are_private_and_validated():
    _, fn = setup()
    add(fn, "buyer", "seller", "s1")
    assert get(fn, "buyer", "seller", caller="mallory")["statusCode"] == 403
    assert add(fn, "buyer", "seller", "s9", caller="mallory")["statusCode"] == 403
    assert remove(fn, "buyer", "seller", "s1", caller="mallory")["statusCode"] == 403
    assert get(fn, "buyer", "seller", feed=True, caller="mallory")["statusCode"] == 403
    unauth = {"httpMethod": "GET", "pathParameters": {"userId": "buyer", "kind": "seller"}, "resource": "/favourites/{userId}/{kind}"}
    assert fn.lambda_handler(unauth, None)["statusCode"] == 401
    assert add(fn, "buyer", "seller", "buyer")["statusCode"] == 400, "not yourself"
    assert add(fn, "buyer", "admin", "s1")["statusCode"] == 400
    assert add(fn, "buyer", "seller", "../x")["statusCode"] == 400
    assert [i["favUserId"] for i in body(get(fn, "buyer", "seller"))["Items"]] == ["s1"]


@mock_aws
def test_limit_of_50():
    _, fn = setup()
    for i in range(50):
        assert add(fn, "buyer", "seller", f"s{i}")["statusCode"] == 200
    assert add(fn, "buyer", "seller", "one-more")["statusCode"] == 400


@mock_aws
def test_feed_shows_favourites_current_posts_within_25_miles():
    tables, fn = setup()
    listing("eat", "provider", "s1", zip_code="19103")      # Philadelphia, ~18 miles from 19060
    listing("live", "provider", "s1", zip_code="19030")     # ~42 miles: too far
    near = listing("lifestyle", "provider", "s1")
    listing("move", "provider", "s2")                       # not a favourite
    listing("eat", "consumer", "s1")                        # s1's buyer request: not in the seller feed
    tables["live-provider"].put_item(Item={"zipcode": ZIP, "ID": 777, "userId": "s1", "title": "old",
                                           "createdAt": (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()})
    load("eabu-listing-picture").lambda_handler(as_user(post(
        {"category": "lifestyle", "zipcode": ZIP, "itemId": near, "slot": "1", "picture": jpeg_b64()}), "s1"), None)
    add(fn, "buyer", "seller", "s1")

    res = body(get(fn, "buyer", "seller", feed=True, zip_code=ZIP))
    items = res["Items"]
    assert [(i["category"], i["zipcode"], i["distanceMiles"]) for i in items] == [
        ("lifestyle", ZIP, 0.0), ("eat", "19103", 18.3)], "newest first, far post left out"
    assert res["radiusMiles"] == 25
    assert len(body(get(fn, "buyer", "seller", feed=True))["Items"]) == 3, "no ZIP: no distance filter"
    assert items[0]["pictures"][0]["slot"] == "1"
    assert "latitude" not in items[0] and "interested_by" not in items[0]


@mock_aws
def test_seller_feed_of_favourite_buyers():
    _, fn = setup()
    listing("move", "consumer", "b1", address="1 Main St")
    listing("move", "provider", "b1")                       # b1 selling something: not a buyer request
    add(fn, "seller", "buyer", "b1")
    items = body(get(fn, "seller", "buyer", feed=True))["Items"]
    assert [(i["category"], i["userId"], i.get("address")) for i in items] == [("move", "b1", "1 Main St")]
    assert body(get(fn, "seller", "seller", feed=True))["Items"] == [], "no favourite sellers"


@mock_aws
def test_ads_can_be_limited_to_25_miles_of_a_zip():
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
    ads.put_item(Item={"zipcode": "08820", "ID": 3, "status": "active", "title": "Elsewhere"})   # ~78 miles
    ads.put_item(Item={"zipcode": "19103", "ID": 4, "status": "active", "title": "Philly"})      # ~18 miles
    ads.put_item(Item={"zipcode": "781301", "ID": 5, "status": "active", "title": "India"})      # not "78130" (Texas)
    fn = load("eabu-advertisement-get")
    get_ads = lambda q=None: json.loads(fn.lambda_handler(
        {"httpMethod": "GET", "pathParameters": {"status": "active"}, "queryStringParameters": q}, None)["body"])
    assert sorted(a["title"] for a in get_ads()["Items"]) == ["Elsewhere", "Here", "India", "Philly"]
    near = {a["title"]: a for a in get_ads({"zip": "19060"})["Items"]}
    assert sorted(near) == ["Here", "Philly"] and "phone" not in near["Here"]
    assert near["Here"]["distanceMiles"] == 0.0 and near["Philly"]["distanceMiles"] == 18.3
    assert get_ads({"zip": "99999"})["Items"] == [], "unknown ZIP: only exact matches"
    assert [a["title"] for a in get_ads({"zip": "08820"})["Items"]] == ["Elsewhere"]
    assert get_ads({"zip": "78130"})["Items"] == []
    assert [a["title"] for a in get_ads({"zip": "781301"})["Items"]] == ["India"], "same non-US code still matches"
