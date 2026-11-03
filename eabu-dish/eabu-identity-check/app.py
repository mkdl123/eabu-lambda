import json
import logging
import os
from decimal import Decimal

from my_utils import helpers
import boto3
from datetime import datetime
from boto3.dynamodb.conditions import Attr

from boto3.dynamodb.conditions import Key

# import requests

logger = logging.getLogger()


dynamodb = boto3.resource('dynamodb')
table1 = dynamodb.Table('eabu-identity') # Replace 'YourTableName'


def decimal_serializer(obj):
    if isinstance(obj, Decimal):
        return str(obj)  # Convert Decimal to string
    raise TypeError("Object of type {} is not JSON serializable".format(type(obj).__name__))


def lambda_handler(event, context):
    current_timestamp = datetime.now().isoformat()

    print(current_timestamp)

    table_name = os.environ.get('EABU_IDENTITY_TABLE_NAME', 'eabu-identity')
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
    #print("Parameter id: ",id);

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
            'body': json.dumps(response, default=decimal_serializer)
        }


    except Exception as e:
        print("There was an error: ", e)

        return {
            'statusCode': 500,
            'body': 'Error retrieving item'
        }





