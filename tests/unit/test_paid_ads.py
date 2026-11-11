"""Paid ads: Apple in-app subscription ($9.99 / 3 months), shown nationwide, 5-day reminder.

Apple's signatures are stubbed: verify_transaction / verify_notification are replaced
with functions that return already-decoded payloads. The real verifier is checked
separately to reject anything not signed by Apple.
"""
import json
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from test_security import load, post
from my_utils import apple_iap

NOW = int(datetime.now(timezone.utc).timestamp())
DAY = 86400
PRODUCT = "net.eabu.app.ad.quarterly"
AD = {"address": "1 Main", "name": "Eve", "email": "eve@example.com", "phone": "1", "zipcode": "19060",
      "title": "Eve's Bakery", "subtitle": "Fresh bread", "description": "Open daily"}


def as_user(event, user):
    return {**event, "requestContext": {"authorizer": {"claims": {"sub": user}}}}


def body(res):
    return json.loads(res["body"])


@pytest.fixture(autouse=True)
def enforce(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "enforce")
    monkeypatch.setenv("AUTH_USER_ID_CLAIM", "sub")


def make_ad_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    return ddb.create_table(
        TableName="eabu-advertisement",
        KeySchema=[{"AttributeName": "zipcode", "KeyType": "HASH"}, {"AttributeName": "ID", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "zipcode", "AttributeType": "S"}, {"AttributeName": "ID", "AttributeType": "N"},
                              {"AttributeName": "status", "AttributeType": "S"}],
        GlobalSecondaryIndexes=[{"IndexName": "status-index", "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}],
                                 "Projection": {"ProjectionType": "ALL"}}],
        BillingMode="PAY_PER_REQUEST")


def tx(user, otid="1000", expires=NOW + 90 * DAY, product=PRODUCT, revoked=False, token=None):
    return {"originalTransactionId": otid, "transactionId": otid + "-1", "bundleId": "net.eabu.app",
            "productId": product, "appAccountToken": token or apple_iap.account_token(user),
            "expiresAt": expires, "revoked": revoked, "environment": "Sandbox"}


def stub_transactions(monkeypatch, decoded):
    """signedTransaction strings in tests are keys of `decoded`; anything else fails."""
    def verify(signed):
        if signed not in decoded:
            raise apple_iap.PurchaseError("bad signature")
        return decoded[signed]
    monkeypatch.setattr(apple_iap, "verify_transaction", verify)


def create_paid_ad(user, **extra):
    res = load("eabu-advertisement-post").lambda_handler(as_user(post({**AD, "paid": True, **extra}), user), None)
    return res


def buy(user, ad, signed):
    return load("eabu-ad-purchase").lambda_handler(
        as_user(post({"ID": ad["ID"], "zipcode": ad["zipcode"], "signedTransaction": signed}), user), None)


def public_ads(zip_code):
    return body(load("eabu-advertisement-get").lambda_handler(
        {"headers": {}, "pathParameters": {"status": "active"}, "queryStringParameters": {"zip": zip_code}}, None))


@mock_aws
def test_paid_ad_goes_live_nationwide_after_verified_purchase(monkeypatch):
    table = make_ad_table()
    res = create_paid_ad("eve")
    assert res["statusCode"] == 200
    ad = body(res)
    assert ad["status"] == "awaiting_payment" and ad["productId"] == PRODUCT
    assert ad["appAccountToken"] == apple_iap.account_token("eve")
    stored = table.get_item(Key={"zipcode": "19060", "ID": ad["ID"]})["Item"]
    assert stored["ownerUserId"] == "eve" and stored["paid"] is True

    # Not shown before payment.
    assert public_ads("19060")["Count"] == 0

    stub_transactions(monkeypatch, {"good": tx("eve")})
    res = buy("eve", ad, "good")
    assert res["statusCode"] == 200 and body(res)["status"] == "active"
    stored = table.get_item(Key={"zipcode": "19060", "ID": ad["ID"]})["Item"]
    assert int(stored["paidThrough"]) == NOW + 90 * DAY and stored["originalTransactionId"] == "1000"

    # Shown in a ZIP 2,500 miles away, flagged nationwide, with no contact details.
    far = public_ads("94103")
    assert far["Count"] == 1 and far["Items"][0]["nationwide"] is True
    assert not {"email", "phone", "name", "address", "ownerUserId", "originalTransactionId"} & set(far["Items"][0])


@mock_aws
def test_free_ads_stay_local_beside_paid(monkeypatch):
    table = make_ad_table()
    table.put_item(Item={"zipcode": "19060", "ID": 5, "status": "active", "title": "Local shop"})
    assert public_ads("19060")["Count"] == 1
    assert public_ads("94103")["Count"] == 0


