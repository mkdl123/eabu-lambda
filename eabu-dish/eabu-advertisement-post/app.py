
import os
import re
import json
import base64
import logging
from urllib.parse import urlparse
from datetime import datetime, timezone
from my_utils import helpers
from my_utils.apple_iap import account_token as _account_token, AWAITING_STATUS as AWAITING_PAYMENT, PAID_STATUS as AD_LIVE
import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REQUIRED_FIELDS = [
"address",
"name",
"email",
"phone",
"zipcode",
"address",
"title",
"subtitle",
"description"
]

# New ad requests are never published directly: they are stored as pending and
# only appear in the app after an admin changes their status.
PENDING_STATUS = "pending"
MAX_LEN = {"name": 200, "email": 254, "phone": 32, "zipcode": 10, "address": 500, "country": 100,
           "title": 120, "subtitle": 200, "url": 2048, "description": 2000}


def _clean_url(value):
    """Allow only absolute https:// links (blocks javascript:, data:, http: etc.)."""
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ValueError("url must be a string")
    value = value.strip()
    if value and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
        value = "https://" + value  # people type "www.shop.com" or "shop.com"
    parsed = urlparse(value)
    if parsed.scheme.lower() != "https" or not parsed.netloc or any(c.isspace() for c in value):
        raise ValueError("url must be a valid https:// link")
    return value


# Resolve environment configuration
table_name = os.environ.get("AD_REQUEST_TABLE", "eabu-advertisement")
region = os.environ.get("REGION_NAME", "us-east-1")
# Initialize AWS resources (reuse across invocations if desired)
dynamodb = boto3.resource("dynamodb", region_name=region)
table = dynamodb.Table(table_name)


def lambda_handler(event, context):
    logger.info("version=%s, top_keys=%s", event.get("version"), list(event.keys()))

    #Get new DishID

    # Ensure method and body are valid
    if event.get("httpMethod") != "POST":
        return helpers._response(400, {"msg": "Bad Request: only POST is supported"})

    try:
        payload = helpers._parse_body(event)
        helpers._validate_payload(payload, REQUIRED_FIELDS)
    except ValueError as ve:
        logger.warning("Validation error: %s", ve)
        return helpers._response(400, {"msg": str(ve)})

    # Submitting an ad requires a signed-in user.
    denied = helpers.require_authenticated(event)
    if denied:
        return denied

    try:
        url = _clean_url(payload.get("url"))
        for field, limit in MAX_LEN.items():
            if len(str(payload.get(field) or "")) > limit:
                raise ValueError(f"{field} is too long (max {limit} characters)")
    except ValueError as ve:
        logger.warning("Rejected ad: %s", ve)
        return helpers._response(400, {"msg": str(ve)})

    caller = helpers.get_caller_id(event)
    paid = payload.get("paid") is True
    if paid:
        # A paid ad is tied to its owner, so the purchase can be matched to it.
        if caller is None:
            return helpers._response(401, {"msg": "Unauthorized"})
        try:
            mine = _paid_ads_of(caller)
        except (BotoCoreError, ClientError):
            logger.exception("Failed to look up the caller's paid ads")
            return helpers._response(500, {"msg": "Failed to create record"})
        now = int(datetime.now(timezone.utc).timestamp())
        if any(a.get("status") == AD_LIVE and int(a.get("paidThrough") or 0) > now for a in mine):
            return helpers._response(409, {"msg": "You already have an ad running"})

    sort_key_initial_value = 100  # Replace 'your_partition_key'
    new_counter_value = helpers.increment_counter_with(sort_key_initial_value, table, partitionkey='zipcode')
    item = {
        "ID": int(new_counter_value),
        "zipcode": str(payload["zipcode"]),
        "name": payload["name"],
        "email": payload["email"],
        "phone": payload["phone"],
        "status": PENDING_STATUS,  # never trust a client-supplied status
        "address": str(payload["address"]).replace("\n", " "),
        "country": str(payload.get("country") or ""),
        "title": payload["title"],
        "subtitle": payload["subtitle"],
        "url": url,
        "description": payload["description"],
        "createdAt": str(datetime.now().isoformat())
    }
    if caller:
        item["ownerUserId"] = caller
    if paid:
        # Shown everywhere once Apple confirms payment (eabu-ad-purchase); until then hidden.
        item.update(status=AWAITING_PAYMENT, paid=True, scope="national")

    try:
        table.put_item(Item=item)
        if paid:
            # Earlier ads this owner never paid for are dropped, so there's one ad per owner.
            for old in mine:
                if old.get("status") == AWAITING_PAYMENT:
                    table.delete_item(Key={"zipcode": old["zipcode"], "ID": old["ID"]})
    except (BotoCoreError, ClientError) as e:
        logger.exception("Failed to write item to DynamoDB")
        return helpers._response(500,
                                 {"msg": "Failed to create record"})

    if paid:
        return helpers._response(200, {
            "message": "Ad saved; complete the purchase to publish it", "ID": item["ID"],
            "zipcode": item["zipcode"], "status": AWAITING_PAYMENT,
            "productId": os.environ.get("AD_PRODUCT_ID", "net.eabu.app.ad.quarterly"),
            "appAccountToken": _account_token(caller)})
    return helpers._response(200, {"message": "Ad request received and pending review", "ID": item["ID"], "status": PENDING_STATUS})


def _paid_ads_of(user_id):
    """The owner's paid ads. A scan, fine while the ad table holds a few hundred rows."""
    kwargs = {"FilterExpression": Attr("ownerUserId").eq(user_id) & Attr("paid").eq(True)}
    found = []
    while True:
        res = table.scan(**kwargs)
        found += res.get("Items", [])
        if "LastEvaluatedKey" not in res:
            return found
        kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]