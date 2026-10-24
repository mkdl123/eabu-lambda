import json
import logging
import os
from decimal import Decimal

import boto3
from datetime import datetime, timedelta, timezone
from boto3.dynamodb.conditions import Attr

from boto3.dynamodb.conditions import Key

# import requests

logger = logging.getLogger()


dynamodb = boto3.resource('dynamodb')
table1 = dynamodb.Table('eabu-eat-provider') # Replace 'YourTableName'


def decimal_serializer(obj):
    if isinstance(obj, Decimal):
        return str(obj)  # Convert Decimal to string
    raise TypeError("Object of type {} is not JSON serializable".format(type(obj).__name__))


def lambda_handler(event, context):
    logger.info('Received event: %s', json.dumps(event, indent=2))
    current_timestamp = datetime.now().isoformat()

    print(current_timestamp)

    table_name = os.environ.get("EAT_PROVIDER_TABLE_NAME", "eabu-eat-provider")
    region = os.environ.get('REGION_NAME', 'us-east-1')

    print(table_name)
    print(region)

    post_table = boto3.resource(
        'dynamodb',
        region_name=region
    )

    table = post_table.Table(table_name)

    #id = event['pathParameters']['id']
    userId = event['pathParameters']['userId']
    print("Parameter userId: ",userId);

    # last 30 days cutoff (epoch seconds, UTC)
    cutoff_epoch = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
    cutoff_iso = datetime.fromtimestamp(cutoff_epoch, tz=timezone.utc).isoformat()
    # e.g. "2026-02-15T00:01:12+00:00"

    try:

        response = table.query(
            IndexName="userId-index",
            KeyConditionExpression=Key("userId").eq(userId),
            FilterExpression=Attr("createdAt").gte(cutoff_iso),
        )

        return {
            'statusCode': 200,
            'body': json.dumps(response, default=decimal_serializer)
        }


    except Exception as e:
        print("There was an error: ", e)

        return {
            'statusCode': 500,
            'body': 'Error retrieving item'
        }