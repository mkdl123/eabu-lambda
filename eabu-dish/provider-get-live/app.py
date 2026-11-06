import json
import logging
import os
from decimal import Decimal

from my_utils import helpers
import boto3
from datetime import datetime, timedelta, timezone
from boto3.dynamodb.conditions import Attr, Key



# import requests

logger = logging.getLogger()


dynamodb = boto3.resource('dynamodb')
table1 = dynamodb.Table('eabu-live-consumer') # Replace 'YourTableName'


def decimal_serializer(obj):
    if isinstance(obj, Decimal):
        return str(obj)  # Convert Decimal to string
    raise TypeError("Object of type {} is not JSON serializable".format(type(obj).__name__))


@helpers.cors
def lambda_handler(event, context):
    current_timestamp = datetime.now().isoformat()

    print(current_timestamp)

    table_name = os.environ.get("LIVE_CONSUMER_TABLE_NAME", "eabu-live-consumer")
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

    # id = event['pathParameters']['id']
    userid = event['pathParameters']['userId']

    # Browsing a zipcode needs sign-in; a specific user's list is only for that user.
    denied = helpers.require_authenticated(event) if userid == "NA" else helpers.authorize_user(event, userid)
    if denied:
        return denied
    # print("Parameter id: ",id);
    print("Parameter userid: ", userid);

    # last 30 days including today (UTC) -> now - 29 days
    start_created_at = (datetime.now(timezone.utc) - timedelta(days=29)).isoformat()

    try:

        if userid == "NA":
            response = table.query(
                KeyConditionExpression="zipcode = :zip_code AND ID > :ID",
                FilterExpression=Attr("createdAt").gte(start_created_at),
                ExpressionAttributeValues={':zip_code': zip_code,
                                           ":ID": 0}
            )
        else:
            response = table.query(
                IndexName='userId-index',
                FilterExpression=Attr("createdAt").gte(start_created_at),
                KeyConditionExpression=Key('userId').eq(userid)
            )

        if userid == "NA":
            # Other people's requests: show the area, not the exact address.
            helpers.coarsen_location(response.get("Items", []))

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





