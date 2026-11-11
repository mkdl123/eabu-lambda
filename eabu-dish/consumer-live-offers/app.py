"""Offers a buyer received on their household requests: all of them (itemId "NA") or for one request."""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

from my_utils import helpers

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Created once per container and reused by every request it serves.
table = boto3.resource("dynamodb", region_name=os.environ.get("REGION_NAME", "us-east-1")).Table(
    os.environ.get("LIVE_OFFER_TABLE_NAME", "eabu-live-offer"))


def decimal_serializer(obj):
    if isinstance(obj, Decimal):
        return str(obj)  # numbers go out as strings, which the app expects
    raise TypeError("Object of type {} is not JSON serializable".format(type(obj).__name__))


@helpers.cors
def lambda_handler(event, context):
    userid = event['pathParameters']['userId']

    denied = helpers.authorize_user(event, userid)
    if denied:
        return denied

    itemid = event['pathParameters']['itemId']

    try:
        if itemid == "NA":
            result = helpers.query_all(
                table,
                IndexName="userId-index",
                KeyConditionExpression=Key("userId").eq(userid),
            )
        else:
            result = helpers.query_all(
                table,
                IndexName="userId-itemId-index",
                KeyConditionExpression="userId = :u_id AND itemId = :val",
                ExpressionAttributeValues={":u_id": str(userid), ":val": int(itemid)},
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
