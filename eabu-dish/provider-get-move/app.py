"""Sellers browse buyers' transport requests in a ZIP code (userId "NA"), or a buyer reads their own,
from the last 30 days."""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key

from my_utils import helpers

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Created once per container and reused by every request it serves.
table = boto3.resource("dynamodb", region_name=os.environ.get("REGION_NAME", "us-east-1")).Table(
    os.environ.get("MOVE_CONSUMER_TABLE_NAME", "eabu-move-consumer"))


def decimal_serializer(obj):
    if isinstance(obj, Decimal):
        return str(obj)  # numbers go out as strings, which the app expects
    raise TypeError("Object of type {} is not JSON serializable".format(type(obj).__name__))


@helpers.cors
def lambda_handler(event, context):
    zip_code = event['pathParameters']['zip']
    userid = event['pathParameters']['userId']

    # Browsing a zipcode needs sign-in; a specific user's list is only for that user.
    denied = helpers.require_authenticated(event) if userid == "NA" else helpers.authorize_user(event, userid)
    if denied:
        return denied

    # last 30 days including today (UTC) -> now - 29 days
    start_created_at = (datetime.now(timezone.utc) - timedelta(days=29)).isoformat()

    try:
        if userid == "NA":
            result = helpers.query_all(
                table,
                KeyConditionExpression="zipcode = :zip_code AND ID > :ID",
                FilterExpression=Attr("createdAt").gte(start_created_at),
                ExpressionAttributeValues={":zip_code": zip_code, ":ID": 0},
            )
            # Other people's requests: show the area, not the exact address.
            helpers.coarsen_location(result["Items"])
        else:
            result = helpers.query_all(
                table,
                IndexName="userId-index",
                KeyConditionExpression=Key("userId").eq(userid),
                FilterExpression=Attr("createdAt").gte(start_created_at),
            )
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
