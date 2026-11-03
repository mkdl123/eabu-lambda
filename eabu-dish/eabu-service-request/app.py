
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
"serviceType",
"email",
"phone",
"message",
"userId"
]

# Resolve environment configuration
table_name = os.environ.get("EABU_SERVICE_REQUEST_TABLE", "eabu-service-request")
region = os.environ.get("REGION_NAME", "us-east-1")
# Initialize AWS resources (reuse across invocations if desired)
dynamodb = boto3.resource("dynamodb", region_name=region)
table = dynamodb.Table(table_name)


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
    denied = helpers.authorize_user(event, payload.get("userId"))
    if denied:
        return denied


    item = {

        "userId": str(payload["userId"]),
        "serviceType": payload["serviceType"],
        "email": payload["email"],
        "phone": payload["phone"],
        "message": payload["message"],
        "createdAt": str(datetime.now().isoformat())
    }


    try:
        table.put_item(Item=item)
    except (BotoCoreError, ClientError) as e:
        logger.exception("Failed to write item to DynamoDB")
        return helpers._response(500,
                                 {"msg": "Failed to create record"})

    return helpers._response(200, {"message": "Message posted, we will contact you soon "})