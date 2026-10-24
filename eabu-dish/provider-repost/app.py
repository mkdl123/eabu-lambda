import os
import json
import base64
import logging
from datetime import datetime, timezone
from my_utils import helpers
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REQUIRED_FIELDS = ["itemId", "serviceCategory", "zipcode"]

EAT_PROVIDER_TABLE_NAME = "eabu-eat-provider"
LIVE_PROVIDER_TABLE_NAME = "eabu-live-provider"
MOVE_PROVIDER_TABLE_NAME = "eabu-move-provider"

# Existing index (if you still use it elsewhere)
ITEM_ID_INDEX_NAME = "itemId-index"
# New composite index to locate the item via (userId, itemId)

# Set these to your table's *actual* primary key attribute names.


def _resolve_table_name(service_category: str) -> str:
    if service_category == "Eat":
        return os.environ.get("EAT_PROVIDER_TABLE_NAME", EAT_PROVIDER_TABLE_NAME)
    if service_category == "Live":
        return os.environ.get("LIVE_PROVIDER_TABLE_NAME", LIVE_PROVIDER_TABLE_NAME)
    return os.environ.get("MOVE_PROVIDER_TABLE_NAME", MOVE_PROVIDER_TABLE_NAME)


region = os.environ.get("REGION_NAME", "us-east-1")
dynamodb = boto3.resource("dynamodb", region_name=region)


def lambda_handler(event, context):
    logger.info("version=%s, top_keys=%s", event.get("version"), list(event.keys()))

    if event.get("httpMethod") != "POST":
        return helpers._response(400, {"msg": "Bad Request: only POST is supported"})

    try:
        payload = helpers._parse_body(event)
        helpers._validate_payload(payload, REQUIRED_FIELDS)
    except ValueError as ve:
        logger.warning("Validation error: %s", ve)
        return helpers._response(400, {"msg": str(ve)})

    table_name = _resolve_table_name(payload.get("serviceCategory"))
    table = dynamodb.Table(table_name)

    # Ensure itemId is an integer (since your ID comparisons expect numeric)
    try:
        item_id = int(payload["itemId"])
    except (TypeError, ValueError):
        return helpers._response(400, {"msg": "itemId must be an integer"})

    zipcode = str(payload["zipcode"])
    created_at = datetime.now(timezone.utc).isoformat()

    try:
        # 1) Query by the (userId, itemId) GSI to locate the exact record.
        # Assumes index keys are: partition key = userId, sort key = itemId


        table.update_item(
            Key={
                "zipcode": zipcode,
                "ID": item_id
            },
            UpdateExpression="SET createdAt = :createdAt",
            ConditionExpression="attribute_exists(ID)",
            ExpressionAttributeValues={
                ":createdAt": created_at

            },
        )

    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return helpers._response(404, {"msg": f"No item found for itemId={item_id}"})
        logger.exception("Failed to update item in DynamoDB")
        return helpers._response(
            500,
            {"msg": "Failed to update item" + " Table Name: " + table_name + " Region: " + region},
        )
    except (BotoCoreError, ValueError):
        logger.exception("Failed to update item in DynamoDB")
        return helpers._response(
            500,
            {"msg": "Failed to update item" + " Table Name: " + table_name + " Region: " + region},
        )

    return helpers._response(200, {"message": "Item updated", "itemId": item_id,  "createdAt": created_at})