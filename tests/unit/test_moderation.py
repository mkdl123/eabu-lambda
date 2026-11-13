"""Blocking users and reporting content (App Store guideline 1.2)."""
import json

import boto3
import pytest
from moto import mock_aws

from test_security import load, post
from test_favourites import setup, add, get, remove, listing, as_user
from test_lifestyle import body, ZIP


@pytest.fixture(autouse=True)
def enforce(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "enforce")


def nearby(fn, user, kind):
    event = {"httpMethod": "GET", "pathParameters": {"userId": user, "kind": kind},
             "resource": "/favourites-feed/{userId}/{kind}",
             "queryStringParameters": {"zip": ZIP, "nearby": "1"}}
    return body(fn.lambda_handler(as_user(event, user), None))["Items"]


@mock_aws
def test_block_list_add_remove_and_private():
    _, fn = setup()
    assert add(fn, "buyer", "blocked", "s1")["statusCode"] == 200
    assert add(fn, "buyer", "blocked", "s1")["statusCode"] == 200, "blocking twice is harmless"
    assert [i["favUserId"] for i in body(get(fn, "buyer", "blocked"))["Items"]] == ["s1"]
    assert body(get(fn, "buyer", "seller"))["Items"] == [], "blocks aren't favourites"
    assert get(fn, "buyer", "blocked", caller="mallory")["statusCode"] == 403
    assert add(fn, "buyer", "blocked", "buyer")["statusCode"] == 400, "not yourself"
    assert remove(fn, "buyer", "blocked", "s1")["statusCode"] == 200
    assert body(get(fn, "buyer", "blocked"))["Items"] == []
    assert get(fn, "buyer", "blocked", feed=True)["statusCode"] == 400, "no feed of blocked users"


@mock_aws
def test_block_limit_is_500_not_50():
    _, fn = setup()
    for i in range(60):
        assert add(fn, "buyer", "blocked", f"u{i}")["statusCode"] == 200


@mock_aws
def test_blocked_users_posts_leave_both_feeds():
    _, fn = setup()
    fn._scan_cache.clear()
    listing("eat", "provider", "s1")
    listing("live", "provider", "s2")
    add(fn, "buyer", "seller", "s1")
    add(fn, "buyer", "seller", "s2")
    assert sorted(i["userId"] for i in nearby(fn, "buyer", "seller")) == ["s1", "s2"]

    add(fn, "buyer", "blocked", "s1")
    assert [i["userId"] for i in nearby(fn, "buyer", "seller")] == ["s2"]
    assert [i["userId"] for i in body(get(fn, "buyer", "seller", feed=True))["Items"]] == ["s2"]
    other = nearby(fn, "seller-x", "seller")
    assert sorted(i["userId"] for i in other) == ["s1", "s2"], "only the blocker stops seeing them"

    remove(fn, "buyer", "blocked", "s1")
    assert sorted(i["userId"] for i in nearby(fn, "buyer", "seller")) == ["s1", "s2"]


def make_feedback():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    return ddb.create_table(TableName="eabu-feedback", BillingMode="PAY_PER_REQUEST",
                            KeySchema=[{"AttributeName": "createdAt", "KeyType": "HASH"},
                                       {"AttributeName": "ID", "KeyType": "RANGE"}],
                            AttributeDefinitions=[{"AttributeName": "createdAt", "AttributeType": "S"},
                                                  {"AttributeName": "ID", "AttributeType": "N"}])


def report(fn, fields, caller="buyer"):
    return fn.lambda_handler(as_user(post(fields), caller), None)


GOOD = {"userId": "buyer", "reportedUserId": "s1", "contentType": "post", "reason": "offensive",
        "category": "eat", "itemId": "12", "zipcode": ZIP, "title": "Cakes", "details": "Rude words"}


@mock_aws
def test_report_is_saved_and_emailed(monkeypatch):
    monkeypatch.setenv("REPORT_EMAIL", "mod@eabu.net")
    table = make_feedback()
    boto3.client("ses", region_name="us-east-1").verify_domain_identity(Domain="eabu.net")
    fn = load("eabu-report")
    sent = []
    real = fn.ses.send_email
    monkeypatch.setattr(fn.ses, "send_email", lambda **kw: (sent.append(kw), real(**kw))[1])

    res = report(fn, GOOD)
    assert res["statusCode"] == 200
    [row] = table.scan()["Items"]
    assert row["type"] == "report" and row["status"] == "open"
    assert (row["reporterUserId"], row["reportedUserId"], row["reason"], row["title"]) == ("buyer", "s1", "offensive", "Cakes")
    assert int(row["ID"]) == json.loads(res["body"])["ID"]
    assert sent[0]["Destination"]["ToAddresses"] == ["mod@eabu.net"]
    text = sent[0]["Message"]["Body"]["Text"]["Data"]
    assert "s1" in text and "Rude words" in text and "Offensive" in sent[0]["Message"]["Subject"]["Data"]


@mock_aws
def test_report_is_validated_and_tied_to_the_caller(monkeypatch):
    monkeypatch.setenv("REPORT_EMAIL", "")
    table = make_feedback()
    fn = load("eabu-report")
    assert report(fn, GOOD, caller="mallory")["statusCode"] == 403, "can't report as someone else"
    assert fn.lambda_handler(post(GOOD), None)["statusCode"] == 401
    assert report(fn, {**GOOD, "reportedUserId": "buyer"})["statusCode"] == 400, "not yourself"
    assert report(fn, {**GOOD, "reason": "meh"})["statusCode"] == 400
    assert report(fn, {**GOOD, "contentType": "table"})["statusCode"] == 400
    assert report(fn, {**GOOD, "category": "x"})["statusCode"] == 400
    assert report(fn, {**GOOD, "reportedUserId": "../x"})["statusCode"] == 400
    assert table.scan()["Items"] == []
    res = report(fn, {**GOOD, "details": "x" * 5000, "contentType": "profile", "category": ""})
    assert res["statusCode"] == 200
    assert len(table.scan()["Items"][0]["details"]) == 1000


@mock_aws
def test_report_saved_even_if_email_fails(monkeypatch):
    monkeypatch.setenv("REPORT_EMAIL", "mod@eabu.net")
    table = make_feedback()
    fn = load("eabu-report")  # SES identity not verified: send_email fails
    assert report(fn, GOOD)["statusCode"] == 200
    assert len(table.scan()["Items"]) == 1
