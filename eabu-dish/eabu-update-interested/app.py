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

REQUIRED_FIELDS = ["zipcode", "ID", "interested", "providerType", "cUserId"]

# Resolve environment configuration for each provider type
EAT_PROVIDER_TABLE_NAME = os.environ.get("EAT_PROVIDER_TABLE_NAME", "eabu-eat-provider")
LIVE_PROVIDER_TABLE_NAME = os.environ.get("LIVE_PROVIDER_TABLE_NAME", "eabu-live-provider")
MOVE_PROVIDER_TABLE_NAME = os.environ.get("MOVE_PROVIDER_TABLE_NAME", "eabu-move-provider")

# New: interested tables
EAT_INTERESTED_TABLE_NAME = os.environ.get("EAT_INTERESTED_TABLE_NAME", "eabu-eat-interested")
LIVE_INTERESTED_TABLE_NAME = os.environ.get("LIVE_INTERESTED_TABLE_NAME", "eabu-live-interested")
MOVE_INTERESTED_TABLE_NAME = os.environ.get("MOVE_INTERESTED_TABLE_NAME", "eabu-move-interested")

REGION_NAME = os.environ.get("REGION_NAME", "us-east-1")

# Initialize AWS resources
dynamodb = boto3.resource("dynamodb", region_name=REGION_NAME)
eat_table = dynamodb.Table(EAT_PROVIDER_TABLE_NAME)
live_table = dynamodb.Table(LIVE_PROVIDER_TABLE_NAME)
move_table = dynamodb.Table(MOVE_PROVIDER_TABLE_NAME)

eat_interested_table = dynamodb.Table(EAT_INTERESTED_TABLE_NAME)
live_interested_table = dynamodb.Table(LIVE_INTERESTED_TABLE_NAME)
move_interested_table = dynamodb.Table(MOVE_INTERESTED_TABLE_NAME)

lambda_client = boto3.client("lambda", region_name=REGION_NAME)


def _get_table_for_provider(provider_type: str):
    """Return (table, table_name) for a given providerType."""
    normalized = (provider_type or "").strip().lower()
    if normalized == "live":
        return live_table, LIVE_PROVIDER_TABLE_NAME
    if normalized == "move":
        return move_table, MOVE_PROVIDER_TABLE_NAME
    # default / "eat"
    return eat_table, EAT_PROVIDER_TABLE_NAME


def _get_interested_table_for_provider(provider_type: str):
    normalized = (provider_type or "").strip().lower()
    if normalized == "live":
        return live_interested_table, LIVE_INTERESTED_TABLE_NAME
    if normalized == "move":
        return move_interested_table, MOVE_INTERESTED_TABLE_NAME
    # default / "eat"
    return eat_interested_table, EAT_INTERESTED_TABLE_NAME


def _ensure_interested_item_exists(provider_type: str, ID: int, cUserId: str):
    """
    For the given providerType, ensure a row exists in the corresponding *-interested table
    with the composite key (ID, cUserId). If it doesn't exist, insert one.
    """
    interested_table, interested_table_name = _get_interested_table_for_provider(provider_type)

    try:
        # Try to insert a new item only if it does not exist yet
        interested_table.put_item(
            Item={
                "ID": ID,
                "cUserId": cUserId,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            },
            ConditionExpression="attribute_not_exists(ID) AND attribute_not_exists(cUserId)",
        )
        logger.info(
            "Inserted new interested item in %s for ID=%s, cUserId=%s",
            interested_table_name,
            ID,
            cUserId,
        )
        return True  # Indicate that a new item was created
    except interested_table.meta.client.exceptions.ConditionalCheckFailedException:
        # Item already exists; nothing to do
        logger.info(
            "Interested item already exists in %s for ID=%s, cUserId=%s",
            interested_table_name,
            ID,
            cUserId,
        )
        return False  # Indicate that the item already existed
    except (BotoCoreError, ClientError):
        logger.exception("Failed to ensure interested item exists in %s", interested_table_name)
        raise


def lambda_handler(event, context):
    logger.info("version=%s, top_keys=%s", event.get("version"), list(event.keys()))

    # Ensure method and body are valid
    if event.get("httpMethod") != "POST":
        return helpers._response(400, {"msg": "Bad Request: only POST is supported"})

    try:
        payload = helpers._parse_body(event)
        helpers._validate_payload(payload, REQUIRED_FIELDS)
    except ValueError as ve:
        logger.warning("Validation error: %s", ve)
        return helpers._response(400, {"msg": str(ve)})

    provider_type = payload.get("providerType", "eat")
    table, table_name = _get_table_for_provider(provider_type)

    zipcode = str(payload["zipcode"])
    ID = int(payload["ID"])
    interested = int(payload["interested"])
    cUserId = str(payload["cUserId"])

    # 1) Ensure row exists in appropriate *-interested table
    try:
        created = _ensure_interested_item_exists(provider_type, ID, cUserId)
    except Exception:
        return helpers._response(
            500,
            {
                "msg": "Failed to update interested table "
                + " Interested Table for providerType: " + provider_type
                + " Region: " + REGION_NAME
            },
        )

    # If the row already existed, stop and tell the caller
    if not created:
        return helpers._response(
            200,
            {
                "message": "You already showed interest for this item",
                "providerType": provider_type,
            }
        )

    # 2) Update the provider table rating as before
    try:
        response = table.update_item(
            Key={
                "zipcode": zipcode,
                "ID": ID
            },
            UpdateExpression="SET interested_by = :is",
            ExpressionAttributeValues={
                ":is": interested,
            },
            ConditionExpression="attribute_exists(zipcode) AND attribute_exists(ID)",
            ReturnValues="UPDATED_NEW",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        logger.warning(
            "Item not found in %s: zipcode=%s, ID=%s",
            table_name,
            zipcode,
            ID
        )
        return helpers._response(404, {"msg": f"Item not found in {table_name}"})
    except (BotoCoreError, ClientError):
        logger.exception("Failed to update item in DynamoDB")
        return helpers._response(
            500,
            {
                "msg": "Failed to update provider rating"
                       + " Table Name: " + table_name
                       + " Region: " + REGION_NAME
            },
        )

    return helpers._response(
        200,
        {
            "message": "Rating updated",
            "providerType": provider_type,
            "table": table_name,
        }
    )