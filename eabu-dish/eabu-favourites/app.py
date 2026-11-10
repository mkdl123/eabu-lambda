"""Favourite sellers (saved by buyers) and favourite buyers (saved by sellers).

GET    /favourites/{userId}/{kind}                 the user's favourites of that kind
POST   /favourites                                 body {"userId", "kind", "favUserId"}
DELETE /favourites/{userId}/{kind}/{favUserId}
GET    /favourites-feed/{userId}/{kind}?zip=12345  current posts (last 30 days) by those favourites
                                                   within 25 miles of ?zip (all of them without ?zip)

kind is "seller" (a buyer's favourite sellers: their service listings) or "buyer" (a seller's
favourite buyers: their requests). Favourites are private: only the user can read or change
their own. Feed posts are newest first, each with distanceMiles from ?zip.
"""
import json
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError

from my_utils import helpers

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

region = os.environ.get("REGION_NAME", "us-east-1")
dynamodb = boto3.resource("dynamodb", region_name=region)
favourites = dynamodb.Table(os.environ.get("FAVOURITES_TABLE_NAME", "eabu-favourites"))

KINDS = ("seller", "buyer")
MAX_FAVOURITES = 50
USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_@.+:-]{1,128}$")
CATEGORIES = ("eat", "live", "move", "lifestyle")
# seller -> their service listings (provider tables); buyer -> their requests (consumer tables)
ROLE_TABLE = {"seller": "PROVIDER", "buyer": "CONSUMER"}
FEED_FIELDS = ("ID", "zipcode", "userId", "title", "description", "address", "phone",
               "totalstars", "ratingscount", "createdAt", "picture1", "picture2")


def _table_for(kind, category):
    role = ROLE_TABLE[kind]
    return dynamodb.Table(os.environ.get(f"{category.upper()}_{role}_TABLE_NAME",
                                         f"eabu-{category}-{role.lower()}"))


def _json(status, body):
    return helpers._response(status, json.loads(json.dumps(body, default=_decimal)))


def _decimal(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    raise TypeError(type(obj).__name__)


def _check(event, user_id, kind, fav_user_id=None):
    """Validate ids/kind and that the caller is user_id. Returns an error response or None."""
    if not isinstance(user_id, str) or not USER_ID_PATTERN.match(user_id):
        return helpers._response(400, {"msg": "Invalid userId"})
    if kind not in KINDS:
        return helpers._response(400, {"msg": "kind must be seller or buyer"})
    if fav_user_id is not None and (not isinstance(fav_user_id, str) or not USER_ID_PATTERN.match(fav_user_id)):
        return helpers._response(400, {"msg": "Invalid favUserId"})
    return helpers.authorize_user(event, user_id)


def _list(user_id, kind):
    items, kwargs = [], {"KeyConditionExpression": Key("userId").eq(user_id) & Key("favKey").begins_with(f"{kind}#")}
    while True:
        page = favourites.query(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def list_favourites(event, user_id, kind):
    denied = _check(event, user_id, kind)
    if denied:
        return denied
    items = [{"favUserId": i["favUserId"], "createdAt": i.get("createdAt")} for i in _list(user_id, kind)]
    return _json(200, {"Items": items, "Count": len(items)})


def add_favourite(event):
    try:
        body = helpers._parse_body(event)
    except ValueError:
        return helpers._response(400, {"msg": "Invalid JSON format"})
    user_id, kind, fav = body.get("userId"), body.get("kind"), body.get("favUserId")
    denied = _check(event, user_id, kind, fav)
    if denied:
        return denied
    if fav == user_id:
        return helpers._response(400, {"msg": "You can't add yourself as a favourite"})
    current = _list(user_id, kind)
    if any(i["favUserId"] == fav for i in current):
        return _json(200, {"message": "Already a favourite", "favUserId": fav})
    if len(current) >= MAX_FAVOURITES:
        return helpers._response(400, {"msg": f"You can save up to {MAX_FAVOURITES} favourites"})
    favourites.put_item(Item={"userId": user_id, "favKey": f"{kind}#{fav}", "kind": kind, "favUserId": fav,
                              "createdAt": datetime.now(timezone.utc).isoformat()})
    return _json(200, {"message": "Favourite saved", "favUserId": fav})


def remove_favourite(event, user_id, kind, fav):
    denied = _check(event, user_id, kind, fav)
    if denied:
        return denied
    favourites.delete_item(Key={"userId": user_id, "favKey": f"{kind}#{fav}"})
    return _json(200, {"message": "Favourite removed", "favUserId": fav})


def feed(event, user_id, kind):
    denied = _check(event, user_id, kind)
    if denied:
        return denied
    zip_code = str(((event.get("queryStringParameters") or {}).get("zip") or "")).strip()[:10]
    cutoff = helpers.post_limit_cutoff()
    posts = []
    for fav in _list(user_id, kind):
        for category in CATEGORIES:
            table = _table_for(kind, category)
            kwargs = {"IndexName": "userId-index",
                      "KeyConditionExpression": Key("userId").eq(fav["favUserId"]),
                      "FilterExpression": Attr("createdAt").gte(cutoff)}
            while True:
                page = table.query(**kwargs)
                found = [{k: i[k] for k in FEED_FIELDS if k in i} for i in page.get("Items", [])]
                if kind == "seller":
                    helpers.attach_picture_urls(found, category)
                posts += [{**p, "category": category} for p in found]
                if "LastEvaluatedKey" not in page:
                    break
                kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    if zip_code:
        posts = helpers.within_miles(zip_code, posts, helpers.NEARBY_MILES)
    posts.sort(key=lambda p: str(p.get("createdAt", "")), reverse=True)
    return _json(200, {"Items": posts, "Count": len(posts), "radiusMiles": helpers.NEARBY_MILES if zip_code else None})


@helpers.cors
def lambda_handler(event, context):
    try:
        method = (event.get("httpMethod") or "").upper()
        resource = event.get("resource") or ""
        path = event.get("pathParameters") or {}
        if method == "POST":
            return add_favourite(event)
        if method == "DELETE":
            return remove_favourite(event, path.get("userId"), path.get("kind"), path.get("favUserId"))
        if method == "GET" and resource.startswith("/favourites-feed"):
            return feed(event, path.get("userId"), path.get("kind"))
        if method == "GET":
            return list_favourites(event, path.get("userId"), path.get("kind"))
        return helpers._response(405, {"msg": "Method not allowed"})
    except (BotoCoreError, ClientError):
        logger.exception("Favourites request failed")
        return helpers._response(500, {"msg": "Something went wrong. Please try again."})
    except Exception:
        logger.exception("Lambda execution failed")
        return helpers._response(500, {"msg": "Internal server error"})
