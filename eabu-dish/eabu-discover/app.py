"""Weekly "Discovered near you" job (EventBridge schedule, no API route).

Monday run (scheduled event):
  1. Collects the ZIPs of every post and request from the last POST_DAYS days, plus the centre
     of every area someone viewed in the last VIEWED_DAYS days (GET /discovered records views
     and starts a first search for a ZIP with no area yet, so any US ZIP where someone opens
     the app gets covered).
  2. Groups them into areas: the busiest ZIP becomes an area centre, and every ZIP within
     AREA_MILES of it joins that area; repeat with the busiest ZIP left. At most MAX_AREAS,
     busiest first.
  3. Invokes this same function once per area, asynchronously ({"areaZip": ..., ...}), so a
     slow or failed area doesn't hold up the others.

Area run (that payload): asks Claude (MODEL, with web search) for new or limited food finds
this week near the area, checks each one (source dated within MAX_SOURCE_AGE_DAYS, a specific
https article, content filter, no repeat of last week) and saves up to MAX_FINDS in the
discovered table. Rows expire after EXPIRE_DAYS (DynamoDB TTL on expiresAt).

Table rows (eabu-discovered, key areaZip + ID):
  ID 0                  the area: centre ZIP, member ZIPs, weekOf, last run's place/headline keys
  ID yyyymmdd*100 + n   one find of the week starting yyyymmdd (Monday, UTC)

Only ZIP codes go to Claude, never anything about users. Run by hand for one area:
  aws lambda invoke --function-name <name> --invocation-type Event \
      --cli-binary-format raw-in-base64-out --payload '{"areaZip":"19060"}' /dev/null
"""
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError

from my_utils import content_filter, helpers

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REGION = os.environ.get("REGION_NAME", "us-east-1")
dynamodb = boto3.resource("dynamodb", region_name=REGION)
discovered = dynamodb.Table(os.environ.get("DISCOVERED_TABLE_NAME", "eabu-discovered"))
ssm = boto3.client("ssm", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)

MODEL = os.environ.get("DISCOVER_MODEL", "claude-haiku-5-5")
API_KEY_PARAM = os.environ.get("ANTHROPIC_KEY_PARAM", "/eabu/anthropic-api-key")
MAX_AREAS = int(os.environ.get("DISCOVER_MAX_AREAS", "40"))
AREA_MILES = 25
POST_DAYS = 60
VIEWED_DAYS = 28
MAX_FINDS = 8
MAX_SEARCHES = 15
MAX_FETCHES = 6
MAX_SOURCE_AGE_DAYS = 14
EXPIRE_DAYS = 8
MAX_PER_SOURCE = 2
AREA_ROW = 0
POST_TABLES = [f"{c}_{r}" for c in ("EAT", "LIVE", "MOVE", "LIFESTYLE") for r in ("CONSUMER", "PROVIDER")]
LIMITS = {"headline": 100, "place": 100, "address_or_area": 150, "what": 400, "when": 200, "why_new": 200}

SAVE_TOOL = {
    "name": "save_finds",
    "description": "Save the final list of food finds. Call this exactly once, after searching.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "finds": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "headline": {"type": "string", "description": "Short, fun one-liner, e.g. 'Sweet potato fries for breakfast'"},
                        "place": {"type": "string", "description": "Restaurant, cafe, truck or market name"},
                        "address_or_area": {"type": "string", "description": "Street address if known, else neighbourhood/town"},
                        "what": {"type": "string", "description": "One or two sentences in your own words, no copied text"},
                        "when": {"type": "string", "description": "Dates/days/hours it applies, as stated by the source"},
                        "why_new": {"type": "string", "description": "Why it counts as new this week"},
                        "source_url": {"type": "string"},
                        "source_date": {"type": "string", "description": "Publish/post date of the source, YYYY-MM-DD, or 'unknown'"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": ["headline", "place", "address_or_area", "what", "when",
                                 "why_new", "source_url", "source_date", "confidence"],
                    "additionalProperties": False,
                },
            },
            "notes": {"type": "string"},
        },
        "required": ["finds", "notes"],
        "additionalProperties": False,
    },
}

SYSTEM = """You find what is new in local food for an app's "Discovered near you" feed.

Search the public web for food items or food happenings near the given US ZIP code that are new or
limited for this week: new menu items, daily/weekly specials, seasonal items, new openings, pop-ups,
food trucks at a new spot, farmers market or food festival happenings.

Rules:
- Only include things whose source is dated within the last 14 days, or that state dates covering this week.
  Skip anything you can't date.
- Within about 25 miles of the ZIP. Prefer independent local places over national chain promotions.
- Each find needs a real source URL you actually saw. It must be the specific article, post or menu page,
  never a site's homepage or a section index. If you only saw a headline, open the page with web_fetch to
  confirm the details first; if you can't confirm them, leave the find out.
- Spread your searches across the whole area (nearby towns, farmers markets, food trucks), not one source.
  Use at most 2 finds from any one article, and at most 2 holiday-menu finds in total.
- Save fetches for the finds you'll actually include; one search result page or article per find is enough.
- Write descriptions in your own words; don't copy text from the source.
- No alcohol-only events, nothing sexual, no coupons that require sign-up.
- Fewer good finds beat more weak ones. If you find nothing solid, return an empty list and say why in notes.

When done searching, call save_finds once with your picks, best first."""

