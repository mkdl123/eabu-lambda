"""GET /discovered?zip=12345[&v=2] - this week's "Discovered near you" cards for the caller's area.

Signed in only. The area is one whose member ZIPs include ?zip or whose centre is within
AREA_MILES, preferring one with current finds, then the nearest. Finds come from the weekly
eabu-discover job (web search, not Eabu users), newest week first, and expire after 8 days.

Any US ZIP gets covered: a known ZIP with no area yet becomes a new (pending) area and its
first search starts at once (cards appear a minute or two later); at most NEW_AREAS_PER_DAY
a day (UTC), beyond that it waits for Monday. Viewing an area marks it (lastViewedAt, at most
once a day) so the Monday run keeps searching it; unviewed areas drop out after 28 days.

Cards are top-rated places (category eat or lifestyle) and Ticketmaster events (lifestyle,
kind event; hidden once their day has passed, US Eastern). Without ?v=2 (app 1.7) only eat places
and the older news finds come back, since that build only knows food cards.

Response: {"areaZip", "weekOf", "pending", "Items": [{ID, areaZip, category, topic, kind, headline,
           place, addressOrArea, what, when, whyNew, sourceUrl, sourceSite, sourceDate, placeType,
           rating, ratingCount, ratingSite, mapsUrl, eventDate, eventTime, ticketUrl, addressLat,
           addressLng (map pin; "" when the address couldn't be placed)}], "Count"}
"""
import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from my_utils import helpers

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REGION = os.environ.get("REGION_NAME", "us-east-1")
discovered = boto3.resource("dynamodb", region_name=REGION).Table(
    os.environ.get("DISCOVERED_TABLE_NAME", "eabu-discovered"))

lambda_client = boto3.client("lambda", region_name=REGION)
DISCOVER_FUNCTION = os.environ.get("DISCOVER_FUNCTION_NAME", "")

AREA_MILES = 25
AREA_ROW = 0
NEW_AREAS_PER_DAY = int(os.environ.get("DISCOVER_NEW_AREAS_PER_DAY", "20"))
VIEWED_DAYS = 28
DAY = 86400
CACHE_SECONDS = 600  # the table changes once a week
FIELDS = ("ID", "areaZip", "category", "topic", "kind", "headline", "place", "addressOrArea", "what",
          "when", "whyNew", "sourceUrl", "sourceSite", "sourceDate", "placeType", "rating", "ratingCount",
          "ratingSite", "mapsUrl", "eventDate", "eventTime", "ticketUrl", "addressLat", "addressLng")
_cache = None  # (time loaded, {areaZip: area row}, {areaZip: [finds]})


def _load(force=False):
    """The whole table (a few hundred rows at most), kept for CACHE_SECONDS per container."""
    global _cache
    if not force and _cache and time.time() - _cache[0] < CACHE_SECONDS:
        return _cache[1], _cache[2]
    areas, finds, kwargs = {}, {}, {}
    while True:
        page = discovered.scan(**kwargs)
        for item in page.get("Items", []):
            if helpers._norm_zip(item["areaZip"]) != item["areaZip"]:
                continue  # the daily new-area counter rows
            if int(item["ID"]) == AREA_ROW:
                areas[item["areaZip"]] = item
            else:
                finds.setdefault(item["areaZip"], []).append(item)
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    _cache = (time.time(), areas, finds)
    return areas, finds


def _live(rows, now):
    return [f for f in rows or [] if int(f.get("expiresAt") or 0) > now]


def _today_eastern(now):
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(now, ZoneInfo(helpers.POST_LIMIT_TIMEZONE)).date().isoformat()


def for_client(items, version, now):
    """What this app build can show: v2 gets every card except past events; older builds get food
    places only (rows without a category are the older news finds, which are food too)."""
    today = _today_eastern(now)
    out = []
    for f in items:
        if f.get("kind") == "event" and str(f.get("eventDate") or "") < today:
            continue
        if version < 2 and (f.get("category") not in (None, "eat") or f.get("kind") == "event"):
            continue
        out.append(f)
    return out


def area_for(zip_code, areas, finds=None, now=0):
    """The area serving zip_code: among areas that list it or whose centre is within AREA_MILES,
    one with current finds first, then the nearest. None if there is no such area."""
    z = helpers._norm_zip(zip_code)
    if not z:
        return None
    best = None
    for centre, row in areas.items():
        if z == centre:
            d = -1.0  # its own area beats one it merely belongs to
        else:
            d = 0.0 if z in (row.get("zips") or []) else helpers.zip_distance_miles(z, centre)
        if d is None or d > AREA_MILES:
            continue
        rank = (0 if _live((finds or {}).get(centre), now) else 1, d, centre)
        if best is None or rank < best:
            best = rank
    return best[2] if best else None


def _mark_viewed(centre, row, now):
    """Keep the area in the Monday run (written at most once a day per area)."""
    if int(row.get("lastViewedAt") or 0) > now - DAY:
        return
    try:
        discovered.update_item(Key={"areaZip": centre, "ID": AREA_ROW},
                               UpdateExpression="SET lastViewedAt = :n, expiresAt = :e",
                               ConditionExpression="attribute_exists(areaZip)",
                               ExpressionAttributeValues={":n": now, ":e": now + VIEWED_DAYS * DAY})
        row["lastViewedAt"] = now
    except (BotoCoreError, ClientError):
        logger.warning("Could not mark area %s viewed", centre)


