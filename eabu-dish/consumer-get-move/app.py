import json
import logging
import os
from decimal import Decimal

import boto3
from datetime import datetime, timedelta, timezone
from boto3.dynamodb.conditions import Attr, Key

from boto3.dynamodb.conditions import Key

# import requests

logger = logging.getLogger()


dynamodb = boto3.resource('dynamodb')
table1 = dynamodb.Table('eabu-move') # Replace 'YourTableName'


def decimal_serializer(obj):
    if isinstance(obj, Decimal):
        return str(obj)  # Convert Decimal to string
    raise TypeError("Object of type {} is not JSON serializable".format(type(obj).__name__))


def lambda_handler(event, context):
    current_timestamp = datetime.now().isoformat()

    print(current_timestamp)

    table_name = os.environ.get('MOVE_PROVIDER_TABLE_NAME', 'eabu-move')
    region = os.environ.get('REGION_NAME', 'us-east-1')

    print(table_name)
    print(region)

    post_table = boto3.resource(
        'dynamodb',
        region_name=region
    )

    table = post_table.Table(table_name)

    #id = event['pathParameters']['id']
    zip_code = event['pathParameters']['zip']
    #print("Parameter id: ",id);
    print("Parameter zip_code: ",zip_code);

    # last 30 days including today (UTC) -> now - 29 days
    start_created_at = (datetime.now(timezone.utc) - timedelta(days=29)).isoformat()

    try:
        response = table.query(
            KeyConditionExpression =  "zipcode = :zip_code AND ID > :ID",
            FilterExpression=Attr("createdAt").gte(start_created_at),
            ExpressionAttributeValues = {':zip_code': zip_code,
                                         ":ID": 0}
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





