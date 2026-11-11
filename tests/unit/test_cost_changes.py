"""Cost work: posts expire after 60 days, reads follow every result page, the nearby feed
reuses table scans briefly, and the template settings that keep Lambda cheap."""
import os
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from test_security import ROOT, load, post
from test_lifestyle import make_tables, body, ZIP, LOCATION
from my_utils import helpers

BUCKET = "eabu-identity-pictures"
SIXTY_DAYS = 60 * 86400


def as_user(event, user):
    return {**event, "requestContext": {"authorizer": {"claims": {"sub": user}}}}


@pytest.fixture(autouse=True)
def enforce(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "enforce")


@mock_aws
@pytest.mark.parametrize("role", ["consumer", "provider"])
@pytest.mark.parametrize("category", ["eat", "live", "move", "lifestyle"])
def test_new_posts_expire_after_60_days(role, category):
    tables = make_tables()
    fields = {"zipcode": ZIP, "title": "T", "description": "D", "userId": "u", "phone": "6105550100", **LOCATION}
    res = load(f"{role}-post-{category}").lambda_handler(as_user(post(fields), "u"), None)
    assert res["statusCode"] == 200
    item = tables[f"{category}-{role}"].get_item(Key={"zipcode": ZIP, "ID": body(res)["ID"]})["Item"]
    assert abs(int(item["expiresAt"]) - (time.time() + SIXTY_DAYS)) < 60


class SpyS3:
    """Wraps the real (moto) S3 client and records copy_object calls."""
    def __init__(self, client):
        self.client, self.copies = client, []

    def copy_object(self, **kwargs):
        self.copies.append(kwargs["Key"])
        return self.client.copy_object(**kwargs)

    def __getattr__(self, name):
        return getattr(self.client, name)


@mock_aws
@pytest.mark.parametrize("function, role", [("consumer-repost", "consumer"), ("provider-repost", "provider")])
def test_repost_extends_expiry(function, role, monkeypatch):
    tables = make_tables()
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET)
    s3.put_object(Bucket=BUCKET, Key="listings/eat/7/1.jpg", Body=b"x", ContentType="image/jpeg")
    spy = SpyS3(s3)
    monkeypatch.setattr(helpers, "_s3_client", spy)
    tables[f"eat-{role}"].put_item(Item={"zipcode": ZIP, "ID": 7, "userId": "u", "picture1": "t",
                                        "createdAt": "2026-01-01T00:00:00", "expiresAt": 1})

    res = load(function).lambda_handler(as_user(post({"itemId": 7, "serviceCategory": "Eat", "zipcode": ZIP}), "u"),
                                        None)

    assert res["statusCode"] == 200
    item = tables[f"eat-{role}"].get_item(Key={"zipcode": ZIP, "ID": 7})["Item"]
    assert abs(int(item["expiresAt"]) - (time.time() + SIXTY_DAYS)) < 60
    if role == "provider":
        assert spy.copies == ["listings/eat/7/1.jpg"], "the picture's expiry restarts with the listing"
        assert s3.head_object(Bucket=BUCKET, Key="listings/eat/7/1.jpg")["ContentType"] == "image/jpeg"
    else:
        assert spy.copies == [], "buyer requests have no pictures"


class PagedTable:
    """A table whose query answers in fixed pages, like DynamoDB at its 1 MB limit."""
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def query(self, **kwargs):
        self.calls.append(kwargs.get("ExclusiveStartKey"))
        n = len(self.calls) - 1
        page = {"Items": self.pages[n], "ScannedCount": len(self.pages[n])}
        if n + 1 < len(self.pages):
            page["LastEvaluatedKey"] = {"page": n + 1}
        return page


def test_query_all_reads_every_page():
    table = PagedTable([[{"ID": 1}], [{"ID": 2}, {"ID": 3}], [{"ID": 4}]])
    result = helpers.query_all(table, KeyConditionExpression="x")
    assert [i["ID"] for i in result["Items"]] == [1, 2, 3, 4]
    assert result["Count"] == 4 and result["ScannedCount"] == 4
    assert table.calls == [None, {"page": 1}, {"page": 2}]


@mock_aws
@pytest.mark.parametrize("name, path", [
    ("consumer-get-eat", {"zip": ZIP}),
    ("provider-get-live", {"zip": ZIP, "userId": "NA"}),
    ("provider-my-move-services", {"userId": "seller"}),
    ("consumer-lifestyle-offers", {"userId": "buyer", "itemId": "NA"}),
])
def test_read_functions_return_every_page(name, path):
    module = load(name)
    now = datetime.now(timezone.utc).isoformat()
    module.table = PagedTable([[{"ID": Decimal(1), "zipcode": ZIP, "createdAt": now}],
                               [{"ID": Decimal(2), "zipcode": ZIP, "createdAt": now}]])
    caller = path.get("userId") if path.get("userId") not in (None, "NA") else "someone"
    res = module.lambda_handler(as_user({"httpMethod": "GET", "pathParameters": path}, caller), None)
    assert res["statusCode"] == 200
    assert sorted(i["ID"] for i in body(res)["Items"]) == ["1", "2"], "numbers still go out as strings"


@mock_aws
def test_nearby_feed_reuses_scans_for_a_minute(monkeypatch):
    tables = make_tables()
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(TableName="eabu-favourites", BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "userId", "KeyType": "HASH"},
                                {"AttributeName": "favKey", "KeyType": "RANGE"}],
                     AttributeDefinitions=[{"AttributeName": "userId", "AttributeType": "S"},
                                           {"AttributeName": "favKey", "AttributeType": "S"}])
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    fn = load("eabu-favourites")
    now = datetime.now(timezone.utc)
    tables["eat-provider"].put_item(Item={"zipcode": ZIP, "ID": 1, "userId": "s1", "title": "a",
                                          "createdAt": now.isoformat()})
    scans = []
    real = fn._recent_posts_uncached
    monkeypatch.setattr(fn, "_recent_posts_uncached", lambda table, cutoff: scans.append(table.name) or real(table, cutoff))

    def feed(query):
        event = {"httpMethod": "GET", "pathParameters": {"userId": "buyer", "kind": "seller"},
                 "resource": "/favourites-feed/{userId}/{kind}",
                 "queryStringParameters": {"zip": ZIP, "nearby": "1", **query}}
        return body(fn.lambda_handler(as_user(event, "buyer"), None))

    first = feed({"limit": "1"})
    assert len(scans) == 4, "one scan per category table"
    feed({"limit": "1", **({"cursor": first["nextCursor"]} if first.get("nextCursor") else {})})
    assert len(scans) == 4, "the next page reuses the scans"

    tables["eat-provider"].put_item(Item={"zipcode": ZIP, "ID": 2, "userId": "s2", "title": "b",
                                          "createdAt": now.isoformat()})
    monkeypatch.setattr(fn, "SCAN_CACHE_SECONDS", 0)
    assert len(feed({})["Items"]) == 2, "after the cache window new posts show up"
    assert len(scans) == 8


def test_template_lambda_settings():
    template = open(os.path.join(ROOT, "template.yaml")).read()
    globals_block = template.split("\nParameters:")[0]
    assert re.search(r"Architectures:\s*\n\s*- arm64", globals_block), "Graviton is 20% cheaper per GB-second"
    assert re.search(r"^    Timeout: 10$", globals_block, re.M)
    repost = re.search(r"^  EabuProviderRepostFuctionName:\n(.*?)(?=^  \S)", template, re.M | re.S).group(1)
    assert "s3:PutObject" in repost and "listings/*" in repost
