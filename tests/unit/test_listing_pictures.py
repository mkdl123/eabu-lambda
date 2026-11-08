"""Seller listing pictures: two slots per listing, owner-only changes, links on reads."""
import base64, io, json, os

import boto3
import pytest
from moto import mock_aws
from PIL import Image

from test_security import load, post, jpeg_b64
from test_lifestyle import make_tables, get, body, ZIP, LOCATION

BUCKET = "eabu-identity-pictures"


def as_user(event, user):
    return {**event, "requestContext": {"authorizer": {"claims": {"sub": user}}}}


def setup(category="eat"):
    tables = make_tables()
    s3 = boto3.client("s3", region_name="us-east-1"); s3.create_bucket(Bucket=BUCKET)
    res = load(f"provider-post-{category}").lambda_handler(as_user(post(
        {"zipcode": ZIP, "title": "Cakes", "description": "Fresh", "phone": "6105550100", "userId": "seller",
         **LOCATION}), "seller"), None)
    return tables, s3, body(res)["ID"]


def keys(s3):
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", []))


def upload(item_id, slot="1", category="eat", picture=None, user="seller", zipcode=ZIP):
    return as_user(post({"category": category, "zipcode": zipcode, "itemId": item_id, "slot": slot,
                         "picture": picture or jpeg_b64()}), user)


def delete(item_id, slot="1", category="eat", user="seller"):
    return as_user({"httpMethod": "DELETE", "pathParameters": {
        "category": category, "zipcode": ZIP, "itemId": str(item_id), "slot": slot}}, user)


@pytest.fixture(autouse=True)
def enforce(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "enforce")


@mock_aws
@pytest.mark.parametrize("category", ["eat", "live", "move", "lifestyle"])
def test_upload_replace_delete(category):
    tables, s3, item_id = setup(category)
    fn = load("eabu-listing-picture")
    row = lambda: tables[f"{category}-provider"].get_item(Key={"zipcode": ZIP, "ID": item_id})["Item"]

    assert fn.lambda_handler(upload(item_id, "1", category), None)["statusCode"] == 200
    assert fn.lambda_handler(upload(item_id, "2", category), None)["statusCode"] == 200
    prefix = f"listings/{category}/{item_id}/"
    assert keys(s3) == [prefix + "1.jpg", prefix + "2.jpg"]
    assert row().get("picture1") and row().get("picture2")

    first = s3.get_object(Bucket=BUCKET, Key=prefix + "1.jpg")["Body"].read()
    assert fn.lambda_handler(upload(item_id, "1", category, jpeg_b64((40, 40))), None)["statusCode"] == 200
    assert s3.get_object(Bucket=BUCKET, Key=prefix + "1.jpg")["Body"].read() != first, "replaced"
    assert len(keys(s3)) == 2

    res = fn.lambda_handler(delete(item_id, "1", category), None)
    assert res["statusCode"] == 200 and keys(s3) == [prefix + "2.jpg"]
    assert "picture1" not in row() and row().get("picture2")
    assert row()["title"] == "Cakes", "the listing itself stays"
    assert fn.lambda_handler(delete(item_id, "1", category), None)["statusCode"] == 200, "delete twice is harmless"


@mock_aws
def test_only_the_owner_can_change_pictures():
    _, s3, item_id = setup()
    fn = load("eabu-listing-picture")
    fn.lambda_handler(upload(item_id), None)

    assert fn.lambda_handler(upload(item_id, user="mallory"), None)["statusCode"] == 403
    assert fn.lambda_handler(delete(item_id, user="mallory"), None)["statusCode"] == 403
    unauth = post({"category": "eat", "zipcode": ZIP, "itemId": item_id, "slot": "2", "picture": jpeg_b64()})
    assert fn.lambda_handler(unauth, None)["statusCode"] == 401
    assert keys(s3) == [f"listings/eat/{item_id}/1.jpg"], "rejected requests change nothing"


@mock_aws
def test_rejects_bad_input():
    _, s3, item_id = setup()
    fn = load("eabu-listing-picture")
    code = lambda e: fn.lambda_handler(e, None)["statusCode"]

    assert code(upload(item_id, slot="3")) == 400
    assert code(upload(item_id, category="dish")) == 400
    assert code(upload("../x")) == 400
    assert code(upload(100)) == 400, "the counter row is not a listing"
    assert code(upload(999999)) == 404
    assert code(upload(item_id, zipcode="00000")) == 404
    assert code(upload(item_id, picture=base64.b64encode(b"<?php evil ?>").decode())) == 400
    gif = io.BytesIO(); Image.new("RGB", (8, 8)).save(gif, format="GIF")
    assert code(upload(item_id, picture=base64.b64encode(gif.getvalue()).decode())) == 400
    assert code(as_user(post({"category": "eat", "zipcode": ZIP, "itemId": item_id, "slot": "1"}), "seller")) == 400
    assert code(as_user({"httpMethod": "GET"}, "seller")) == 405
    assert keys(s3) == []

    big = io.BytesIO(); Image.new("RGB", (4000, 3000)).save(big, format="JPEG")
    assert code(upload(item_id, picture=base64.b64encode(big.getvalue()).decode())) == 200
    stored = s3.get_object(Bucket=BUCKET, Key=f"listings/eat/{item_id}/1.jpg")["Body"].read()
    assert max(Image.open(io.BytesIO(stored)).size) <= 1600


@mock_aws
def test_reads_include_picture_links():
    _, s3, item_id = setup("lifestyle")
    load("eabu-listing-picture").lambda_handler(upload(item_id, "2", "lifestyle"), None)

    browse = body(load("consumer-get-lifestyle").lambda_handler(as_user(get(zip=ZIP), "buyer"), None))
    pictures = browse["Items"][0]["pictures"]
    assert [p["slot"] for p in pictures] == ["2"]
    assert f"listings/lifestyle/{item_id}/2.jpg" in pictures[0]["url"]

    profile = body(load("provider-my-lifestyle-services").lambda_handler(get("buyer", userId="seller"), None))
    assert profile["Items"][0]["pictures"][0]["slot"] == "2", "buyers see pictures on the seller profile"
    assert "phone" not in profile["Items"][0]

    mine = body(load("provider-my-lifestyle-services").lambda_handler(get("seller", userId="seller"), None))
    assert mine["Items"][0]["pictures"][0]["slot"] == "2"


@mock_aws
def test_listing_without_pictures_has_no_links():
    _, _, item_id = setup()
    browse = body(load("consumer-get-eat").lambda_handler(as_user(get(zip=ZIP), "buyer"), None))
    assert "pictures" not in browse["Items"][0]