def start_new_area(zip_code, areas, now):
    """Create a pending area centred on zip_code and start its first search (within the daily
    cap). True if the area now exists (searching now, or on Monday)."""
    if not helpers.is_known_zip(zip_code):
        return False
    row = {"areaZip": zip_code, "ID": AREA_ROW, "zips": [zip_code], "pending": True,
           "requestedAt": now, "lastViewedAt": now, "expiresAt": now + VIEWED_DAYS * DAY}
    try:
        discovered.put_item(Item=row, ConditionExpression="attribute_not_exists(areaZip)")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return True  # another request created it first
        logger.warning("Could not create area %s", zip_code)
        return False
    except BotoCoreError:
        return False
    areas[zip_code] = row
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    try:  # daily cap, so trying lots of ZIPs can't run up the Claude bill
        discovered.update_item(Key={"areaZip": f"daily#{day}", "ID": AREA_ROW},
                               UpdateExpression="ADD n :one SET expiresAt = :e",
                               ConditionExpression="attribute_not_exists(n) OR n < :cap",
                               ExpressionAttributeValues={":one": 1, ":cap": NEW_AREAS_PER_DAY,
                                                          ":e": now + 2 * DAY})
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info("New-area cap reached; %s waits for Monday", zip_code)
            row.pop("pending", None)  # nothing is searching it now
            try:
                discovered.update_item(Key={"areaZip": zip_code, "ID": AREA_ROW}, UpdateExpression="REMOVE pending")
            except (BotoCoreError, ClientError):
                pass
            return True
        logger.warning("Could not count new area %s", zip_code)
        return True
    except BotoCoreError:
        return True
    try:
        lambda_client.invoke(FunctionName=DISCOVER_FUNCTION, InvocationType="Event",
                             Payload=json.dumps({"areaZip": zip_code, "zips": [zip_code]}).encode())
        logger.info("Started first search for new area %s", zip_code)
    except (BotoCoreError, ClientError):
        logger.exception("Could not start the search for %s; it waits for Monday", zip_code)
    return True


def _fresh(centre, areas, finds, now):
    """Re-read one searching area, bypassing the cache."""
    try:
        rows = helpers.query_all(discovered, KeyConditionExpression=Key("areaZip").eq(centre)).get("Items", [])
    except (BotoCoreError, ClientError):
        return []
    area = next((r for r in rows if int(r["ID"]) == AREA_ROW), None)
    if area:
        areas[centre] = area
    finds[centre] = [r for r in rows if int(r["ID"]) != AREA_ROW]
    return _live(finds[centre], now)


@helpers.cors
def lambda_handler(event, context):
    if (event.get("httpMethod") or "GET").upper() != "GET":
        return helpers._response(405, {"msg": "Method not allowed"})
    denied = helpers.require_authenticated(event)
    if denied:
        return denied
    params = event.get("queryStringParameters") or {}
    zip_code = (params.get("zip") or "").strip()
    try:
        version = int(params.get("v") or 1)
    except ValueError:
        version = 1
    if not helpers._norm_zip(zip_code):
        return helpers._response(400, {"msg": "A 5-digit zip is required"})
    try:
        areas, finds = _load()
    except (BotoCoreError, ClientError):
        logger.exception("Could not read discovered finds")
        return helpers._response(500, {"msg": "Could not load. Please try again."})

    now = int(time.time())
    zip5 = helpers._norm_zip(zip_code)
    centre = area_for(zip5, areas, finds, now)
    if not centre and helpers.is_known_zip(zip5):
        # This container's copy may predate an area another user's request just created (same
        # or nearby ZIP): re-read before treating the ZIP as new, so nobody waits for the cache
        # or starts a second search for the same place.
        try:
            areas, finds = _load(force=True)
        except (BotoCoreError, ClientError):
            logger.exception("Could not re-read discovered finds")
            return helpers._response(500, {"msg": "Could not load. Please try again."})
        centre = area_for(zip5, areas, finds, now)
    if centre:
        _mark_viewed(centre, areas[centre], now)
    elif start_new_area(zip5, areas, now):
        centre = zip5
    items = _live(finds.get(centre), now) if centre else []
    if centre and not items and (areas.get(centre) or {}).get("pending"):
        items = _fresh(centre, areas, finds, now)  # its first search may have just finished
    if items:  # newest week only, in the order Claude ranked them
        latest = max(int(f["ID"]) // 100 for f in items)
        items = sorted((f for f in items if int(f["ID"]) // 100 == latest), key=lambda f: int(f["ID"]))
        items = for_client(items, version, now)
    out = [{k: str(f.get(k, "")) for k in FIELDS} for f in items]
    pending = bool(centre and not items and (areas.get(centre) or {}).get("pending"))
    return helpers._response(200, {"areaZip": centre, "weekOf": items[0].get("weekOf") if items else None,
                                   "pending": pending, "Items": out, "Count": len(out)})
