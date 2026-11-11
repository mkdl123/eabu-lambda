"""Paid ads bought with Apple in-app purchase.

POST /eabu-ad-purchase        {"ID", "zipcode", "signedTransaction"}
    The app sends the StoreKit transaction (a JWS signed by Apple) after a purchase or
    a restore. It is verified, checked to belong to the caller, and the caller's ad
    goes live nationwide until the subscription's expiry date. Without ID/zipcode the
    caller's most recent paid ad is used (restore on a new phone).
GET  /eabu-ad-purchase/{userId}
    The caller's paid ad: status, title, paidThrough and autoRenew, for "My ad".

Renewals, cancellations and refunds arrive later through eabu-apple-notifications.
"""
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import BotoCoreError, ClientError

from my_utils import helpers
from my_utils import apple_iap

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

table = boto3.resource("dynamodb", region_name=os.environ.get("REGION_NAME", "us-east-1")).Table(
    os.environ.get("AD_TABLE_NAME", "eabu-advertisement"))

MY_AD_FIELDS = ("ID", "zipcode", "status", "title", "subtitle", "description", "url",
                "paidThrough", "autoRenew", "createdAt")
MAX_JWS_LENGTH = 20000
REMOVED_STATUS = "removed"   # set by an admin to take a paid ad down


def _json(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj == int(obj) else float(obj)
    raise TypeError(type(obj).__name__)


def _ok(body):
    return {"statusCode": 200, "body": json.dumps(body, default=_json)}


def _paid_ads_of(user_id):
    """The owner's paid ads, newest first (a scan: the ad table is small)."""
    kwargs = {"FilterExpression": Attr("ownerUserId").eq(user_id) & Attr("paid").eq(True)}
    found = []
    while True:
        res = table.scan(**kwargs)
        found += res.get("Items", [])
        if "LastEvaluatedKey" not in res:
            break
        kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]
    return sorted(found, key=lambda a: int(a["ID"]), reverse=True)


def _my_ad(event):
    user_id = (event.get("pathParameters") or {}).get("userId")
    denied = helpers.authorize_user(event, user_id)
    if denied:
        return denied
    ads = _paid_ads_of(str(user_id))
    live = [a for a in ads if a.get("status") == apple_iap.PAID_STATUS]
    ad = (live or ads or [None])[0]
    return _ok({"ad": {k: ad[k] for k in MY_AD_FIELDS if k in ad} if ad else None,
                "productId": apple_iap.product_id(),
                "appAccountToken": apple_iap.account_token(user_id)})


def _purchase(event):
    caller = helpers.get_caller_id(event)
    if caller is None:
        return helpers._response(401, {"msg": "Unauthorized"})
    try:
        payload = helpers._parse_body(event)
    except ValueError:
        return helpers._response(400, {"msg": "Invalid JSON"})
    signed = payload.get("signedTransaction")
    if not isinstance(signed, str) or not signed or len(signed) > MAX_JWS_LENGTH:
        return helpers._response(400, {"msg": "signedTransaction is required"})

    try:
        tx = apple_iap.verify_transaction(signed)
        apple_iap.check_ad_purchase(tx, caller)
    except apple_iap.PurchaseError as e:
        logger.warning("Ad purchase rejected: %s", e)
        return helpers._response(403, {"msg": "This purchase could not be verified"})

    ads = _paid_ads_of(caller)
    ad = None
    if payload.get("ID") is not None and payload.get("zipcode"):
        try:
            ad_id = int(payload["ID"])
        except (TypeError, ValueError):
            return helpers._response(400, {"msg": "ID must be a number"})
        ad = next((a for a in ads if int(a["ID"]) == ad_id and a["zipcode"] == str(payload["zipcode"])), None)
    else:
        # Restore: the ad this subscription already pays for, else the newest one.
        ad = next((a for a in ads if a.get("originalTransactionId") == tx["originalTransactionId"]), None) \
            or (ads[0] if ads else None)
    if ad is None:
        return helpers._response(404, {"msg": "Ad not found"})
    if ad.get("status") == REMOVED_STATUS:
        # Taken down by an admin: a purchase never puts it back up.
        return helpers._response(403, {"msg": "This ad was removed. Contact Eabu support."})

    # One subscription pays for one ad: move it off any other ad of this owner.
    for other in ads:
        if other is not ad and other.get("originalTransactionId") == tx["originalTransactionId"]:
            table.update_item(Key={"zipcode": other["zipcode"], "ID": other["ID"]},
                              UpdateExpression="SET #s = :e REMOVE originalTransactionId",
                              ExpressionAttributeNames={"#s": "status"},
                              ExpressionAttributeValues={":e": apple_iap.EXPIRED_STATUS})

    table.update_item(
        Key={"zipcode": ad["zipcode"], "ID": ad["ID"]},
        UpdateExpression="SET #s = :live, paidThrough = :exp, autoRenew = :t, "
                         "originalTransactionId = :otid, iapEnvironment = :env, activatedAt = :now",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":live": apple_iap.PAID_STATUS, ":exp": tx["expiresAt"], ":t": True,
            ":otid": tx["originalTransactionId"], ":env": tx["environment"],
            ":now": datetime.now(timezone.utc).isoformat()})
    logger.info("Ad %s live until %s (%s)", ad["ID"], tx["expiresAt"], tx["environment"])
    return _ok({"ID": ad["ID"], "zipcode": ad["zipcode"], "status": apple_iap.PAID_STATUS,
                "paidThrough": tx["expiresAt"]})


@helpers.cors
def lambda_handler(event, context):
    method = (event.get("httpMethod") or "").upper()
    try:
        if method == "GET":
            return _my_ad(event)
        if method == "POST":
            return _purchase(event)
    except (BotoCoreError, ClientError):
        logger.exception("Ad purchase storage failed")
        return helpers._response(500, {"msg": "Something went wrong, please try again"})
    return helpers._response(405, {"msg": "Method Not Allowed"})
