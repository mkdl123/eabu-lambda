import json
import logging
import os
from my_utils import helpers
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

# Initialize AWS resources
region = os.environ.get('REGION_NAME', 'us-east-1')
table_name = os.environ.get('EABU_IDENTITY_TABLE_NAME', 'eabu-identity')
dynamodb = boto3.resource("dynamodb", region_name=region)
table = dynamodb.Table(table_name)

@helpers.cors
def lambda_handler(event, context):
    param = str(event['pathParameters']['param'])

    denied = helpers.require_authenticated(event)
    if denied:
        return denied

    if not param or param == "NA":
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": "Parameter 'param' (email or phone) is required"})
        }

    # Logic: treat as phone if only digits, otherwise treat as email
    is_phone = param.isdigit()
    search_attr = 'phone' if is_phone else 'email'
    index_name = f"{search_attr}-index" # Expected GSIs: email-index, phone-index

    try:
        # Efficient lookup using Query on Global Secondary Index
        response = table.query(
            IndexName=index_name,
            KeyConditionExpression=Key(search_attr).eq(param)
        )
        # Callers may only look up their own record. Someone else's match gets the
        # same "User not found" as no match, so this can't reveal who is registered.
        items = [i for i in response.get('Items', []) if helpers.is_owner(event, i.get('userId'))]

        if items:
            # Return the userId found in the first matching record
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({
                    "userId": items[0].get('userId'),
                    "identifiedBy": search_attr
                })
            }
        else:
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"message": "User not found"})
            }

    except (BotoCoreError, ClientError):
        logger.exception("Database lookup failed")
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": "Database lookup failed"})
        }