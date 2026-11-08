import os
import json
import base64
import functools
import logging
from datetime import datetime, timezone
import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Longest value accepted for each client-supplied field.
FIELD_LIMITS = {
    "zipcode": 10, "zip": 10, "userId": 128, "pUserId": 128, "cUserId": 128,
    "title": 200, "description": 5000, "offerDescription": 5000, "message": 5000,
    "phone": 32, "phoneNumber": 32, "email": 254, "price": 32, "serviceType": 100,
    "latitude": 32, "longitude": 32, "address": 200,
}
COORDINATE_FIELDS = ("latitude", "longitude")
MAX_NUMBER_LENGTH = 32  # DynamoDB numbers hold 38 digits at most

def _parse_body(event) -> dict:
    body = event.get("body")
    if body is None:
        raise ValueError("Missing request body")

    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")

    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON body: {e}") from e

    if isinstance(body, dict):
        return body

    raise ValueError("Request body must be a JSON object")

def _validate_payload(payload: dict, REQUIRED_FIELDS):
    missing = [k for k in REQUIRED_FIELDS if k not in payload]
    if missing:
        raise ValueError(f"Missing required field(s): {', '.join(missing)}")
    _validate_fields(payload)


def _validate_fields(payload: dict):
    """Reject values that are too long, or of a type DynamoDB can't store (floats, lists...)."""
    for field, limit in FIELD_LIMITS.items():
        value = payload.get(field)
        if value is None:
            continue
        allowed = (str, int, float) if field in COORDINATE_FIELDS else (str, int)
        if isinstance(value, bool) or not isinstance(value, allowed):
            raise ValueError(f"{field} has an invalid type")
        if len(str(value)) > (limit if isinstance(value, str) else MAX_NUMBER_LENGTH):
            raise ValueError(f"{field} is too long (max {limit} characters)")


def _response(status: int, body: dict):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }

def cors(handler):
    """Add the CORS header to every response of a handler that builds its own responses."""
    @functools.wraps(handler)
    def wrapper(event, context):
        response = handler(event, context)
        response.setdefault("headers", {}).setdefault("Access-Control-Allow-Origin", "*")
        return response
    return wrapper


# ---------------------------------------------------------------------------
# Authentication / authorization
#
# The caller's identity comes ONLY from the Cognito claims that API Gateway
# places in event["requestContext"] after its Cognito authorizer has verified
# the token. Clients cannot forge requestContext. Never trust a userId that
# arrives in the body, path or query string without comparing it to this.
#
# AUTH_MODE:
#   "off"     - allow requests as before, but log every request that
#               WOULD be rejected. Use this while rolling the authorizer out.
#   "enforce" - (default if unset) reject unauthenticated (401) and mismatched (403) requests.
# AUTH_USER_ID_CLAIM: which Cognito claim equals the app's userId ("sub" by
#   default; use "cognito:username" if the app stores the username).
# ---------------------------------------------------------------------------

def auth_enforced() -> bool:
    return os.environ.get("AUTH_MODE", "enforce").strip().lower() == "enforce"


def get_caller_id(event):
    """Return the authenticated caller's user id, or None if unauthenticated."""
    rc = (event or {}).get("requestContext") or {}
    authorizer = rc.get("authorizer") or {}
    # REST API Cognito authorizer -> authorizer.claims; HTTP API JWT authorizer -> authorizer.jwt.claims
    claims = authorizer.get("claims") or (authorizer.get("jwt") or {}).get("claims") or {}
    value = claims.get(os.environ.get("AUTH_USER_ID_CLAIM", "sub"))
    return str(value) if value else None


def require_authenticated(event):
    """Return None if the caller is signed in (or auth is not enforced), else a 401 response."""
    if get_caller_id(event) is not None:
        return None
    if not auth_enforced():
        logger.warning("AUTH not enforced: would reject unauthenticated request to %s", (event or {}).get("resource"))
        return None
    return _response(401, {"msg": "Unauthorized"})


