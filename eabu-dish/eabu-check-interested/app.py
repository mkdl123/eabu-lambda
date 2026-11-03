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

REQUIRED_FIELDS = ["cUserId", "itemId","providerType"]

# Resolve environment configuration for each provider type


# New: interested tables
EAT_INTERESTED_TABLE_NAME = os.environ.get("EAT_INTERESTED_TABLE_NAME", "eabu-eat-interested")
LIVE_INTERESTED_TABLE_NAME = os.environ.get("LIVE_INTERESTED_TABLE_NAME", "eabu-live-interested")
MOVE_INTERESTED_TABLE_NAME = os.environ.get("MOVE_INTERESTED_TABLE_NAME", "eabu-move-interested")

REGION_NAME = os.environ.get("REGION_NAME", "us-east-1")

# Initialize AWS resources
dynamodb = boto3.resource("dynamodb", region_name=REGION_NAME)


eat_interested_table = dynamodb.Table(EAT_INTERESTED_TABLE_NAME)
live_interested_table = dynamodb.Table(LIVE_INTERESTED_TABLE_NAME)
move_interested_table = dynamodb.Table(MOVE_INTERESTED_TABLE_NAME)

lambda_client = boto3.client("lambda", region_name=REGION_NAME)




def _get_interested_table_for_provider(provider_type: str):
    normalized = (provider_type or "").strip().lower()
    if normalized == "live":
        return live_interested_table
    if normalized == "move":
        return move_interested_table
    # default / "eat"
    return eat_interested_table


def _ensure_interested_item_exists(provider_type: str, ID: int, cUserId: str):

    interested_table = _get_interested_table_for_provider(provider_type)

    try:
        # check if there is a row for itemId and userId, if there is a row, return true otherwise return false
        response = interested_table.get_item(
            Key={
                "ID": ID,
                "cUserId": cUserId
            }
        )
        
        if "Item" in response:
            return 1
        
        return 0
    except (BotoCoreError, ClientError):
        logger.exception("Failed to ensure interested item exists in %s", interested_table)
        raise



def lambda_handler(event, context):
    logger.info("version=%s, top_keys=%s", event.get("version"), list(event.keys()))

    try:
        # Extract parameters from path and body
        user_id = str(event['pathParameters']['cUserId'])
        denied = helpers.authorize_user(event, user_id)
        if denied:
            return denied
        item_id = int(event['pathParameters']['itemId'])
        provider_type = str(event['pathParameters']['providerType'])

    except (KeyError, ValueError, TypeError) as e:
        logger.warning("Validation error: %s", e)
        return helpers._response(400, {"msg": "Invalid path or body parameters"})

    # 1) Ensure row exists in appropriate *-interested table
    try:
        # Call the function and get the boolean result
        result = _ensure_interested_item_exists(provider_type, item_id, user_id)
        
        # Return true or false as part of the response body
        return helpers._response(200, {
            "interested": result,
            "providerType": provider_type,
            "message": "Interest processed"
        })

    except (BotoCoreError, ClientError):
        logger.exception("Failed to retrieve item in DynamoDB")
        return helpers._response(
            500,
            {
                "msg": "Failed to retrieve item"
            },
        )