"""A seller's transport listings from the last 30 days: full for the owner, public fields for others."""
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
    os.environ.get("MOVE_PROVIDER_TABLE_NAME", "eabu-move-provider"))


def decimal_serializer(obj):
    if isinstance(obj, Decimal):
        return str(obj)  # numbers go out as strings, which the app expects
    raise TypeError("Object of type {} is not JSON serializable".format(type(obj).__name__))


@helpers.cors
def lambda_handler(event, context):
    userId = event['pathParameters']['userId']

    # The owner gets their full listings; anyone else signed in (e.g. a buyer
    # viewing this seller's profile) gets the public fields only.
    denied = helpers.require_authenticated(event)
    if denied:
        return denied
    is_owner = helpers.get_caller_id(event) == str(userId)

    # last 30 days cutoff (UTC, whole seconds), e.g. "2026-02-15T00:01:12+00:00"
    cutoff_epoch = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
    cutoff_iso = datetime.fromtimestamp(cutoff_epoch, tz=timezone.utc).isoformat()

    try:
        result = helpers.query_all(
            table,
            IndexName="userId-index",
            KeyConditionExpression=Key("userId").eq(userId),
            FilterExpression=Attr("createdAt").gte(cutoff_iso),
        )
        helpers.attach_picture_urls(result["Items"], "move")
        if not is_owner:
            result["Items"] = helpers.public_listings(result["Items"])
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