def authorize_user(event, claimed_user_id):
    """
    Ensure the user id the client claims to act as is the authenticated caller.
    Return None if allowed, otherwise a 401/403 response to return immediately.
    """
    caller = get_caller_id(event)
    if caller is not None and claimed_user_id is not None and str(claimed_user_id) == caller:
        return None
    if not auth_enforced():
        logger.warning(
            "AUTH not enforced: would reject request to %s (caller=%s, claimed=%s)",
            (event or {}).get("resource"), caller, claimed_user_id,
        )
        return None
    if caller is None:
        return _response(401, {"msg": "Unauthorized"})
    return _response(403, {"msg": "Forbidden"})


def is_owner(event, user_id) -> bool:
    """True if the caller owns user_id. Always True while auth is not enforced."""
    caller = get_caller_id(event)
    if caller is not None and user_id is not None and str(user_id) == caller:
        return True
    return not auth_enforced()


# Listing fields anyone signed in may see on a seller's profile. Everything else
# (phone, exact coordinates, interested_by, ...) is for the owner only.
PUBLIC_LISTING_FIELDS = ("ID", "zipcode", "userId", "title", "description",
                         "address", "picture1", "picture2", "pictures",
                         "totalstars", "ratingscount", "createdAt")


# Sellers may add up to two pictures to each service listing. They live in the
# (private) picture bucket at listings/{category}/{ID}/{slot}.jpg, and the listing
# row records each one as picture1/picture2 = upload time.
LISTING_CATEGORIES = ("eat", "live", "move", "lifestyle")
PICTURE_SLOTS = ("1", "2")
PICTURE_URL_SECONDS = 3600
_s3_client = None


def listing_picture_key(category: str, item_id, slot: str) -> str:
    return f"listings/{category}/{int(item_id)}/{slot}.jpg"


def attach_picture_urls(items, category: str):
    """Add "pictures": [{"slot", "url"}] (short-lived links) to listings that have pictures."""
    global _s3_client
    bucket = os.environ.get("IDENTITY_BUCKET_NAME", "eabu-identity-pictures")
    for item in items:
        slots = [slot for slot in PICTURE_SLOTS if item.get(f"picture{slot}")]
        if not slots or "ID" not in item:
            continue
        if _s3_client is None:
            _s3_client = boto3.client("s3", region_name=os.environ.get("REGION_NAME", "us-east-1"))
        try:
            item["pictures"] = [{
                "slot": slot,
                "url": _s3_client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": bucket, "Key": listing_picture_key(category, item["ID"], slot)},
                    ExpiresIn=PICTURE_URL_SECONDS),
            } for slot in slots]
        except (BotoCoreError, ClientError, TypeError, ValueError):
            logger.exception("Could not sign listing picture links")
    return items


def optional_address(payload: dict):
    """The poster's optional pickup/service address, trimmed; None when not given."""
    value = payload.get("address")
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value or None


def public_listings(items):
    """Copies of provider listings reduced to PUBLIC_LISTING_FIELDS."""
    return [{k: item[k] for k in PUBLIC_LISTING_FIELDS if k in item} for item in items]


def query_result(response: dict) -> dict:
    """A DynamoDB query response without the AWS metadata, safe to return to clients."""
    return {k: v for k, v in response.items() if k != "ResponseMetadata"}


def coarsen_location(items, places=2):
    """Round coordinates in place (2 places is about 1 km) so a listing can't pinpoint a home."""
    for item in items:
        for field in COORDINATE_FIELDS:
            if field in item:
                try:
                    item[field] = str(round(float(item[field]), places))
                except (TypeError, ValueError):
                    del item[field]


def user_has_item(table, user_id: str, item_id: int) -> bool:
    """True if item_id is one of user_id's items, found through the table's userId-index."""
    kwargs = {"IndexName": "userId-index", "KeyConditionExpression": Key("userId").eq(user_id)}
    while True:
        response = table.query(**kwargs)
        if any(int(i["ID"]) == item_id for i in response.get("Items", [])):
            return True
        if "LastEvaluatedKey" not in response:
            return False
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


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

