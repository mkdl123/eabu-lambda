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
    tables["post-credits"] = table(ddb, "eabu-post-credits", [("userId", "HASH")], [("userId", "S")])
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
def test_seller_may_offer_once_per_request():
    make_tables()
    item_id = post_request()
    offer = load("provider-offer-lifestyle")
    assert offer.lambda_handler(post(offer_payload(item_id)), None)["statusCode"] == 200
    again = offer.lambda_handler(post(offer_payload(item_id)), None)
    assert again["statusCode"] == 409
    assert body(again)["code"] == "ALREADY_OFFERED"
    other_seller = {**offer_payload(item_id), "pUserId": "seller2"}
    assert offer.lambda_handler(post(other_seller), None)["statusCode"] == 200
    second_request = post_request()
    assert offer.lambda_handler(post(offer_payload(second_request)), None)["statusCode"] == 200
    offers = load("consumer-lifestyle-offers")
    assert body(offers.lambda_handler(get(userId="buyer", itemId=str(item_id)), None))["Count"] == 2


@mock_aws
def test_offer_keeps_what_one_unit_is():
    make_tables()
    item_id = post_request()
    offer = load("provider-offer-lifestyle")
    assert offer.lambda_handler(post({**offer_payload(item_id), "unit": " 1 hr "}), None)["statusCode"] == 200
    items = body(load("consumer-lifestyle-offers").lambda_handler(get(userId="buyer", itemId=str(item_id)), None))["Items"]
    assert (items[0]["price"], items[0]["unit"]) == ("20", "1 hr")
    too_long = {**offer_payload(item_id), "unit": "x" * 41}
    assert offer.lambda_handler(post(too_long), None)["statusCode"] == 400


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
        ("provider-get-lifestyle", get("mallory", zip="11111", userId="buyer")),
    ):
        assert load(function).lambda_handler(event, None)["statusCode"] == 403, function
    assert load("provider-get-lifestyle").lambda_handler(get(zip=ZIP, userId="NA"), None)["statusCode"] == 401
    # Another user's services are viewable (seller profile) but only the public fields.
    other = load("provider-my-lifestyle-services").lambda_handler(get("mallory", userId="seller"), None)
    assert other["statusCode"] == 200 and "6105550100" not in other["body"]
    assert load("provider-my-lifestyle-services").lambda_handler(get(userId="seller"), None)["statusCode"] == 401


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
        # The one exception: posting counts this month's free posts across all four seller
        # tables, a read-only Query on their userId-index.
        block = re.sub(r"            # The free posts are counted across all four seller tables\.\n"
                       r"            - Effect: Allow\n              Action:\n                - dynamodb:Query\n"
                       r"              Resource:\n(?:                - .*userId-index'\n){4}", "", block)
        assert not re.search(r"table/\$\{(Eat|Live|Move)", block), logical


@mock_aws
@pytest.mark.parametrize("role", ["provider", "consumer"])
@pytest.mark.parametrize("category", ["eat", "live", "move", "lifestyle"])
def test_post_stores_optional_address(category, role):
    tables = make_tables()
    base = {"zipcode": ZIP, "title": "T", "description": "D", "phone": "6105550100", "userId": "u", **LOCATION}
    fn = load(f"{role}-post-{category}")

    with_address = body(fn.lambda_handler(post({**base, "address": "  12 Main St,\n Media PA "}), None))["ID"]
    without = body(fn.lambda_handler(post(base), None))["ID"]
    blank = body(fn.lambda_handler(post({**base, "address": "   "}), None))["ID"]

    get_item = lambda i: tables[f"{category}-{role}"].get_item(Key={"zipcode": ZIP, "ID": i})["Item"]
    assert get_item(with_address)["address"] == "12 Main St, Media PA"
    assert "address" not in get_item(without)
    assert "address" not in get_item(blank)

    too_long = fn.lambda_handler(post({**base, "address": "x" * 201}), None)
    assert too_long["statusCode"] == 400


@mock_aws
def test_address_is_shown_on_seller_profile():
    make_tables()
    load("provider-post-lifestyle").lambda_handler(
        post({"zipcode": ZIP, "title": "Gym", "description": "Walk in", "phone": "6105550100", "userId": "seller",
              "address": "1 Gym Rd", **LOCATION}), None)
    other = body(load("provider-my-lifestyle-services").lambda_handler(get("mallory", userId="seller"), None))
    assert other["Items"][0]["address"] == "1 Gym Rd"
    assert "phone" not in other["Items"][0]


@mock_aws
@pytest.mark.parametrize("category", ["eat", "live", "move", "lifestyle"])
def test_seller_post_limit_is_ten_per_month_in_total(category):
    from datetime import datetime, timedelta, timezone
    tables = make_tables()
    fn = load(f"provider-post-{category}")
    payload = lambda user="seller": post({"zipcode": ZIP, "title": "T", "description": "D", "phone": "6105550100",
                                          "userId": user, **LOCATION})

    # Posts from before this month don't count.
    old = (datetime.now(timezone.utc) - timedelta(days=32)).isoformat()
    for i in range(5):
        tables[f"{category}-provider"].put_item(Item={"zipcode": ZIP, "ID": 5000 + i, "userId": "seller", "createdAt": old})

    for _ in range(10):
        assert fn.lambda_handler(payload(), None)["statusCode"] == 200
    res = fn.lambda_handler(payload(), None)
    assert res["statusCode"] == 429 and body(res)["code"] == "POST_LIMIT"
    assert "eabu-" not in res["body"]
    stored = tables[f"{category}-provider"].scan()["Items"]
    assert len([i for i in stored if i.get("userId") == "seller"]) == 15, "the 11th post was not stored"

    assert fn.lambda_handler(payload("other-seller"), None)["statusCode"] == 200, "limits are per seller"
    other = "eat" if category != "eat" else "live"
    res = load(f"provider-post-{other}").lambda_handler(payload(), None)
    assert res["statusCode"] == 429, "the 10 free posts are shared by all categories"


@mock_aws
def test_repost_of_expired_listing_respects_post_limit(monkeypatch):
    from datetime import datetime, timedelta, timezone
    tables = make_tables()
    t = tables["eat-provider"]
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=40)).isoformat()
    recent = now.isoformat()   # this month, whatever today's date
    t.put_item(Item={"zipcode": ZIP, "ID": 900, "userId": "seller", "createdAt": old})       # expired
    t.put_item(Item={"zipcode": ZIP, "ID": 901, "userId": "seller", "createdAt": recent})    # live
    repost = lambda item_id, user="seller": load("provider-repost").lambda_handler(
        {**post({"itemId": item_id, "serviceCategory": "Eat", "zipcode": ZIP}),
         "requestContext": {"authorizer": {"claims": {"sub": user}}}}, None)

    assert repost(900)["statusCode"] == 200, "under the limit an expired listing can be revived"
    t.put_item(Item={"zipcode": ZIP, "ID": 900, "userId": "seller", "createdAt": old})
    for i in range(9):  # 901 + 9 more = 10 live posts
        t.put_item(Item={"zipcode": ZIP, "ID": 1000 + i, "userId": "seller", "createdAt": recent})

    res = repost(900)
    assert res["statusCode"] == 429 and body(res)["code"] == "POST_LIMIT"
    assert t.get_item(Key={"zipcode": ZIP, "ID": 900})["Item"]["createdAt"] == old, "not revived"
    assert repost(901)["statusCode"] == 200, "reposting a live listing doesn't add a post"

    monkeypatch.setenv("AUTH_MODE", "enforce")
    assert repost(901, user="mallory")["statusCode"] == 404
    assert repost(424242)["statusCode"] == 404