@mock_aws
def test_purchase_rejections(monkeypatch):
    make_ad_table()
    ad = body(create_paid_ad("eve"))
    stub_transactions(monkeypatch, {
        "mallory-bought": tx("mallory"),
        "wrong-product": tx("eve", product="net.eabu.app.other"),
        "expired": tx("eve", expires=NOW - 10),
        "refunded": tx("eve", revoked=True),
        "good": tx("eve"),
    })
    assert buy("eve", ad, "forged")["statusCode"] == 403
    assert buy("eve", ad, "mallory-bought")["statusCode"] == 403, "someone else's purchase"
    assert buy("eve", ad, "wrong-product")["statusCode"] == 403
    assert buy("eve", ad, "expired")["statusCode"] == 403
    assert buy("eve", ad, "refunded")["statusCode"] == 403
    # Mallory can't attach Eve's valid purchase to Eve's ad, nor activate Eve's ad.
    assert buy("mallory", ad, "good")["statusCode"] == 403
    unsigned = load("eabu-ad-purchase").lambda_handler(post({"ID": ad["ID"], "zipcode": "19060", "signedTransaction": "good"}), None)
    assert unsigned["statusCode"] == 401
    assert public_ads("19060")["Count"] == 0


@mock_aws
def test_one_paid_ad_per_owner_and_restore(monkeypatch):
    table = make_ad_table()
    first = body(create_paid_ad("eve"))
    second = body(create_paid_ad("eve", title="Second try"))
    # The unpaid draft is replaced, so only one paid ad row exists.
    assert table.get_item(Key={"zipcode": "19060", "ID": first["ID"]}).get("Item") is None
    stub_transactions(monkeypatch, {"good": tx("eve")})
    assert buy("eve", second, "good")["statusCode"] == 200
    assert create_paid_ad("eve")["statusCode"] == 409, "already has an ad running"

    # Restore on a new phone: no ID, the subscription finds its ad.
    res = load("eabu-ad-purchase").lambda_handler(as_user(post({"signedTransaction": "good"}), "eve"), None)
    assert res["statusCode"] == 200 and body(res)["ID"] == second["ID"]

    mine = load("eabu-ad-purchase").lambda_handler(as_user({"httpMethod": "GET", "pathParameters": {"userId": "eve"}}, "eve"), None)
    assert mine["statusCode"] == 200 and body(mine)["ad"]["title"] == "Second try" and body(mine)["ad"]["autoRenew"] is True
    other = load("eabu-ad-purchase").lambda_handler(as_user({"httpMethod": "GET", "pathParameters": {"userId": "eve"}}, "mallory"), None)
    assert other["statusCode"] == 403


def notify(monkeypatch, kind, transaction=None, renewal=None, subtype=None):
    note = {"type": kind, "subtype": subtype, "uuid": "n-1", "environment": "Sandbox",
            "transaction": transaction, "renewal": renewal}
    monkeypatch.setattr(apple_iap, "verify_notification", lambda signed: note if signed == "signed" else (_ for _ in ()).throw(apple_iap.PurchaseError("bad")))
    return load("eabu-apple-notifications").lambda_handler(
        {"httpMethod": "POST", "body": json.dumps({"signedPayload": "signed"})}, None)


@mock_aws
def test_notifications_renew_cancel_and_expire(monkeypatch):
    table = make_ad_table()
    ad = body(create_paid_ad("eve"))
    stub_transactions(monkeypatch, {"good": tx("eve")})
    buy("eve", ad, "good")
    key = {"zipcode": "19060", "ID": ad["ID"]}

    assert notify(monkeypatch, "DID_RENEW", tx("eve", expires=NOW + 180 * DAY), {"autoRenew": True, "originalTransactionId": "1000"})["statusCode"] == 200
    assert int(table.get_item(Key=key)["Item"]["paidThrough"]) == NOW + 180 * DAY

    # A late, older notification never shortens the paid period.
    notify(monkeypatch, "DID_RENEW", tx("eve", expires=NOW + 90 * DAY))
    assert int(table.get_item(Key=key)["Item"]["paidThrough"]) == NOW + 180 * DAY

    notify(monkeypatch, "DID_CHANGE_RENEWAL_STATUS", tx("eve", expires=NOW + 180 * DAY),
           {"autoRenew": False, "originalTransactionId": "1000"}, subtype="AUTO_RENEW_DISABLED")
    item = table.get_item(Key=key)["Item"]
    assert item["autoRenew"] is False and item["status"] == "active", "cancelled ads run to the end"

    notify(monkeypatch, "EXPIRED", tx("eve", expires=NOW - 1), subtype="VOLUNTARY")
    assert table.get_item(Key=key)["Item"]["status"] == "expired"
    assert public_ads("94103")["Count"] == 0


