
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

REQUIRED_FIELDS = ["zipcode", "title", "description","phone"]


# Resolve environment configuration
table_name = os.environ.get("EAT_PROVIDER_TABLE_NAME", "eabu-eat-provider")
followers_table_name = os.environ.get("FOLLOWERS_TABLE_NAME", "eabu-followers")
region = os.environ.get("REGION_NAME", "us-east-1")
# notification_function_arn = os.environ.get(
#     "NOTIFICATION_FUNCTION_ARN",
#     "arn:aws:lambda:us-east-1:YOUR_ACCOUNT_ID:function:eabulambda-EabuNotificationFunction-E3SG0yF62GO4"
# )

# Initialize AWS resources (reuse across invocations if desired)
dynamodb = boto3.resource("dynamodb", region_name=region)
table = dynamodb.Table(table_name)
followers_table = dynamodb.Table(followers_table_name)
#ses = boto3.client('ses', region_name=region)
lambda_client = boto3.client('lambda', region_name=region)


def lambda_handler(event, context):
    logger.info("version=%s, top_keys=%s", event.get("version"), list(event.keys()))
    # print("Notification Function ARN: ", notification_function_arn)
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
        "interested_by" : 0,
        "phone": payload["phone"],
        "latitude": str(payload["latitude"]),
        "longitude": str(payload["longitude"]),
        "totalstars":0,
        "ratingscount":0,
        "createdAt": str(datetime.now().isoformat())
    }


    try:
        table.put_item(Item=item)



    except (BotoCoreError, ClientError) as e:
        logger.exception("Failed to write item to DynamoDB")
        return helpers._response(500, {"msg": "Failed to create dish" + "Table Name: " + table_name + " Region: " + region})

    return helpers._response(200, {"message": "Recipe posted ", "ID": item["ID"]})