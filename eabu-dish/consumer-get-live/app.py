"""Buyers browse sellers' household listings in a ZIP code, posted in the last 30 days."""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr

from my_utils import helpers

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Created once per container and reused by every request it serves.
table = boto3.resource("dynamodb", region_name=os.environ.get("REGION_NAME", "us-east-1")).Table(
    os.environ.get("LIVE_PROVIDER_TABLE_NAME", "eabu-live-provider"))


def decimal_serializer(obj):
    if isinstance(obj, Decimal):
        return str(obj)  # numbers go out as strings, which the app expects
    raise TypeError("Object of type {} is not JSON serializable".format(type(obj).__name__))


@helpers.cors
def lambda_handler(event, context):
    zip_code = event['pathParameters']['zip']
    # last 30 days including today (UTC) -> now - 29 days
    start_created_at = (datetime.now(timezone.utc) - timedelta(days=29)).isoformat()

    try:
        result = helpers.query_all(
            table,
            KeyConditionExpression="zipcode = :zip_code AND ID > :ID",
            FilterExpression=Attr("createdAt").gte(start_created_at),
            ExpressionAttributeValues={":zip_code": zip_code, ":ID": 0},
        )
        helpers.attach_picture_urls(result["Items"], "live")
        return {
            'statusCode': 200,
            'body': json.dumps(result, default=decimal_serializer)
        }
    except Exception:
        logger.exception("Query failed")
        return {
            'statusCode': 500,
            'body': 'Error retrieving item'
        }