@mock_aws
def test_notification_signature_and_unknowns(monkeypatch):
    make_ad_table()
    fn = load("eabu-apple-notifications")
    monkeypatch.setattr(apple_iap, "verify_notification", lambda s: (_ for _ in ()).throw(apple_iap.PurchaseError("bad")))
    assert fn.lambda_handler({"httpMethod": "POST", "body": json.dumps({"signedPayload": "x"})}, None)["statusCode"] == 400
    assert fn.lambda_handler({"httpMethod": "POST", "body": "not json"}, None)["statusCode"] == 400
    assert fn.lambda_handler({"httpMethod": "GET"}, None)["statusCode"] == 405
    # Verified but about a subscription we don't have: 200, so Apple stops retrying.
    assert notify(monkeypatch, "DID_RENEW", tx("zed", otid="999"))["statusCode"] == 200
    assert notify(monkeypatch, "TEST")["statusCode"] == 200


@mock_aws
def test_removed_ad_stays_down(monkeypatch):
    table = make_ad_table()
    ad = body(create_paid_ad("eve"))
    stub_transactions(monkeypatch, {"good": tx("eve")})
    buy("eve", ad, "good")
    table.update_item(Key={"zipcode": "19060", "ID": ad["ID"]}, UpdateExpression="SET #s = :r",
                      ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":r": "removed"})
    assert buy("eve", ad, "good")["statusCode"] == 403
    notify(monkeypatch, "DID_RENEW", tx("eve", expires=NOW + 180 * DAY))
    assert table.get_item(Key={"zipcode": "19060", "ID": ad["ID"]})["Item"]["status"] == "removed"
    assert public_ads("19060")["Count"] == 0


@mock_aws
def test_refund_hides_ad(monkeypatch):
    table = make_ad_table()
    ad = body(create_paid_ad("eve"))
    stub_transactions(monkeypatch, {"good": tx("eve")})
    buy("eve", ad, "good")
    notify(monkeypatch, "REFUND", tx("eve", revoked=True))
    assert table.get_item(Key={"zipcode": "19060", "ID": ad["ID"]})["Item"]["status"] == "expired"


@mock_aws
def test_reminder_five_days_ahead_once(monkeypatch):
    table = make_ad_table()
    ses = boto3.client("ses", region_name="us-east-1")
    ses.verify_domain_identity(Domain="eabu.net")
    ses.verify_email_identity(EmailAddress="eve@example.com")
    base = {"paid": True, "status": "active", "email": "eve@example.com", "name": "Eve", "title": "Bakery"}
    table.put_item(Item={**base, "zipcode": "1", "ID": 1, "paidThrough": NOW + 4 * DAY, "autoRenew": True})   # due
    table.put_item(Item={**base, "zipcode": "1", "ID": 2, "paidThrough": NOW + 9 * DAY, "autoRenew": True})   # too early
    table.put_item(Item={**base, "zipcode": "1", "ID": 3, "paidThrough": NOW + 3 * DAY, "autoRenew": False})  # cancelled
    table.put_item(Item={**base, "zipcode": "1", "ID": 4, "paidThrough": NOW - 3 * DAY, "autoRenew": True})   # lapsed
    table.put_item(Item={"zipcode": "1", "ID": 5, "status": "active", "title": "free ad"})                     # free

    fn = load("eabu-ad-reminder")
    assert fn.lambda_handler({}, None, now=NOW) == {"checked": 4, "reminded": 1, "hidden": 1, "failed": 0}
    assert int(table.get_item(Key={"zipcode": "1", "ID": 1})["Item"]["reminderSentFor"]) == NOW + 4 * DAY
    assert table.get_item(Key={"zipcode": "1", "ID": 4})["Item"]["status"] == "expired"
    # The next day's run doesn't email again for the same renewal.
    assert fn.lambda_handler({}, None, now=NOW + DAY)["reminded"] == 0


@pytest.mark.parametrize("app_id", ["", "1234567890"])
def test_real_verifier_rejects_unsigned_data(monkeypatch, app_id):
    monkeypatch.setenv("APP_APPLE_ID", app_id)
    monkeypatch.setattr(apple_iap, "_verifiers", {})
    with pytest.raises(apple_iap.PurchaseError):
        apple_iap.verify_transaction("eyJhbGciOiJFUzI1NiJ9.e30.c2ln")
    with pytest.raises(apple_iap.PurchaseError):
        apple_iap.verify_notification("not-a-jws")


def test_account_token_is_stable_uuid():
    assert apple_iap.account_token("eve") == apple_iap.account_token("eve")
    assert apple_iap.account_token("eve") != apple_iap.account_token("mallory")
    assert len(apple_iap.account_token("eve")) == 36
