"""Extra posts bought with Apple in-app purchase.

Sellers get helpers.MONTHLY_POST_LIMIT free posts a month in total across all categories
(reset on the 1st, US Eastern). Past that,
each post uses one extra post. Extra posts come in packs of helpers.POSTS_PER_PACK
(the $0.99 consumable), never expire and work in every category.

GET  /post-credits/{userId}
    The caller's extra-post balance, posts this month (per category and in total), the
    reset date, and the product id and appAccountToken the app needs to buy a pack.
POST /post-credits          {"signedTransaction"}
    The app sends the StoreKit transaction (a JWS signed by Apple) after buying a pack.
    It is verified and checked to belong to the caller, then the pack is added. The
    same transaction is only ever counted once, so the app can safely send it again.

Refunds arrive through eabu-apple-notifications, which takes the pack back.
"""
import logging
from datetime import datetime, timezone

from botocore.exceptions import BotoCoreError, ClientError

from my_utils import helpers
from my_utils import apple_iap

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MAX_JWS_LENGTH = 20000


def _balance_body(user_id, balance, **extra):
    return {"balance": balance, "postsPerPack": helpers.POSTS_PER_PACK,
            "freeLimit": helpers.free_post_limit(user_id), "resetsOn": helpers.free_posts_reset_on(),
            "productId": apple_iap.posts_product_id(),
            "appAccountToken": apple_iap.account_token(user_id), **extra}


def _get(event):
    user_id = (event.get("pathParameters") or {}).get("userId")
    denied = helpers.authorize_user(event, user_id)
    if denied:
        return denied
    used = helpers.monthly_post_counts(user_id)
    return helpers._response(200, _balance_body(user_id, helpers.post_credit_balance(user_id),
                                                used=used, usedTotal=sum(used.values())))


def credit_pack(user_id, tx):
    """Add one pack for this transaction unless it was already added. Returns True if added."""
    table = helpers.post_credits_table()
    client = table.meta.client
    try:
        client.transact_write_items(TransactItems=[
            {"Put": {
                "TableName": table.name,
                "Item": {"userId": f"tx#{tx['transactionId']}", "ownerUserId": str(user_id),
                         "posts": helpers.POSTS_PER_PACK, "environment": tx["environment"],
                         "creditedAt": datetime.now(timezone.utc).isoformat()},
                "ConditionExpression": "attribute_not_exists(userId)"}},
            {"Update": {
                "TableName": table.name,
                "Key": {"userId": str(user_id)},
                "UpdateExpression": "ADD balance :n, purchased :n",
                "ExpressionAttributeValues": {":n": helpers.POSTS_PER_PACK}}},
        ])
        return True
    except ClientError as e:
        reasons = e.response.get("CancellationReasons") or []
        if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
            return False   # this transaction was credited before
        raise


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
        apple_iap.check_posts_purchase(tx, caller)
    except apple_iap.PurchaseError as e:
        logger.warning("Posts purchase rejected: %s", e)
        return helpers._response(403, {"msg": "This purchase could not be verified"})

    added = credit_pack(caller, tx)
    balance = helpers.post_credit_balance(caller)
    logger.info("Posts pack %s (%s): %s", tx["transactionId"], tx["environment"],
                "added" if added else "already added")
    return helpers._response(200, _balance_body(caller, balance, added=added))


@helpers.cors
def lambda_handler(event, context):
    method = (event.get("httpMethod") or "").upper()
    try:
        if method == "GET":
            return _get(event)
        if method == "POST":
            return _purchase(event)
    except (BotoCoreError, ClientError):
        logger.exception("Extra posts request failed")
        return helpers._response(500, {"msg": "Please try again in a moment."})
    return helpers._response(405, {"msg": "Method Not Allowed"})
