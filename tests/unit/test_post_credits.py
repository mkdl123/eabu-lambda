"""Extra posts: past the free 10 posts per category in 30 days, sellers buy packs of 15
($0.99 consumable via Apple in-app purchase). Packs never expire and work in every
category. Apple's signatures are stubbed as in test_paid_ads."""
import json

import pytest
from moto import mock_aws

from test_lifestyle import make_tables, LOCATION, ZIP
from test_security import load, post
from my_utils import apple_iap, helpers

PRODUCT = "net.eabu.app.posts15"


def as_user(event, user):
    return {**event, "requestContext": {"authorizer": {"claims": {"sub": user}}}}


def body(res):
    return json.loads(res["body"])


@pytest.fixture(autouse=True)
def enforce(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "enforce")
    monkeypatch.setenv("AUTH_USER_ID_CLAIM", "sub")
    monkeypatch.setattr(helpers, "_credits_table", None)
    monkeypatch.setattr(helpers, "_provider_tables", {})


def tx(user, tid="5000", product=PRODUCT, revoked=False, token=None):
    return {"originalTransactionId": tid, "transactionId": tid, "bundleId": "net.eabu.app",
            "productId": product, "appAccountToken": token or apple_iap.account_token(user),
            "expiresAt": 0, "revoked": revoked, "environment": "Sandbox"}


def stub(monkeypatch, decoded):
    def verify(signed):
        if signed not in decoded:
            raise apple_iap.PurchaseError("bad signature")
        return decoded[signed]
    monkeypatch.setattr(apple_iap, "verify_transaction", verify)


def buy(user, signed):
    return load("eabu-post-credits").lambda_handler(as_user(post({"signedTransaction": signed}), user), None)


def balance(user):
    return body(load("eabu-post-credits").lambda_handler(
        as_user({"httpMethod": "GET", "headers": {}, "pathParameters": {"userId": user}}, user), None))


def seller_post(category, user="seller"):
    return load(f"provider-post-{category}").lambda_handler(as_user(post(
        {"zipcode": ZIP, "title": "T", "description": "D", "phone": "6105550100", "userId": user, **LOCATION}), user), None)


@mock_aws
def test_buying_a_pack_adds_15_posts_once(monkeypatch):
    make_tables()
    stub(monkeypatch, {"good": tx("seller")})
    assert balance("seller")["balance"] == 0
    res = buy("seller", "good")
    assert res["statusCode"] == 200 and body(res)["balance"] == 15 and body(res)["added"] is True
    again = buy("seller", "good")   # StoreKit replays an unfinished transaction
    assert again["statusCode"] == 200 and body(again)["balance"] == 15 and body(again)["added"] is False
    info = balance("seller")
    assert info["balance"] == 15 and info["productId"] == PRODUCT
    assert info["appAccountToken"] == apple_iap.account_token("seller")


@mock_aws
@pytest.mark.parametrize("decoded,user", [
    (tx("seller", product="net.eabu.app.ad.quarterly"), "seller"),   # wrong product
    (tx("seller"), "mallory"),                                       # someone else's purchase
    (tx("seller", revoked=True), "seller"),                          # refunded
])
def test_bad_purchases_add_nothing(monkeypatch, decoded, user):
    make_tables()
    stub(monkeypatch, {"signed": decoded})
    assert buy(user, "signed")["statusCode"] == 403
    assert buy(user, "forged")["statusCode"] == 403
    assert balance(user)["balance"] == 0


@mock_aws
def test_purchase_needs_sign_in_and_balance_is_private(monkeypatch):
    make_tables()
    stub(monkeypatch, {"good": tx("seller")})
    res = load("eabu-post-credits").lambda_handler(post({"signedTransaction": "good"}), None)
    assert res["statusCode"] == 401
    peek = load("eabu-post-credits").lambda_handler(
        as_user({"httpMethod": "GET", "headers": {}, "pathParameters": {"userId": "seller"}}, "mallory"), None)
    assert peek["statusCode"] in (401, 403)


@mock_aws
def test_extra_posts_are_used_past_the_free_limit(monkeypatch):
    make_tables()
    stub(monkeypatch, {"pack": tx("seller")})
    for _ in range(10):
        assert seller_post("eat")["statusCode"] == 200
    res = seller_post("eat")
    assert res["statusCode"] == 429 and body(res)["code"] == "POST_LIMIT" and body(res)["canBuyMore"] is True

    buy("seller", "pack")
    for _ in range(15):
        assert seller_post("eat")["statusCode"] == 200
    assert balance("seller")["balance"] == 0
    assert seller_post("eat")["statusCode"] == 429, "pack used up"
    assert seller_post("move")["statusCode"] == 429, "free posts are shared, so other categories are full too"


@mock_aws
def test_free_posts_are_shared_and_a_pack_works_in_every_category(monkeypatch):
    tables = make_tables()
    stub(monkeypatch, {"pack": tx("seller")})
    buy("seller", "pack")
    for category in ["live"] * 4 + ["eat"] * 3 + ["move"] * 2 + ["lifestyle"]:
        assert seller_post(category)["statusCode"] == 200
    assert balance("seller")["balance"] == 15, "the 10 free posts (any mix of categories) come first"
    assert seller_post("move")["statusCode"] == 200
    assert balance("seller")["balance"] == 14, "the 11th post, in any category, uses an extra post"
    assert seller_post("eat")["statusCode"] == 200
    assert balance("seller")["balance"] == 13


