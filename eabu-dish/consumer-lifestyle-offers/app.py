import json
import logging
import os
from decimal import Decimal

from my_utils import helpers
import boto3
from datetime import datetime

from boto3.dynamodb.conditions import Key

logger = logging.getLogger()

def decimal_serializer(obj):
    if isinstance(obj, Decimal):
        return str(obj)  # Convert Decimal to string
    raise TypeError("Object of type {} is not JSON serializable".format(type(obj).__name__))

@helpers.cors
def lambda_handler(event, context):
    current_timestamp = datetime.now().isoformat()

    print(current_timestamp)

    table_name = os.environ.get('LIFESTYLE_OFFER_TABLE_NAME', 'eabu-lifestyle-offer')
    region = os.environ.get('REGION_NAME', 'us-east-1')

    print(table_name)
    print(region)

    post_table = boto3.resource(
        'dynamodb',
        region_name=region
    )

    table = post_table.Table(table_name)

    userid = event['pathParameters']['userId']

    denied = helpers.authorize_user(event, userid)
    if denied:
        return denied
    print("Parameter userId: ",userid);

    itemid = event['pathParameters']['itemId']
    print("Parameter itemId: ", itemid);

    try:
        if itemid == "NA":
            response = table.query(
                IndexName='userId-index',
                KeyConditionExpression=Key('userId').eq(userid)
            )
        else:
            numeric_id = int(itemid)
            response = table.query(
                IndexName='userId-itemId-index',
                KeyConditionExpression="userId = :u_id AND itemId = :val",
                ExpressionAttributeValues={
                    ':u_id': str(userid),
                    ':val': numeric_id
                }
            )

        return {
            'statusCode': 200,
            'body': json.dumps(helpers.query_result(response), default=decimal_serializer)
        }

    except Exception as e:
        print("There was an error: ", e)

        return {
            'statusCode': 500,
            'body': 'Error retrieving item'
        }
