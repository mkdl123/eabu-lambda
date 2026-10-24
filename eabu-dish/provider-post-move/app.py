
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

REQUIRED_FIELDS = ["zipcode", "title", "description", "phone"]


# Resolve environment configuration
table_name = os.environ.get("MOVE_PROVIDER_TABLE_NAME", "eabu-move-provider")
region = os.environ.get("REGION_NAME", "us-east-1")
# Initialize AWS resources (reuse across invocations if desired)
dynamodb = boto3.resource("dynamodb", region_name=region)
table = dynamodb.Table(table_name)


def lambda_handler(event, context):
    logger.info("version=%s, top_keys=%s", event.get("version"), list(event.keys()))

    #Get new DishID
    partition_key = 100  # Replace 'your_partition_key'
    new_counter_value = helpers.increment_counter(partition_key, table)
    print(f'New counter value: {new_counter_value}')

    # Ensure method and body are valid
    if event.get("httpMethod") != "POST":
        return helpers._response(400, {"msg": "Bad Request: only POST is supported"})

    try:
        payload = helpers._parse_body(event)
        helpers._validate_payload(payload, REQUIRED_FIELDS)
    except ValueError as ve:
        logger.warning("Validation error: %s", ve)
        return helpers._response(400, {"msg": str(ve)})
    logger.info("userId=%s", payload["userId"] if isinstance(payload["userId"], str) else str(payload["userId"]));
    item = {
        "ID": int(new_counter_value),
        "zipcode": str(payload["zipcode"]),
        "userId": payload["userId"] if isinstance(payload["userId"], str) else str(payload["userId"]),
        "title": payload["title"],
        "description": payload["description"],
        "interested_by": 0,
        "phone": payload["phone"],
        "latitude": str(payload["latitude"]),
        "longitude": str(payload["longitude"]),
        "createdAt": str(datetime.now().isoformat())
    }
    # item = {
    #     "recipeid": int(new_counter_value),
    #     "zipcode": payload["zipcode"],
    #     "title": payload["title"],
    #     "description": payload["description"],
    #     "createdAt": str(datetime.now().isoformat())
    # }

    try:
        table.put_item(Item=item)

    except (BotoCoreError, ClientError) as e:
        logger.exception("Failed to write item to DynamoDB")
        return helpers._response(500, {"msg": "Failed to create dish" + "Table Name: " + table_name + " Region: " + region})

    return helpers._response(200, {"message": "Recipe posted ", "ID": item["ID"]})