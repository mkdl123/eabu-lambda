import os
import json
import base64
import logging
from datetime import datetime, timezone
import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

def _parse_body(event) -> dict:
    body = event.get("body")
    if body is None:
        raise ValueError("Missing request body")

    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")

    if isinstance(body, str):
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON body: {e}") from e

    if isinstance(body, dict):
        return body

    raise ValueError("Unsupported body type")

def _validate_payload(payload: dict, REQUIRED_FIELDS):
    missing = [k for k in REQUIRED_FIELDS if k not in payload]
    if missing:
        raise ValueError(f"Missing required field(s): {', '.join(missing)}")


def _response(status: int, body: dict):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }

def increment_counter(partition_key, table, counter_attribute='total_count'):

        response = table.update_item(
            Key={'ID': partition_key, 'zipcode': '11111'},
            UpdateExpression=f'SET {counter_attribute} = if_not_exists({counter_attribute}, :start) + :inc',
            ExpressionAttributeValues={':inc': 1, ':start': 0},
            ReturnValues='UPDATED_NEW'
        )
        return response['Attributes'][counter_attribute]


def increment_counter_with(sort_key_initial_value, table, partitionkey,counter_attribute='total_count', ):
    """
    Increments a counter attribute in a DynamoDB table.

    Args:
        partition_key: The partition key of the item containing the counter.
        counter_attribute: The name of the counter attribute. Defaults to 'counter'.

    Returns:
        The new counter value.
    """
    response = table.update_item(
        Key={'ID': sort_key_initial_value, partitionkey: '11111'},
        UpdateExpression=f'SET {counter_attribute} = if_not_exists({counter_attribute}, :start) + :inc',
        ExpressionAttributeValues={':inc': 1, ':start': 0},
        ReturnValues='UPDATED_NEW'
    )
    return response['Attributes'][counter_attribute]

