"""Lifestyle category: full flow, plus guards that shared functions use the Lifestyle tables."""
import json, os, re

import boto3
import pytest
from moto import mock_aws

from test_security import ROOT, load, post

ZIP = "19060"
LOCATION = {"latitude": 39.8571234, "longitude": -75.4141234}


def table(ddb, name, keys, attrs, indexes=()):
    kwargs = {"GlobalSecondaryIndexes": [
        {"IndexName": n, "KeySchema": k, "Projection": {"ProjectionType": "ALL"}} for n, k in indexes
    ]} if indexes else {}
    return ddb.create_table(
        TableName=name, BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": a, "KeyType": t} for a, t in keys],
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": t} for a, t in attrs], **kwargs)


def make_tables():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    keys, attrs = [("zipcode", "HASH"), ("ID", "RANGE")], [("zipcode", "S"), ("ID", "N"), ("userId", "S")]
    user = ("userId-index", [{"AttributeName": "userId", "KeyType": "HASH"}])
    user_item = ("userId-itemId-index", [{"AttributeName": "userId", "KeyType": "HASH"},
                                         {"AttributeName": "itemId", "KeyType": "RANGE"}])
    tables = {}
    for category in ("eat", "live", "move", "lifestyle"):
        for role in ("consumer", "provider"):
            tables[f"{category}-{role}"] = table(ddb, f"eabu-{category}-{role}", keys, attrs, [user])
        for kind in ("interested", "rated"):
            tables[f"{category}-{kind}"] = table(
                ddb, f"eabu-{category}-{kind}", [("ID", "HASH"), ("cUserId", "RANGE")], [("ID", "N"), ("cUserId", "S")])
    tables["lifestyle-offer"] = table(ddb, "eabu-lifestyle-offer", keys, attrs + [("itemId", "N")], [user, user_item])
    return tables


def get(user=None, **path):
    event = {"httpMethod": "GET", "pathParameters": path}
    if user:
        event["requestContext"] = {"authorizer": {"claims": {"sub": user}}}
    return event


def body(response):
    return json.loads(response["body"])


def post_request(user="buyer"):
    response = load("consumer-post-lifestyle").lambda_handler(
        post({"zipcode": ZIP, "title": "Yoga", "description": "Twice a week", "userId": user, **LOCATION}), None)
    assert response["statusCode"] == 200
    return body(response)["ID"]


def post_service(user="seller"):
    response = load("provider-post-lifestyle").lambda_handler(
        post({"zipcode": ZIP, "title": "Trainer", "description": "Home visits", "phone": "6105550100",
              "userId": user, **LOCATION}), None)
    assert response["statusCode"] == 200
    return body(response)["ID"]


def offer_payload(item_id):
    return {"itemId": item_id, "userId": "buyer", "pUserId": "seller", "title": "Yoga", "description": "Twice a week",
            "offerDescription": "I teach yoga", "phoneNumber": "6105550100", "email": "s@example.com", "price": "20",
            "zip": ZIP, **LOCATION}


@mock_aws
def test_buyer_request_receives_seller_offer():
    make_tables()
    item_id = post_request()

    listing = body(load("provider-get-lifestyle").lambda_handler(get(zip=ZIP, userId="NA"), None))
    assert [i["ID"] for i in listing["Items"]] == [str(item_id)]
    assert listing["Items"][0]["latitude"] == "39.86", "browsing must not reveal the exact location"
    assert "ResponseMetadata" not in listing

    own = body(load("provider-get-lifestyle").lambda_handler(get(zip="11111", userId="buyer"), None))
    assert own["Items"][0]["latitude"] == str(LOCATION["latitude"])

    offer = load("provider-offer-lifestyle")
    assert offer.lambda_handler(post(offer_payload(item_id)), None)["statusCode"] == 200
    assert offer.lambda_handler(post(offer_payload(9999)), None)["statusCode"] == 404

    offers = load("consumer-lifestyle-offers")
    assert body(offers.lambda_handler(get(userId="buyer", itemId=str(item_id)), None))["Count"] == 1
    assert body(offers.lambda_handler(get(userId="buyer", itemId="NA"), None))["Count"] == 1


@mock_aws
def test_seller_listing_is_browsable():
    make_tables()
    item_id = post_service()

    listing = body(load("consumer-get-lifestyle").lambda_handler(get(zip=ZIP), None))
    assert [i["ID"] for i in listing["Items"]] == [str(item_id)]
    assert (listing["Items"][0]["totalstars"], listing["Items"][0]["ratingscount"]) == ("0", "0")

    mine = body(load("provider-my-lifestyle-services").lambda_handler(get(userId="seller"), None))
    assert [i["ID"] for i in mine["Items"]] == [str(item_id)]


@mock_aws
@pytest.mark.parametrize("function, role", [("consumer-repost", "consumer"), ("provider-repost", "provider")])
def test_repost_updates_lifestyle_not_move(function, role):
    tables = make_tables()
    old = "2026-01-01T00:00:00"
    for name in (f"lifestyle-{role}", f"move-{role}"):
        tables[name].put_item(Item={"zipcode": ZIP, "ID": 7, "userId": "u", "createdAt": old})

    response = load(function).lambda_handler(
        post({"itemId": 7, "serviceCategory": "Lifestyle", "zipcode": ZIP}), None)

    assert response["statusCode"] == 200
    key = {"zipcode": ZIP, "ID": 7}
    assert tables[f"lifestyle-{role}"].get_item(Key=key)["Item"]["createdAt"] != old
    assert tables[f"move-{role}"].get_item(Key=key)["Item"]["createdAt"] == old


@mock_aws
@pytest.mark.parametrize("provider_type", ["lifestyle", "LIFESTYLE"])
def test_interest_uses_lifestyle_tables(provider_type):
    tables = make_tables()
    for name in ("lifestyle-provider", "eat-provider"):
        tables[name].put_item(Item={"zipcode": ZIP, "ID": 7, "userId": "seller", "interested_by": 0})

    response = load("eabu-update-interested").lambda_handler(post(
        {"zipcode": ZIP, "ID": 7, "interested": 1, "providerType": provider_type, "cUserId": "buyer"}), None)

    assert response["statusCode"] == 200
    key = {"zipcode": ZIP, "ID": 7}
    assert tables["lifestyle-provider"].get_item(Key=key)["Item"]["interested_by"] == 1
    assert tables["eat-provider"].get_item(Key=key)["Item"]["interested_by"] == 0
    assert "Item" in tables["lifestyle-interested"].get_item(Key={"ID": 7, "cUserId": "buyer"})
    assert "Item" not in tables["eat-interested"].get_item(Key={"ID": 7, "cUserId": "buyer"})

    check = load("eabu-check-interested").lambda_handler(
        get(cUserId="buyer", itemId="7", providerType=provider_type), None)
    assert check["statusCode"] == 200 and body(check)["interested"] == 1


@mock_aws
def test_ratings_use_lifestyle_tables_and_count_in_the_average():
    tables = make_tables()
    tables["lifestyle-provider"].put_item(
        Item={"zipcode": ZIP, "ID": 7, "userId": "seller", "totalstars": 0, "ratingscount": 0})
    tables["eat-provider"].put_item(
        Item={"zipcode": ZIP, "ID": 8, "userId": "seller", "totalstars": 2, "ratingscount": 1})

    response = load("eabu-update-ratings").lambda_handler(post(
        {"zipcode": ZIP, "ID": 7, "totalstars": 4, "ratingscount": 1, "providerType": "lifestyle", "cUserId": "buyer"}), None)

    assert response["statusCode"] == 200
    assert "Item" in tables["lifestyle-rated"].get_item(Key={"ID": 7, "cUserId": "buyer"})
    average = load("eabu-get-user-ratings").lambda_handler(get(pUserId="seller"), None)
    assert body(average)["averageRating"] == 3.0


@mock_aws
def test_enforced_auth_blocks_acting_as_someone_else(monkeypatch):
    make_tables()
    item_id = post_request()
    monkeypatch.setenv("AUTH_MODE", "enforce")

    def signed_in_as(user, payload):
        return {**post(payload), "requestContext": {"authorizer": {"claims": {"sub": user}}}}

    request = {"zipcode": ZIP, "title": "T", "description": "D", "userId": "victim", **LOCATION}
    service = {**request, "phone": "6105550100"}
    assert load("consumer-post-lifestyle").lambda_handler(signed_in_as("mallory", request), None)["statusCode"] == 403
    assert load("provider-post-lifestyle").lambda_handler(signed_in_as("mallory", service), None)["statusCode"] == 403
    assert load("provider-offer-lifestyle").lambda_handler(signed_in_as("mallory", offer_payload(item_id)), None)["statusCode"] == 403
    assert load("provider-offer-lifestyle").lambda_handler(signed_in_as("seller", offer_payload(item_id)), None)["statusCode"] == 200

    for function, event in (
        ("consumer-lifestyle-offers", get("mallory", userId="buyer", itemId="NA")),
        ("provider-my-lifestyle-services", get("mallory", userId="seller")),
        ("provider-get-lifestyle", get("mallory", zip="11111", userId="buyer")),
    ):
        assert load(function).lambda_handler(event, None)["statusCode"] == 403, function
    assert load("provider-get-lifestyle").lambda_handler(get(zip=ZIP, userId="NA"), None)["statusCode"] == 401


def test_template_grants_each_function_only_lifestyle_tables():
    template = open(os.path.join(ROOT, "template.yaml")).read()
    functions = {
        "ConsumerPostLifestyleFunction": ("consumer-post-lifestyle", "LifestyleConsumer"),
        "ConsumerGetLifestyleFunction": ("consumer-get-lifestyle", "LifestyleProvider"),
        "ConsumerReceivedLifestyleOfferFunction": ("consumer-lifestyle-offers", "LifestyleOffer"),
        "ProviderPostLifestyleFunction": ("provider-post-lifestyle", "LifestyleProvider"),
        "ProviderGetLifestyleFunction": ("provider-get-lifestyle", "LifestyleConsumer"),
        "LifestyleOfferFunction": ("provider-offer-lifestyle", "LifestyleOffer"),
        "ProviderMyLifestyleServicesFunction": ("provider-my-lifestyle-services", "LifestyleProvider"),
    }
    for logical, (folder, table_param) in functions.items():
        block = re.search(rf"^  {logical}:\n(.*?)(?=^  \S|\Z)", template, re.M | re.S).group(1)
        assert f"CodeUri: eabu-dish/{folder}\n" in block
        assert os.path.exists(os.path.join(ROOT, "eabu-dish", folder, "app.py"))
        assert f"table/${{{table_param}TableName}}" in block
        assert not re.search(r"table/\$\{(Eat|Live|Move)", block), logical
