"""App Store Server Notifications V2 for paid ads: POST /apple-iap-notifications.

Apple calls this (no Cognito sign-in) whenever an ad subscription renews, fails to
renew, is cancelled, expires or is refunded. The body's signedPayload is verified
against Apple's root certificates before anything is changed; anything that fails
verification gets 400 and changes nothing.

Apple retries a notification until it gets a 200, so a verified notification about a
subscription we don't know still gets 200 (nothing to do), and a storage error gets
500 so Apple tries again later.
"""
import json
import logging
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import BotoCoreError, ClientError

from my_utils import apple_iap

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

table = boto3.resource("dynamodb", region_name=os.environ.get("REGION_NAME", "us-east-1")).Table(
    os.environ.get("AD_TABLE_NAME", "eabu-advertisement"))

# Notification types that end the ad now, and those that extend or keep it.
ENDS_AD = {"EXPIRED", "GRACE_PERIOD_EXPIRED", "REFUND", "REVOKE"}
EXTENDS_AD = {"SUBSCRIBED", "DID_RENEW", "OFFER_REDEEMED"}
MAX_BODY = 100_000


def _reply(status, msg):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"msg": msg})}


def _ad_for(original_transaction_id):
    kwargs = {"FilterExpression": Attr("originalTransactionId").eq(original_transaction_id)}
    while True:
        res = table.scan(**kwargs)
        if res.get("Items"):
            return res["Items"][0]
        if "LastEvaluatedKey" not in res:
            return None
        kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]


def apply_notification(note):
    """Update the ad a verified notification is about. Returns what was done."""
    tx, renewal, kind = note["transaction"], note["renewal"], note["type"]
    if note["type"] == "TEST":
        return "test"
    original_id = (tx or {}).get("originalTransactionId") or (renewal or {}).get("originalTransactionId")
    if not original_id:
        return "ignored"
    if tx and tx["productId"] != apple_iap.product_id():
        return "ignored"
    ad = _ad_for(original_id)
    if ad is None:
        return "unknown"

    sets, values = ["lastNotification = :n", "lastNotificationAt = :at"], {
        ":n": f"{kind}/{note.get('subtype') or ''}", ":at": datetime.now(timezone.utc).isoformat()}
    if renewal is not None:
        sets.append("autoRenew = :ar")
        values[":ar"] = renewal["autoRenew"]
    if kind in ENDS_AD:
        sets.append("#s = :st")
        values[":st"] = apple_iap.EXPIRED_STATUS
    elif kind in EXTENDS_AD and tx and not tx["revoked"]:
        # Never shorten: a late, older notification must not undo a newer renewal.
        if tx["expiresAt"] > int(ad.get("paidThrough") or 0):
            sets.append("paidThrough = :exp")
            values[":exp"] = tx["expiresAt"]
        if ad.get("status") != "removed":   # an ad an admin took down stays down
            sets.append("#s = :st")
            values[":st"] = apple_iap.PAID_STATUS
    names = {"#s": "status"} if ":st" in values else None
    kwargs = {"Key": {"zipcode": ad["zipcode"], "ID": ad["ID"]},
              "UpdateExpression": "SET " + ", ".join(sets), "ExpressionAttributeValues": values}
    if names:
        kwargs["ExpressionAttributeNames"] = names
    table.update_item(**kwargs)
    logger.info("Ad %s: %s", ad["ID"], values[":n"])
    return "updated"


def lambda_handler(event, context):
    if (event.get("httpMethod") or "").upper() != "POST":
        return _reply(405, "Method Not Allowed")
    raw = event.get("body") or ""
    if len(raw) > MAX_BODY:
        return _reply(400, "Bad Request")
    try:
        if event.get("isBase64Encoded"):
            import base64
            raw = base64.b64decode(raw).decode("utf-8")
        signed = json.loads(raw).get("signedPayload")
        if not isinstance(signed, str) or not signed:
            raise ValueError("no signedPayload")
        note = apple_iap.verify_notification(signed)
    except (ValueError, AttributeError, apple_iap.PurchaseError) as e:
        logger.warning("Rejected notification: %s", e)
        return _reply(400, "Bad Request")

    try:
        result = apply_notification(note)
    except (BotoCoreError, ClientError):
        logger.exception("Failed to store notification %s", note.get("uuid"))
        return _reply(500, "Try again")
    logger.info("Notification %s %s: %s", note["type"], note.get("uuid"), result)
    return _reply(200, "OK")
