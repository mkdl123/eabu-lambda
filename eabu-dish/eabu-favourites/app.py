"""Favourite sellers (saved by buyers) and favourite buyers (saved by sellers).

GET    /favourites/{userId}/{kind}                 the user's favourites of that kind
POST   /favourites                                 body {"userId", "kind", "favUserId"}
DELETE /favourites/{userId}/{kind}/{favUserId}
GET    /favourites-feed/{userId}/{kind}?zip=12345  current posts (last 30 days) by those favourites
                                                   within 50 miles of ?zip (all of them without ?zip)
GET    /favourites-feed/{userId}/{kind}?zip=12345&nearby=1
                                                   everyone's current posts of that kind within 50 miles
                                                   of ?zip (not the user's own), favourites flagged with
                                                   isFavourite

Both feeds page when given ?limit=N (1-50): the response then carries nextCursor, which is
passed back as ?cursor= for the next N posts (null once there are no more). The cursor is the
sort key of the last post sent, so posts added meanwhile don't shift or repeat later pages.
Without ?limit the whole list comes back as before.

kind is "seller" (a buyer's favourite sellers: their service listings) or "buyer" (a seller's
favourite buyers: their requests). Favourites are private: only the user can read or change
their own. Feed posts are newest first, each with distanceMiles from ?zip.
"""
import base64
import binascii
import json
import logging
import os
import re
import time
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
NEARBY_MAX_POSTS = 200  # only when not paging
MAX_PAGE_SIZE = 50
# The nearby feed scans whole post tables. A warm container keeps each scan this long, so
# scrolling through pages (and other users nearby) don't repeat it. A new post can take this
# long to appear in other people's feeds.
SCAN_CACHE_SECONDS = 60
_scan_cache = {}  # table name -> (time scanned, cutoff used, items)
FEED_FIELDS = ("ID", "zipcode", "userId", "title", "description", "address", "phone",
               "totalstars", "ratingscount", "createdAt", "picture1", "picture2", "picture3", "picture4")


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


class BadPaging(ValueError):
    pass


def _sort_key(post):
    return (str(post.get("createdAt", "")), post.get("category", ""), int(post.get("ID", 0)))


def _paging(event):
    """(limit, cursor key) from ?limit/?cursor; (None, None) when not paging. Raises BadPaging."""
    query = event.get("queryStringParameters") or {}
    raw_limit, raw_cursor = query.get("limit"), query.get("cursor")
    if raw_limit in (None, ""):
        if raw_cursor:
            raise BadPaging("cursor needs limit")
        return None, None
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        raise BadPaging("Invalid limit")
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise BadPaging(f"limit must be 1-{MAX_PAGE_SIZE}")
    if not raw_cursor:
        return limit, None
    try:
        padded = raw_cursor + "=" * (-len(raw_cursor) % 4)
        created, category, item_id = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return limit, (str(created), str(category), int(item_id))
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError):
        raise BadPaging("Invalid cursor")


def _encode_cursor(post):
    raw = json.dumps(list(_sort_key(post)), separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _page(posts, limit, after):
    """Sort newest first; return (posts for this page, nextCursor or None)."""
    posts.sort(key=_sort_key, reverse=True)
    if limit is None:
        return posts, None
    if after is not None:
        posts = [p for p in posts if _sort_key(p) < after]
    page = posts[:limit]
    return page, (_encode_cursor(page[-1]) if len(posts) > limit else None)


def _attach_pictures(posts):
    for category in CATEGORIES:
        helpers.attach_picture_urls([p for p in posts if p["category"] == category], category)


def feed(event, user_id, kind):
    denied = _check(event, user_id, kind)
    if denied:
        return denied
    limit, after = _paging(event)
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
                posts += [{**p, "category": category} for p in found]
                if "LastEvaluatedKey" not in page:
                    break
                kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    if zip_code:
        posts = helpers.within_miles(zip_code, posts, helpers.NEARBY_MILES)
    posts, next_cursor = _page(posts, limit, after)
    if kind == "seller":
        _attach_pictures(posts)  # only for the posts being sent
    result = {"Items": posts, "Count": len(posts), "radiusMiles": helpers.NEARBY_MILES if zip_code else None}
    if limit is not None:
        result["nextCursor"] = next_cursor
    return _json(200, result)


def _recent_posts(table, cutoff):
    """Every post in the table created since cutoff, reusing a scan made in the last
    SCAN_CACHE_SECONDS (its cutoff is at most that much older, so it is filtered again)."""
    cached = _scan_cache.get(table.name)
    if cached and time.time() - cached[0] < SCAN_CACHE_SECONDS:
        return [i for i in cached[2] if str(i.get("createdAt", "")) >= cutoff]
    items = _recent_posts_uncached(table, cutoff)
    _scan_cache[table.name] = (time.time(), cutoff, items)
    return items


def _recent_posts_uncached(table, cutoff):
    """Scan the table for posts created since cutoff.
    A scan is fine while the post tables are small (DynamoDB TTL now deletes posts after
    60 days, which bounds them). If they grow large, add a geohash GSI and query the cells
    around the user instead (a 50-mile radius covers up to ~900 ZIPs, so querying ZIP by
    ZIP isn't practical)."""
    items, kwargs = [], {"FilterExpression": Attr("createdAt").gte(cutoff)}  # skips the counter row too
    while True:
        page = table.scan(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def nearby_feed(event, user_id, kind, zip_code):
    """Everyone's current posts within NEARBY_MILES of zip_code, newest first, favourites flagged."""
    denied = _check(event, user_id, kind)
    if denied:
        return denied
    if not zip_code:
        return helpers._response(400, {"msg": "zip is required"})
    limit, after = _paging(event)
    fav_ids = {f["favUserId"] for f in _list(user_id, kind)}
    cutoff = helpers.post_limit_cutoff()
    posts = []
    for category in CATEGORIES:
        found = [{k: i[k] for k in FEED_FIELDS if k in i}
                 for i in _recent_posts(_table_for(kind, category), cutoff) if i.get("userId") != user_id]
        found = helpers.within_miles(zip_code, found, helpers.NEARBY_MILES)
        posts += [{**p, "category": category, "isFavourite": p.get("userId") in fav_ids} for p in found]
    posts, next_cursor = _page(posts, limit, after)
    if limit is None:
        posts = posts[:NEARBY_MAX_POSTS]
    if kind == "seller":
        _attach_pictures(posts)  # only for the posts being sent
    result = {"Items": posts, "Count": len(posts), "radiusMiles": helpers.NEARBY_MILES}
    if limit is not None:
        result["nextCursor"] = next_cursor
    return _json(200, result)


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
            query = event.get("queryStringParameters") or {}
            if str(query.get("nearby") or "").lower() in ("1", "true", "yes"):
                zip_code = str(query.get("zip") or "").strip()[:10]
                return nearby_feed(event, path.get("userId"), path.get("kind"), zip_code)
            return feed(event, path.get("userId"), path.get("kind"))
        if method == "GET":
            return list_favourites(event, path.get("userId"), path.get("kind"))
        return helpers._response(405, {"msg": "Method not allowed"})
    except BadPaging as e:
        return helpers._response(400, {"msg": str(e)})
    except (BotoCoreError, ClientError):
        logger.exception("Favourites request failed")
        return helpers._response(500, {"msg": "Something went wrong. Please try again."})
    except Exception:
        logger.exception("Lambda execution failed")
        return helpers._response(500, {"msg": "Internal server error"})
