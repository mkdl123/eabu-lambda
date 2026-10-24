import json
import logging
import os
from decimal import Decimal

import boto3
from datetime import datetime
from boto3.dynamodb.conditions import Attr

from boto3.dynamodb.conditions import Key

# import requests

logger = logging.getLogger()


dynamodb = boto3.resource('dynamodb')
table1 = dynamodb.Table('eabu-eat-offer') # Replace 'YourTableName'


def decimal_serializer(obj):
    if isinstance(obj, Decimal):
        return str(obj)  # Convert Decimal to string
    raise TypeError("Object of type {} is not JSON serializable".format(type(obj).__name__))


def lambda_handler(event, context):
    logger.info('Received event: %s', json.dumps(event, indent=2))
    current_timestamp = datetime.now().isoformat()

    print(current_timestamp)

    table_name = os.environ.get('EAT_OFFER_TABLE_NAME', 'eabu-eat-offer')
    region = os.environ.get('REGION_NAME', 'us-east-1')

    print(table_name)
    print(region)

    post_table = boto3.resource(
        'dynamodb',
        region_name=region
    )

    table = post_table.Table(table_name)

    #id = event['pathParameters']['id']
    userid = event['pathParameters']['userId']
    #print("Parameter id: ",id);
    print("Parameter userId: ",userid);


    itemid = event['pathParameters']['itemId']
    # print("Parameter id: ",id);
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
            'body': json.dumps(response, default=decimal_serializer)
        }


    except Exception as e:
        print("There was an error: ", e)

        return {
            'statusCode': 500,
            'body': 'Error retrieving item'
        }





