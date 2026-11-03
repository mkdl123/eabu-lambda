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

REQUIRED_FIELDS = ["zipcode", "ID", "totalstars", "ratingscount", "providerType", "cUserId"]

# Resolve environment configuration for each provider type
EAT_PROVIDER_TABLE_NAME = os.environ.get("EAT_PROVIDER_TABLE_NAME", "eabu-eat-provider")
LIVE_PROVIDER_TABLE_NAME = os.environ.get("LIVE_PROVIDER_TABLE_NAME", "eabu-live-provider")
MOVE_PROVIDER_TABLE_NAME = os.environ.get("MOVE_PROVIDER_TABLE_NAME", "eabu-move-provider")

# New: rated tables
EAT_RATED_TABLE_NAME = os.environ.get("EAT_RATED_TABLE_NAME", "eabu-eat-rated")
LIVE_RATED_TABLE_NAME = os.environ.get("LIVE_RATED_TABLE_NAME", "eabu-live-rated")
MOVE_RATED_TABLE_NAME = os.environ.get("MOVE_RATED_TABLE_NAME", "eabu-move-rated")

REGION_NAME = os.environ.get("REGION_NAME", "us-east-1")

# Initialize AWS resources
dynamodb = boto3.resource("dynamodb", region_name=REGION_NAME)
eat_table = dynamodb.Table(EAT_PROVIDER_TABLE_NAME)
live_table = dynamodb.Table(LIVE_PROVIDER_TABLE_NAME)
move_table = dynamodb.Table(MOVE_PROVIDER_TABLE_NAME)

eat_rated_table = dynamodb.Table(EAT_RATED_TABLE_NAME)
live_rated_table = dynamodb.Table(LIVE_RATED_TABLE_NAME)
move_rated_table = dynamodb.Table(MOVE_RATED_TABLE_NAME)

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


def _get_rated_table_for_provider(provider_type: str):
    """Return (rated_table, rated_table_name) for a given providerType."""
    normalized = (provider_type or "").strip().lower()
    if normalized == "live":
        return live_rated_table, LIVE_RATED_TABLE_NAME
    if normalized == "move":
        return move_rated_table, MOVE_RATED_TABLE_NAME
    # default / "eat"
    return eat_rated_table, EAT_RATED_TABLE_NAME


def _ensure_rated_item_exists(provider_type: str, ID: int, cUserId: str):
    """
    For the given providerType, ensure a row exists in the corresponding *-rated table
    with the composite key (ID, cUserId). If it doesn't exist, insert one.
    """
    rated_table, rated_table_name = _get_rated_table_for_provider(provider_type)

    try:
        # Try to insert a new item only if it does not exist yet
        rated_table.put_item(
            Item={
                "ID": ID,
                "cUserId": cUserId,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            },
            ConditionExpression="attribute_not_exists(ID) AND attribute_not_exists(cUserId)",
        )
        logger.info(
            "Inserted new rated item in %s for ID=%s, cUserId=%s",
            rated_table_name,
            ID,
            cUserId,
        )
        return True  # Indicate that a new item was created
    except rated_table.meta.client.exceptions.ConditionalCheckFailedException:
        # Item already exists; nothing to do
        logger.info(
            "Rated item already exists in %s for ID=%s, cUserId=%s",
            rated_table_name,
            ID,
            cUserId,
        )
        return False  # Indicate that the item already existed
    except (BotoCoreError, ClientError):
        logger.exception("Failed to ensure rated item exists in %s", rated_table_name)
        raise


def _remove_rated_item(provider_type: str, ID: int, cUserId: str):
    """Roll back the 'already rated' marker when the provider update did not apply."""
    rated_table, rated_table_name = _get_rated_table_for_provider(provider_type)
    try:
        rated_table.delete_item(Key={"ID": ID, "cUserId": cUserId})
    except (BotoCoreError, ClientError):
        logger.exception("Failed to roll back rated item in %s", rated_table_name)


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

    # Only the signed-in user may act as themselves.
    denied = helpers.authorize_user(event, payload.get("cUserId"))
    if denied:
        return denied

    provider_type = payload.get("providerType", "eat")
    table, table_name = _get_table_for_provider(provider_type)

    try:
        zipcode = str(payload["zipcode"])
        ID = int(payload["ID"])
        totalstars = int(payload["totalstars"])
        ratingscount = int(payload["ratingscount"])
        cUserId = str(payload["cUserId"])
    except (TypeError, ValueError):
        return helpers._response(400, {"msg": "Invalid rating payload"})

    # A single rating adds exactly 1 to the count and 1..5 stars to the total.
    if ratingscount < 1 or totalstars < 1 or totalstars > 5 * ratingscount:
        return helpers._response(400, {"msg": "Invalid rating values"})

    # 1) Ensure row exists in appropriate *-rated table
    try:
        created = _ensure_rated_item_exists(provider_type, ID, cUserId)
    except Exception:
        return helpers._response(
            500,
            {
                "msg": "Failed to update rated table "
                },
        )

    # If the row already existed, stop and tell the caller
    if not created:
        return helpers._response(
            200,
            {
                "message": "You already rated this item",
                "providerType": provider_type,
            }
        )

    # 2) Update the provider table rating. Only accept exactly one new 1-5 star
    #    vote on top of the stored values; reject arbitrary overwrites of the totals.
    if ratingscount == 1:
        condition = (
            "attribute_exists(zipcode) AND attribute_exists(ID) "
            "AND (attribute_not_exists(ratingscount) OR ratingscount = :prev_rc) "
            "AND (attribute_not_exists(totalstars) OR (totalstars >= :min_prev_ts AND totalstars <= :max_prev_ts))"
        )
    else:
        condition = (
            "attribute_exists(zipcode) AND attribute_exists(ID) "
            "AND ratingscount = :prev_rc "
            "AND totalstars >= :min_prev_ts AND totalstars <= :max_prev_ts"
        )
    try:
        response = table.update_item(
            Key={
                "zipcode": zipcode,
                "ID": ID
            },
            UpdateExpression="SET totalstars = :ts, ratingscount = :rc",
            ExpressionAttributeValues={
                ":ts": totalstars,
                ":rc": ratingscount,
                ":prev_rc": ratingscount - 1,
                ":min_prev_ts": totalstars - 5,
                ":max_prev_ts": totalstars - 1,
            },
            ConditionExpression=condition,
            ReturnValues="UPDATED_NEW",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        logger.warning(
            "Rating rejected for %s: zipcode=%s, ID=%s, ts=%s, rc=%s",
            table_name, zipcode, ID, totalstars, ratingscount,
        )
        _remove_rated_item(provider_type, ID, cUserId)
        return helpers._response(409, {"msg": "Rating could not be applied; refresh and try again"})
    except (BotoCoreError, ClientError):
        logger.exception("Failed to update item in DynamoDB")
        _remove_rated_item(provider_type, ID, cUserId)
        return helpers._response(500, {"msg": "Failed to update provider rating"})

    return helpers._response(
        200,
        {
            "message": "Rating updated",
            "providerType": provider_type,
        }
    )