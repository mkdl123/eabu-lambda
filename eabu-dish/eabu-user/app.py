import json
import boto3
from botocore.exceptions import BotoCoreError, ClientError

s3 = boto3.client("s3")

def lambda_handler(event, context):
    #query = event.get("queryStringParameters") or {}
    #bucket = query.get("bucket")
    #key = query.get("key")
    userid = event['pathParameters']['pUserId']
    key=userid+"/"+"selfie.jpg"
    bucket="eabu-identity-pictures"

    if not bucket or not key:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": "bucket and key query parameters are required"})
        }

    try:
        url = s3.generate_presigned_url(
            ClientMethod='get_object',
            Params={'Bucket': bucket, 'Key': key},
            ExpiresIn=6000  # URL valid for 5 minutes
        )

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"url": url})
        }

    except (BotoCoreError, ClientError):
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"message": "Failed to generate presigned URL"})
        }