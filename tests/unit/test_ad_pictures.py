"""Ad pictures: up to four per ad, only the ad's owner, the same nudity check as listings,
stored under ads/ (no 60-day expiry), with links on the public list and My Ad."""
import json

import boto3
import pytest
from moto import mock_aws

from test_content_filter import EXPLICIT
from test_listing_pictures import BUCKET, as_user, keys, delete
from test_security import load, post, jpeg_b64

ZIP = "19060"


@pytest.fixture(autouse=True)
def enforce(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "enforce")


def setup():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ads = ddb.create_table(TableName="eabu-advertisement", BillingMode="PAY_PER_REQUEST",
                           KeySchema=[{"AttributeName": "zipcode", "KeyType": "HASH"}, {"AttributeName": "ID", "KeyType": "RANGE"}],
                           AttributeDefinitions=[{"AttributeName": "zipcode", "AttributeType": "S"},
                                                 {"AttributeName": "ID", "AttributeType": "N"},
                                                 {"AttributeName": "status", "AttributeType": "S"}],
                           GlobalSecondaryIndexes=[{"IndexName": "status-index", "Projection": {"ProjectionType": "ALL"},
                                                    "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}]}])
    ads.put_item(Item={"zipcode": ZIP, "ID": 7, "status": "active", "title": "Bakery", "ownerUserId": "owner",
                       "phone": "1", "paid": True, "paidThrough": 4102444800})
    ads.put_item(Item={"zipcode": ZIP, "ID": 8, "status": "active", "title": "Old ad, no owner"})
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET)
    return ads, s3


def upload(slot="1", user="owner", item_id=7, picture=None):
    return as_user(post({"category": "ad", "zipcode": ZIP, "itemId": item_id, "slot": slot,
                         "picture": picture or jpeg_b64()}), user)


@mock_aws
def test_owner_adds_up_to_four_ad_pictures_outside_listings():
    ads, s3 = setup()
    fn = load("eabu-listing-picture")
    for slot in "1234":
        assert fn.lambda_handler(upload(slot), None)["statusCode"] == 200
    assert keys(s3) == [f"ads/7/{s}.jpg" for s in "1234"]   # not listings/: no 60-day expiry
    row = ads.get_item(Key={"zipcode": ZIP, "ID": 7})["Item"]
    assert all(row.get(f"picture{s}") for s in "1234")
    assert fn.lambda_handler(upload("5"), None)["statusCode"] == 400

    ev = delete(7, "2", "ad", "owner")
    ev["pathParameters"]["zipcode"] = ZIP
    assert fn.lambda_handler(ev, None)["statusCode"] == 200
    assert "ads/7/2.jpg" not in keys(s3) and "picture2" not in ads.get_item(Key={"zipcode": ZIP, "ID": 7})["Item"]


@mock_aws
def test_only_the_ads_owner_and_never_an_ownerless_ad():
    setup()
    fn = load("eabu-listing-picture")
    assert fn.lambda_handler(upload(user="someone-else"), None)["statusCode"] == 403
    assert fn.lambda_handler(upload(item_id=8, user="anyone"), None)["statusCode"] == 403
    assert fn.lambda_handler(upload(item_id=99), None)["statusCode"] == 404


@mock_aws
def test_ad_picture_with_nudity_is_rejected_and_not_stored(rekognition):
    _, s3 = setup()
    rekognition.labels = EXPLICIT
    res = load("eabu-listing-picture").lambda_handler(upload(), None)
    assert res["statusCode"] == 400 and json.loads(res["body"])["code"] == "IMAGE_REJECTED"
    assert keys(s3) == []


@mock_aws
def test_public_ads_and_my_ad_carry_picture_links():
    setup()
    assert load("eabu-listing-picture").lambda_handler(upload("1"), None)["statusCode"] == 200
    res = load("eabu-advertisement-get").lambda_handler(
        {"httpMethod": "GET", "resource": "/guest/ads", "queryStringParameters": {"zip": ZIP},
         "requestContext": {"identity": {"sourceIp": "203.0.113.9"}}}, None)
    items = {int(a["ID"]): a for a in json.loads(res["body"])["Items"]}
    assert [p["slot"] for p in items[7]["pictures"]] == ["1"] and "ads/7/1.jpg" in items[7]["pictures"][0]["url"]
    assert "pictures" not in items[8] and "ownerUserId" not in items[7]

    mine = load("eabu-ad-purchase").lambda_handler(as_user(
        {"httpMethod": "GET", "pathParameters": {"userId": "owner"}}, "owner"), None)
    ad = json.loads(mine["body"])["ad"]
    assert int(ad["ID"]) == 7 and "ads/7/1.jpg" in ad["pictures"][0]["url"]
