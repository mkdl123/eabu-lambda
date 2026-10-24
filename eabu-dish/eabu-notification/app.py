import os
import logging
import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

region = os.environ.get("REGION_NAME", "us-east-1")
followers_table_name = os.environ.get("FOLLOWERS_TABLE_NAME", "eabu-followers")

dynamodb = boto3.resource("dynamodb", region_name=region)
followers_table = dynamodb.Table(followers_table_name)
ses = boto3.client('ses', region_name=region)


def send_email(recipient_email, subject, body):
    try:
        print("Sending email to: ", recipient_email)
        print("Subject: ", subject)
        print("Body: ", body)
        sender_email = os.environ.get("SENDER_EMAIL", "eabu@eabu.net")
        response = ses.send_email(
            Source=sender_email,
            Destination={'ToAddresses': [recipient_email]},
            Message={
                'Subject': {'Data': subject},
                'Body': {'Text': {'Data': body}}
            }
        )
        logger.info(f"Email sent! Message ID: {response['MessageId']}")
        return True
    except (BotoCoreError, ClientError) as e:
        logger.error(f"Failed to send email: {str(e)}")
        return False


def get_follower_emails(user_id):
    try:
        response = followers_table.query(
            KeyConditionExpression='pUserId = :userId',
            ExpressionAttributeValues={':userId': user_id}
        )
        return [item['cEmail'] for item in response.get('Items', []) if 'cEmail' in item]
    except (BotoCoreError, ClientError) as e:
        logger.error(f"Failed to get followers: {str(e)}")
        return []


def lambda_handler(event, context):
    try:
        item = event['item']
        user_id = item['userId']

        follower_email = get_follower_emails(user_id)
        print("Follower Email: ",follower_email);
        if not follower_email:
            logger.info("No followers found to notify")
            return {'statusCode': 200, 'message': 'No followers to notify'}

        subject = f"New Service Alert: {item['title']}"
        body = f"""
A new service has been posted by a provider you follow!

Title: {item['title']}
Description: {item['description']}
Location: Zipcode {item['zipcode']}
Contact Phone: {item['phone']}

Check it out now!
"""

        for email in follower_email:
            send_email(email, subject, body)

        return {
            'statusCode': 200,
            'message': f'Notifications sent to {len(follower_email)} followers'
        }

    except Exception as e:
        logger.error(f"Error processing notification: {str(e)}")
        return {
            'statusCode': 500,
            'message': 'Error processing notification'
        }