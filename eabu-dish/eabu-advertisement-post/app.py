
import os
import json
import base64
import logging
from urllib.parse import urlparse
from datetime import datetime, timezone
from my_utils import helpers
import boto3
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

    try:
        url = _clean_url(payload.get("url"))
        for field, limit in MAX_LEN.items():
            if field != "url" and len(str(payload.get(field) or "")) > limit:
                raise ValueError(f"{field} is too long (max {limit} characters)")
    except ValueError as ve:
        return helpers._response(400, {"msg": str(ve)})

    sort_key_initial_value = 100  # Replace 'your_partition_key'
    new_counter_value = helpers.increment_counter_with(sort_key_initial_value, table, partitionkey='zipcode')
    print(f'New counter value: {new_counter_value}')
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


    try:
        table.put_item(Item=item)
    except (BotoCoreError, ClientError) as e:
        logger.exception("Failed to write item to DynamoDB")
        return helpers._response(500,
                                 {"msg": "Failed to create record"})

    return helpers._response(200, {"message": "Ad request received and pending review", "ID": item["ID"], "status": PENDING_STATUS})