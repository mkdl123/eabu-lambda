
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

REQUIRED_FIELDS = ["zipcode", "title", "description", "phone", "userId", "latitude", "longitude"]


# Resolve environment configuration
table_name = os.environ.get("EAT_PROVIDER_TABLE_NAME", "eabu-eat-provider")
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

    # Only the signed-in user may act as themselves.
    denied = helpers.authorize_user(event, payload.get("userId"))
    if denied:
        return denied
    # At most helpers.MONTHLY_POST_LIMIT posts per category in 30 days (sellers can't delete posts).
    limited, spent_credit = helpers.post_allowance(table, payload["userId"])
    if limited:
        return limited
    logger.info("userId=%s", payload["userId"] if isinstance(payload["userId"], str) else str(payload["userId"]));
    partition_key = 100  # Replace 'your_partition_key'
    new_counter_value = helpers.increment_counter(partition_key, table)
    item = {
        "ID": int(new_counter_value),
        "zipcode": str(payload["zipcode"]),
        "userId": payload["userId"] if isinstance(payload["userId"], str) else str(payload["userId"]),
        "title": payload["title"],
        "description": payload["description"],
        "interested_by" : 0,
        "phone": payload["phone"],
        "latitude": str(payload["latitude"]),
        "longitude": str(payload["longitude"]),
        "totalstars":0,
        "ratingscount":0,
        "createdAt": str(datetime.now().isoformat()),
        "expiresAt": helpers.post_expires_at(),  # DynamoDB TTL deletes the post then
    }
    # Optional street address so buyers/sellers can find the place on a map.
    address = helpers.optional_address(payload)
    if address:
        item["address"] = address
        item.update(helpers.address_location(payload))  # pin for the Home map


    try:
        table.put_item(Item=item)



    except (BotoCoreError, ClientError) as e:
        logger.exception("Failed to write item to DynamoDB")
        if spent_credit:
            helpers.refund_post_credit(payload["userId"])
        return helpers._response(500, {"msg": "Failed to create dish"})

    return helpers._response(200, {"message": "Recipe posted ", "ID": item["ID"]})