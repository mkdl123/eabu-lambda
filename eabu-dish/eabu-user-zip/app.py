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

REQUIRED_FIELDS = ["zipcode", "userId"]

# Resolve environment configuration
table_name = os.environ.get("EABU_USER_TABLE_NAME", "eabu-users")
region = os.environ.get("REGION_NAME", "us-east-1")
dynamodb = boto3.resource("dynamodb", region_name=region)
table = dynamodb.Table(table_name)


def lambda_handler(event, context):
    logger.info("version=%s, top_keys=%s", event.get("version"), list(event.keys()))
    print(f"table name: {table_name}")

    if event.get("httpMethod") != "POST":
        return helpers._response(400, {"msg": "Bad Request: only POST is supported"})

    try:
        payload = helpers._parse_body(event)
        helpers._validate_payload(payload, REQUIRED_FIELDS)
    except ValueError as ve:
        logger.warning("Validation error: %s", ve)
        return helpers._response(400, {"msg": str(ve)})

    user_id = payload["userId"] if isinstance(payload["userId"], str) else str(payload["userId"])
    zipcode = str(payload["zipcode"])

    try:
        # If the row for this userId already exists, update only the zipcode.
        # If it doesn't exist, create it.
        table.update_item(
            Key={"userId": user_id},
            UpdateExpression="SET zipcode = :z",
            ExpressionAttributeValues={":z": zipcode},
            ReturnValues="UPDATED_NEW",
        )
    except (BotoCoreError, ClientError):
        logger.exception("Failed to insert or update user zipcode")
        return helpers._response(
            500,
            {
                "msg": "Failed to insert or update user zipcode"
                + " Table Name: "
                + table_name
                + " Region: "
                + region
            },
        )

    return helpers._response(200, {"message": "zipcode updated", "zip": zipcode})