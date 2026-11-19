"""Shareable post pages: signed links for posts someone shares; safe fields only."""
import json
import time

import boto3
import pytest
from moto import mock_aws

from test_lifestyle import make_tables, ZIP
from test_security import load, post

REGION = "us-east-1"


@pytest.fixture(autouse=True)
def enforce(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "enforce")
    monkeypatch.setenv("AUTH_USER_ID_CLAIM", "sub")


def setup():
    tables = make_tables()
    boto3.client("ssm", region_name=REGION).put_parameter(
        Name="/eabu/share-link-secret", Value="test-secret", Type="SecureString")
    boto3.client("s3", region_name=REGION).create_bucket(Bucket="eabu-identity-pictures")
    seller = boto3.resource("dynamodb", region_name=REGION).Table("eabu-eat-provider")
    seller.put_item(Item={"zipcode": ZIP, "ID": 7, "userId": "sam", "title": "Biryani", "description": "x " * 400,
                          "phone": "6105550100", "email": "sam@example.com", "address": "1 Main St",
                          "latitude": "39.9", "longitude": "-75.1", "picture1": "t", "createdAt": "2026-11-18T10:00:00",
                          "expiresAt": int(time.time()) + 86400})
    seller.put_item(Item={"zipcode": ZIP, "ID": 8, "userId": "sam", "title": "Old", "expiresAt": int(time.time()) - 10})
    fn = load("eabu-public-post")
    fn._secret = None
    return fn


def link(fn, user="viewer", **payload):
    event = post(payload)
    if user:
        event["requestContext"] = {"authorizer": {"claims": {"sub": user}}}
    return fn.lambda_handler(event, None)


def page(fn, token):
    return fn.lambda_handler({"httpMethod": "GET", "pathParameters": {"token": token}}, None)


@mock_aws
def test_shared_post_page_shows_only_safe_fields():
    fn = setup()
    res = link(fn, kind="seller", category="eat", zipcode=ZIP, ID=7)
    assert res["statusCode"] == 200
    url = json.loads(res["body"])["url"]
    assert url.startswith("https://www.eabu.net/p/s-eat-")
    token = url.rsplit("/", 1)[1]

    shown = json.loads(page(fn, token)["body"])
    assert shown["title"] == "Biryani" and shown["categoryLabel"] == "Food" and shown["zipcode"] == ZIP
    assert len(shown["description"]) <= 300 and "listings/eat/7/1.jpg" in shown["picture"]
    text = json.dumps(shown)
    for secret in ("6105550100", "sam@example.com", "1 Main St", "39.9", '"sam"', "userId"):
        assert secret not in text


@mock_aws
def test_links_cannot_be_guessed_or_made_without_signing_in():
    fn = setup()
    token = json.loads(link(fn, kind="seller", category="eat", zipcode=ZIP, ID=7)["body"])["url"].rsplit("/", 1)[1]
    forged = token.replace("-7-", "-8-")
    assert page(fn, forged)["statusCode"] == 404, "signature covers the ID"
    assert page(fn, token[:-1] + ("0" if token[-1] != "0" else "1"))["statusCode"] == 404
    assert page(fn, "../etc")["statusCode"] == 404
    assert link(fn, user=None, kind="seller", category="eat", zipcode=ZIP, ID=7)["statusCode"] == 401
    assert link(fn, kind="seller", category="eat", zipcode=ZIP, ID=8)["statusCode"] == 404, "expired"
    assert link(fn, kind="seller", category="eat", zipcode=ZIP, ID=999)["statusCode"] == 404
    assert link(fn, kind="admin", category="eat", zipcode=ZIP, ID=7)["statusCode"] == 400
    assert link(fn, kind="seller", category="eat", zipcode=ZIP, ID=100)["statusCode"] == 404, "counter row"


@mock_aws
def test_page_disappears_with_the_post():
    fn = setup()
    token = json.loads(link(fn, kind="seller", category="eat", zipcode=ZIP, ID=7)["body"])["url"].rsplit("/", 1)[1]
    boto3.resource("dynamodb", region_name=REGION).Table("eabu-eat-provider").delete_item(Key={"zipcode": ZIP, "ID": 7})
    assert page(fn, token)["statusCode"] == 404


@mock_aws
def test_buyer_request_page_has_no_picture():
    fn = setup()
    boto3.resource("dynamodb", region_name=REGION).Table("eabu-move-consumer").put_item(
        Item={"zipcode": ZIP, "ID": 3, "userId": "bea", "title": "Ride to the airport", "description": "Friday"})
    token = json.loads(link(fn, kind="buyer", category="move", zipcode=ZIP, ID=3)["body"])["url"].rsplit("/", 1)[1]
    shown = json.loads(page(fn, token)["body"])
    assert token.startswith("b-move-") and shown["kind"] == "buyer" and shown["picture"] is None