_client = None


def _claude():
    """The Anthropic client, with the key from SSM (SecureString), made once per container."""
    global _client
    if _client is None:
        import anthropic  # only area runs need it
        key = ssm.get_parameter(Name=API_KEY_PARAM, WithDecryption=True)["Parameter"]["Value"]
        _client = anthropic.Anthropic(api_key=key, timeout=240, max_retries=1)
    return _client


# ---------------------------------------------------------------------------
# Weekly run: areas
# ---------------------------------------------------------------------------
def week_start(today=None):
    today = today or datetime.now(timezone.utc).date()
    return today - timedelta(days=today.weekday())


def active_zip_counts(now=None):
    """{5-digit ZIP: number of posts and requests from the last POST_DAYS days}."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=POST_DAYS)).strftime("%Y-%m-%d")
    counts = {}
    for name in POST_TABLES:
        table = dynamodb.Table(os.environ.get(f"{name}_TABLE_NAME", "eabu-" + name.lower().replace("_", "-")))
        kwargs = {"FilterExpression": Attr("createdAt").gte(cutoff), "ProjectionExpression": "zipcode"}
        while True:
            page = table.scan(**kwargs)
            for item in page.get("Items", []):
                z = helpers._norm_zip(item.get("zipcode"))
                if z:
                    counts[z] = counts.get(z, 0) + 1
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return counts


def group_areas(counts, miles=AREA_MILES, max_areas=MAX_AREAS):
    """[(centre ZIP, [member ZIPs], activity)], busiest first. Unknown ZIPs form their own area."""
    left = sorted(counts, key=lambda z: (-counts[z], z))
    areas = []
    while left and len(areas) < max_areas:
        centre = left.pop(0)
        members, rest = [centre], []
        for z in left:
            d = helpers.zip_distance_miles(centre, z)
            (members if d is not None and d <= miles else rest).append(z)
        left = rest
        areas.append((centre, members, sum(counts[z] for z in members)))
    return areas


def viewed_area_zips(now=None):
    """Centres of areas whose finds someone looked at (or asked for) in the last VIEWED_DAYS days."""
    since = int((now or datetime.now(timezone.utc)).timestamp()) - VIEWED_DAYS * 86400
    zips, kwargs = [], {"FilterExpression": Attr("ID").eq(AREA_ROW) & Attr("lastViewedAt").gte(since),
                        "ProjectionExpression": "areaZip"}
    while True:
        page = discovered.scan(**kwargs)
        zips += [i["areaZip"] for i in page.get("Items", []) if helpers._norm_zip(i["areaZip"]) == i["areaZip"]]
        if "LastEvaluatedKey" not in page:
            return zips
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def dispatch(context, now=None):
    counts = active_zip_counts(now)
    try:
        viewed = viewed_area_zips(now)
    except (BotoCoreError, ClientError):
        logger.exception("Could not list viewed areas; using post ZIPs only")
        viewed = []
    for z in viewed:  # viewed areas count, even with no posts there yet
        counts[z] = counts.get(z, 0) + 1
    areas = group_areas(counts)
    started = 0
    for centre, members, _ in areas:
        try:
            lambda_client.invoke(FunctionName=context.invoked_function_arn, InvocationType="Event",
                                 Payload=json.dumps({"areaZip": centre, "zips": members}).encode())
            started += 1
        except (BotoCoreError, ClientError):
            logger.exception("Could not start area %s", centre)
    logger.info("Discover: %d area(s) started", started)
    return {"areas": started}


# ---------------------------------------------------------------------------
# Area run: ask Claude, check, save
# ---------------------------------------------------------------------------
def ask_claude(area_zip, avoid, today):
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_SEARCHES},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": MAX_FETCHES, "max_content_tokens": 8000},
        SAVE_TOOL,
    ]
    ask = (f"Today is {today:%A, %B %d, %Y}. Find up to {MAX_FINDS} new or limited food finds "
           f"this week near ZIP {area_zip}.")
    if avoid:
        ask += " These were shown last week, so skip them unless something about them is new: " + "; ".join(avoid[:20])
    messages = [{"role": "user", "content": ask}]
    usage = {"input": 0, "output": 0, "searches": 0}
    for _ in range(6):  # pause_turn resumes
        res = _claude().messages.create(model=MODEL, max_tokens=16000, output_config={"effort": "medium"},
                                        system=SYSTEM, tools=tools, messages=messages)
        usage["input"] += res.usage.input_tokens or 0
        usage["output"] += res.usage.output_tokens or 0
        stu = getattr(res.usage, "server_tool_use", None)
        usage["searches"] += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        saved = next((b for b in res.content if b.type == "tool_use" and b.name == "save_finds"), None)
        if saved:
            return saved.input.get("finds") or [], usage
        if res.stop_reason != "pause_turn":
            logger.warning("Area %s: no finds saved (stop_reason=%s)", area_zip, res.stop_reason)
            return [], usage
        messages.append({"role": "assistant", "content": res.content})
    return [], usage


def _clean(text, limit):
    return " ".join(str(text or "").split())[:limit]


def _specific_https(url):
    """The URL's host if it's https with a path beyond the homepage, else None."""
    try:
        u = urlparse(str(url or "").strip())
    except ValueError:
        return None
    if u.scheme != "https" or not u.netloc or len(str(url)) > 500:
        return None
    if not u.path.strip("/") and not u.query:
        return None
    host = u.netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def check_finds(finds, today, avoid_keys=()):
    """Keep the finds that pass every check, best first, at most MAX_FINDS."""
    kept, per_source, seen = [], {}, set(avoid_keys)
    for f in finds if isinstance(finds, list) else []:
        if not isinstance(f, dict) or f.get("confidence") not in ("high", "medium"):
            continue
        site = _specific_https(f.get("source_url"))
        if not site:
            continue
        try:
            src_date = date.fromisoformat(str(f.get("source_date")))
        except ValueError:
            continue
        if not (today - timedelta(days=MAX_SOURCE_AGE_DAYS) <= src_date <= today + timedelta(days=1)):
            continue
        row = {k: _clean(f.get(k), n) for k, n in LIMITS.items()}
        if not row["headline"] or not row["place"]:
            continue
        if any(content_filter.text_is_blocked(v) for v in row.values()):
            continue
        key = f"{row['place'].lower()}|{row['headline'].lower()}"
        url = str(f["source_url"]).strip()
        if key in seen or per_source.get(url, 0) >= MAX_PER_SOURCE:
            continue
        seen.add(key)
        per_source[url] = per_source.get(url, 0) + 1
        kept.append({**row, "sourceUrl": url, "sourceSite": site, "sourceDate": src_date.isoformat(), "key": key})
        if len(kept) >= MAX_FINDS:
            break
    return kept


def save_area(area_zip, members, finds, week, now_ts):
    """Replace this week's finds for the area and record what was shown."""
    week_id = int(week.strftime("%Y%m%d")) * 100
    old = helpers.query_all(discovered, KeyConditionExpression=Key("areaZip").eq(area_zip)
                            & Key("ID").between(week_id, week_id + 99)).get("Items", [])
    expires = now_ts + EXPIRE_DAYS * 86400
    new_ids = {week_id + n for n in range(1, len(finds) + 1)}
    with discovered.batch_writer() as batch:
        for item in old:  # leftovers from an earlier run this week (never a key rewritten below)
            if int(item["ID"]) not in new_ids:
                batch.delete_item(Key={"areaZip": area_zip, "ID": item["ID"]})
        for n, f in enumerate(finds, 1):
            batch.put_item(Item={"areaZip": area_zip, "ID": week_id + n, "weekOf": week.isoformat(),
                                 "createdAt": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
                                 "expiresAt": expires, "findKey": f["key"],
                                 "headline": f["headline"], "place": f["place"],
                                 "addressOrArea": f["address_or_area"], "what": f["what"], "when": f["when"],
                                 "whyNew": f["why_new"], "sourceUrl": f["sourceUrl"],
                                 "sourceSite": f["sourceSite"], "sourceDate": f["sourceDate"]})
    # The area row is updated, not replaced, so lastViewedAt (set by GET /discovered) survives.
    discovered.update_item(
        Key={"areaZip": area_zip, "ID": AREA_ROW},
        UpdateExpression="SET zips = :z, weekOf = :w, #c = :c, shownKeys = :k, updatedAt = :t, "
                         "expiresAt = if_not_exists(expiresAt, :e) REMOVE pending",
        ExpressionAttributeNames={"#c": "count"},
        ExpressionAttributeValues={":z": members or [area_zip], ":w": week.isoformat(), ":c": len(finds),
                                   ":k": [f["key"] for f in finds], ":t": now_ts,
                                   ":e": now_ts + VIEWED_DAYS * 86400})


def run_area(event, now=None):
    now = now or datetime.now(timezone.utc)
    area_zip = helpers._norm_zip(event.get("areaZip"))
    if not area_zip:
        logger.warning("Area run without a valid areaZip")
        return {"saved": 0}
    members = [z for z in (helpers._norm_zip(x) for x in event.get("zips") or []) if z][:2000]
    previous = discovered.get_item(Key={"areaZip": area_zip, "ID": AREA_ROW}).get("Item") or {}
    avoid_keys = list(previous.get("shownKeys") or [])
    today = now.date()
    raw, usage = ask_claude(area_zip, [k.replace("|", ": ") for k in avoid_keys], today)
    finds = check_finds(raw, today, avoid_keys)
    save_area(area_zip, members or previous.get("zips"), finds, week_start(today), int(now.timestamp()))
    logger.info("Discover area %s: %d of %d find(s) kept; %d searches, %d in / %d out tokens",
                area_zip, len(finds), len(raw), usage["searches"], usage["input"], usage["output"])
    return {"saved": len(finds)}


def lambda_handler(event, context):
    event = event or {}
    if event.get("areaZip"):
        return run_area(event)
    return dispatch(context)
