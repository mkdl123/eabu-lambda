
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

# Resolve environment configuration
table_name = os.environ.get("AD_REQUEST_TABLE", "eabu-advertisement")
region = os.environ.get("REGION_NAME", "us-east-1")
# Initialize AWS resources (reuse across invocations if desired)
dynamodb = boto3.resource("dynamodb", region_name=region)
table = dynamodb.Table(table_name)


def lambda_handler(event, context):
    logger.info("version=%s, top_keys=%s", event.get("version"), list(event.keys()))

    #Get new DishID
    sort_key_initial_value = 100  # Replace 'your_partition_key'
    new_counter_value = helpers.increment_counter_with(sort_key_initial_value, table, partitionkey='zipcode')
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


    item = {
        "ID": int(new_counter_value),
        "zipcode": str(payload["zipcode"]),
        "name": payload["name"],
        "email": payload["email"],
        "phone": payload["phone"],
        "status": payload["status"],
        "address": str(payload["address"]).replace("\n", " "),
        "country": payload["country"],
        "title": payload["title"],
        "subtitle": payload["subtitle"],
        "url": payload["url"],
        "description": payload["description"],
        "createdAt": str(datetime.now().isoformat())
    }


    try:
        table.put_item(Item=item)
    except (BotoCoreError, ClientError) as e:
        logger.exception("Failed to write item to DynamoDB")
        return helpers._response(500,
                                 {"msg": "Failed to create record" + "Table Name: " + table_name + " Region: " + region})

    return helpers._response(200, {"message": "Message posted ", "ID": item["ID"]})