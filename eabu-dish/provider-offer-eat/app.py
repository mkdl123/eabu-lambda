
import os
import json
import base64
import logging
from datetime import datetime, timezone
from my_utils import helpers
import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REQUIRED_FIELDS = [
"itemId",
"userId",
"pUserId",
"title",
"description",
"offerDescription",
"phoneNumber",
"email",
"price",
"zip",
"latitude",
"longitude"
]

# Resolve environment configuration
table_name = os.environ.get("EAT_OFFER_TABLE_NAME", "eabu-eat-offer")
region = os.environ.get("REGION_NAME", "us-east-1")
# Initialize AWS resources (reuse across invocations if desired)
dynamodb = boto3.resource("dynamodb", region_name=region)
table = dynamodb.Table(table_name)
consumer_table = dynamodb.Table(os.environ.get("EAT_CONSUMER_TABLE_NAME", "eabu-eat-consumer"))


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

    # Only the signed-in user may act as themselves.
    denied = helpers.authorize_user(event, payload.get("pUserId"))
    if denied:
        return denied

    # An offer must answer a real request from that consumer.
    try:
        item_id = int(payload["itemId"])
        if not helpers.user_has_item(consumer_table, str(payload["userId"]), item_id):
            return helpers._response(404, {"msg": "Request not found"})
        # A seller may make only one offer per request.
        if helpers.already_offered(table, str(payload["userId"]), item_id, str(payload["pUserId"])):
            return helpers._response(409, {"msg": "You have already made an offer on this request.",
                                           "code": "ALREADY_OFFERED"})
    except (TypeError, ValueError):
        return helpers._response(400, {"msg": "itemId must be an integer"})
    except (BotoCoreError, ClientError):
        logger.exception("Failed to check the consumer request")
        return helpers._response(500, {"msg": "Failed to create offer"})

    partition_key = 100  # Replace 'your_partition_key'
    new_counter_value = helpers.increment_counter(partition_key, table)
    item = {
        "ID": int(new_counter_value),
        "zipcode": str(payload["zip"]),
        "itemId": item_id,
        "userId": payload["userId"],
        "pUserId": payload["pUserId"],
        "title":payload["title"],
        "itemDescription":payload["description"],
        "offerDescription":payload["offerDescription"],
        "offererPhoneNumber":payload["phoneNumber"],
        "offererEmail":payload["email"],
        "price":payload["price"],
        "latitude": str(payload["latitude"]),
        "longitude": str(payload["longitude"]),
        "createdAt": str(datetime.now().isoformat())
    }
    # What one unit of the price covers ("1 hr", "Everything"...). Older app builds don't send it.
    unit = str(payload.get("unit") or "").strip()
    if unit:
        item["unit"] = unit


    try:
        table.put_item(Item=item)
    except (BotoCoreError, ClientError) as e:
        logger.exception("Failed to write item to DynamoDB")
        return helpers._response(500, {"msg": "Failed to create dish"})

    return helpers._response(200, {"message": "Recipe posted ", "Item ID": item["itemId"]})