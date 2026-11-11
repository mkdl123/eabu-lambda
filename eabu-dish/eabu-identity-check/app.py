import json
import logging
import os
from decimal import Decimal

from my_utils import helpers
import boto3
from boto3.dynamodb.conditions import Attr

from boto3.dynamodb.conditions import Key

# import requests

logger = logging.getLogger()


# Created once per container and reused by every request it serves.
table = boto3.resource("dynamodb", region_name=os.environ.get("REGION_NAME", "us-east-1")).Table(
    os.environ.get("EABU_IDENTITY_TABLE_NAME", "eabu-identity"))


def decimal_serializer(obj):
    if isinstance(obj, Decimal):
        return str(obj)  # Convert Decimal to string
    raise TypeError("Object of type {} is not JSON serializable".format(type(obj).__name__))


@helpers.cors
def lambda_handler(event, context):
    userId = event['pathParameters']['userId']

    denied = helpers.require_authenticated(event)
    if denied:
        return denied



    try:
        # response = table.query(
        #     IndexName='userId-index',
        #     KeyConditionExpression=Key('userId').eq(userId)
        # )

        response = table.query(
             KeyConditionExpression =  "userId = :userId",
             ExpressionAttributeValues = {':userId': userId }
        )

        if not helpers.is_owner(event, userId):
            # Other users may only learn whether this user is verified -
            # never their name, email, phone or address.
            public_fields = ("userId", "verified", "submitted")
            response = {
                "Items": [{k: i[k] for k in public_fields if k in i} for i in response.get("Items", [])],
                "Count": response.get("Count", 0),
            }

        return {
            'statusCode': 200,
            'body': json.dumps(helpers.query_result(response), default=decimal_serializer)
        }


    except Exception:
        logger.exception("Identity lookup failed")

        return {
            'statusCode': 500,
            'body': 'Error retrieving item'
        }