@mock_aws
def test_failed_save_gives_the_extra_post_back(monkeypatch):
    tables = make_tables()
    tables["post-credits"].put_item(Item={"userId": "seller", "balance": 1})
    for _ in range(10):
        assert seller_post("eat")["statusCode"] == 200
    fn = load("provider-post-eat")
    from botocore.exceptions import ClientError

    def boom(**kwargs):
        raise ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "PutItem")
    monkeypatch.setattr(fn.table, "put_item", boom)
    res = fn.lambda_handler(as_user(post({"zipcode": ZIP, "title": "T", "description": "D", "phone": "1",
                                          "userId": "seller", **LOCATION}), "seller"), None)
    assert res["statusCode"] == 500
    assert helpers.post_credit_balance("seller") == 1


@mock_aws
def test_refund_takes_the_pack_back_once(monkeypatch):
    tables = make_tables()
    stub(monkeypatch, {"pack": tx("seller", tid="77")})
    buy("seller", "pack")
    tables["post-credits"].update_item(Key={"userId": "seller"}, UpdateExpression="ADD balance :m",
                                       ExpressionAttributeValues={":m": -5})   # 5 used
    notes = load("eabu-apple-notifications")
    note = {"type": "REFUND", "subtype": None, "uuid": "n1", "environment": "Sandbox",
            "transaction": tx("seller", tid="77"), "renewal": None}
    assert notes.apply_notification(note) == "posts taken back"
    assert helpers.post_credit_balance("seller") == 0, "never below zero"
    assert notes.apply_notification(note) == "already refunded"
    assert notes.apply_notification({**note, "transaction": tx("seller", tid="unknown")}) == "unknown"


@mock_aws
def test_free_limit_can_be_lowered_for_one_account(monkeypatch):
    tables = make_tables()
    tables["post-credits"].put_item(Item={"userId": "tester", "freeLimit": 1})
    assert seller_post("eat", user="tester")["statusCode"] == 200
    res = seller_post("move", user="tester")
    assert res["statusCode"] == 429 and "your 1 free posts" in body(res)["msg"]
    assert balance("tester")["freeLimit"] == 1
    assert balance("seller")["freeLimit"] == 10, "everyone else keeps the normal limit"
    assert seller_post("eat")["statusCode"] == 200


def test_free_posts_reset_on_the_1st_us_eastern():
    from datetime import datetime, timezone
    # 2026-12-01 03:30 UTC is still Nov 30 in New York: November's window.
    late_nov = datetime(2026, 12, 1, 3, 30, tzinfo=timezone.utc)
    assert helpers.post_limit_cutoff(late_nov) == "2026-11-01T04:00:00+00:00"
    assert helpers.free_posts_reset_on(late_nov) == "2026-12-01"
    # 05:30 UTC is Dec 1 in New York: a new month.
    dec = datetime(2026, 12, 1, 5, 30, tzinfo=timezone.utc)
    assert helpers.post_limit_cutoff(dec) == "2026-12-01T05:00:00+00:00"
    assert helpers.free_posts_reset_on(dec) == "2027-01-01"
    # Both createdAt styles in the tables compare correctly against the cutoff.
    cutoff = helpers.post_limit_cutoff(dec)
    assert "2026-12-01T05:00:01.123456" >= cutoff and "2026-12-01T05:00:01+00:00" >= cutoff
    assert "2026-12-01T04:59:59.999999" < cutoff


@mock_aws
def test_balance_reports_this_months_free_posts_per_category(monkeypatch):
    from datetime import datetime, timedelta, timezone
    tables = make_tables()
    last_month = (datetime.now(timezone.utc) - timedelta(days=32)).isoformat()
    tables["eat-provider"].put_item(Item={"zipcode": ZIP, "ID": 1, "userId": "seller", "createdAt": last_month})
    seller_post("eat")
    seller_post("eat")
    seller_post("move")
    info = balance("seller")
    assert info["used"] == {"eat": 2, "live": 0, "move": 1, "lifestyle": 0}
    assert info["usedTotal"] == 3
    assert info["resetsOn"] == helpers.free_posts_reset_on() and info["freeLimit"] == 10


@mock_aws
def test_unused_extra_posts_carry_into_next_month(monkeypatch):
    from datetime import datetime, timedelta, timezone
    tables = make_tables()
    tables["post-credits"].put_item(Item={"userId": "seller", "balance": 7})
    last_month = (datetime.now(timezone.utc) - timedelta(days=32)).isoformat()
    for i in range(10):   # last month's free posts are used up but don't count now
        tables["eat-provider"].put_item(Item={"zipcode": ZIP, "ID": 100 + i, "userId": "seller", "createdAt": last_month})
    assert seller_post("eat")["statusCode"] == 200
    assert helpers.post_credit_balance("seller") == 7, "a new month's free posts come before extra posts"
