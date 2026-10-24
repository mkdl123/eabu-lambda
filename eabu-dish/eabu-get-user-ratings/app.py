import json
import logging
import os

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

EAT_PROVIDER_TABLE_NAME = os.environ.get("EAT_PROVIDER_TABLE_NAME", "eabu-eat-provider")
LIVE_PROVIDER_TABLE_NAME = os.environ.get("LIVE_PROVIDER_TABLE_NAME", "eabu-live-provider")
MOVE_PROVIDER_TABLE_NAME = os.environ.get("MOVE_PROVIDER_TABLE_NAME", "eabu-move-provider")
REGION_NAME = os.environ.get("REGION_NAME", "us-east-1")

dynamodb = boto3.resource("dynamodb", region_name=REGION_NAME)

eat_table = dynamodb.Table(EAT_PROVIDER_TABLE_NAME)
live_table = dynamodb.Table(LIVE_PROVIDER_TABLE_NAME)
move_table = dynamodb.Table(MOVE_PROVIDER_TABLE_NAME)


def _extract_puser_id(event) -> str:
    """
    Try to get pUserId from:
    - queryStringParameters: ?pUserId=...
    - JSON body: { "pUserId": "..." }
    """
    # Query string
    qs = event.get("queryStringParameters") or {}
    if "pUserId" in qs and qs["pUserId"]:
        return str(qs["pUserId"])

    # JSON body
    body = event.get("body")
    if body:
        try:
            payload = json.loads(body)
            if isinstance(payload, dict) and "pUserId" in payload and payload["pUserId"]:
                return str(payload["pUserId"])
        except (TypeError, ValueError):
            pass

    return ""


def _sum_ratings_for_user(table, pUserId: str) -> tuple[int, int]:
    """
    Query a single table for items with the given pUserId (in userId-index)
    and sum totalstars and ratingscount.

    Returns:
        (total_stars_sum, ratings_count_sum)
    """
    total_stars = 0
    ratings_count = 0

    try:
        response = table.query(
            IndexName="userId-index",
            KeyConditionExpression=Key("userId").eq(pUserId),
        )
    except (BotoCoreError, ClientError):
        logger.exception("Failed to query table %s for user %s", table.name, pUserId)
        return 0, 0

    items = response.get("Items", []) or []

    for item in items:
        try:
            ts = int(item.get("totalstars", 0))
            rc = int(item.get("ratingscount", 0))
        except (TypeError, ValueError):
            ts = 0
            rc = 0
        total_stars += ts
        ratings_count += rc

    # Handle pagination if LastEvaluatedKey is present
    while "LastEvaluatedKey" in response:
        try:
            response = table.query(
                IndexName="userId-index",
                KeyConditionExpression=Key("userId").eq(pUserId),
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
        except (BotoCoreError, ClientError):
            logger.exception("Failed to continue query for table %s", table.name)
            break

        items = response.get("Items", []) or []
        for item in items:
            try:
                ts = int(item.get("totalstars", 0))
                rc = int(item.get("ratingscount", 0))
            except (TypeError, ValueError):
                ts = 0
                rc = 0
            total_stars += ts
            ratings_count += rc

    return total_stars, ratings_count


def lambda_handler(event, context):
    logger.info("Received event: %s", json.dumps({k: event.get(k) for k in ("httpMethod", "queryStringParameters")}))

    pUserId = event['pathParameters']['pUserId']
    # print("Parameter id: ",id);
    print("Parameter userId: ", pUserId);
    if not pUserId:
        return {
            "statusCode": 400,
            "body": json.dumps({"msg": "Missing required parameter: pUserId"}),
        }

    # Sum ratings from all three tables
    eat_ts, eat_rc = _sum_ratings_for_user(eat_table, pUserId)
    live_ts, live_rc = _sum_ratings_for_user(live_table, pUserId)
    move_ts, move_rc = _sum_ratings_for_user(move_table, pUserId)

    total_stars = eat_ts + live_ts + live_rc + move_ts  # bug; correct below
    total_ratings = eat_rc + live_rc + move_rc         # this is fine

    # Fix total_stars (typo)
    total_stars = eat_ts + live_ts + move_ts

    if total_ratings == 0:
        average_rating = 0.0
    else:
        average_rating = total_stars / total_ratings

    # body = {
    #     "pUserId": pUserId,
    #     "totalstars": total_stars,
    #     "ratingscount": total_ratings,
    #     "averageRating": average_rating,
    #     "details": {
    #         "eat": {"totalstars": eat_ts, "ratingscount": eat_rc},
    #         "live": {"totalstars": live_ts, "ratingscount": live_rc},
    #         "move": {"totalstars": move_ts, "ratingscount": move_rc},
    #     },
    # }
    body = {
        "averageRating": average_rating
    }
    return {
        "statusCode": 200,
        "body": json.dumps(body),
    }