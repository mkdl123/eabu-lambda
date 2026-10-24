import json
import os
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from boto3.dynamodb.conditions import Key

# Initialize AWS resources
region = os.environ.get('REGION_NAME', 'us-east-1')
table_name = os.environ.get('EABU_IDENTITY_TABLE_NAME', 'eabu-identity')
dynamodb = boto3.resource("dynamodb", region_name=region)
table = dynamodb.Table(table_name)

def lambda_handler(event, context):
    param = str(event['pathParameters']['param'])

    if not param or param == "NA":
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": "Parameter 'param' (email or phone) is required"})
        }

    # Logic: treat as phone if only digits, otherwise treat as email
    is_phone = param.isdigit()
    search_attr = 'phone' if is_phone else 'email'
    index_name = f"{search_attr}-index" # Expected GSIs: email-index, phone-index

    try:
        # Efficient lookup using Query on Global Secondary Index
        response = table.query(
            IndexName=index_name,
            KeyConditionExpression=Key(search_attr).eq(param)
        )
        items = response.get('Items', [])

        if items:
            # Return the userId found in the first matching record
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({
                    "userId": items[0].get('userId'),
                    "identifiedBy": search_attr
                })
            }
        else:
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"message": "User not found"})
            }

    except (BotoCoreError, ClientError) as e:
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": "Database lookup failed", "error": str(e)})
        }