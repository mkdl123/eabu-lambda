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

Area run (that payload) saves up to about 36 cards for the week:
  - Top-rated places (Claude, MODEL, with web search): EAT_PLACES restaurants/cafes, plus
    LIFESTYLE_PER_TOPIC places for each Lifestyle topic (LIFESTYLE_TOPICS: fitness, yoga,
    beauty, pets and dog parks, all every week; dog parks since 2026-11-26). A place is kept only if Claude saw a
    rating of at least MIN_RATING with enough reviews on a specific https page, it passes the
    content filter, and it wasn't shown in the last SKIP_DAYS days, so each week brings a
    different set.
  - Events this week (Ticketmaster Discovery API, music and arts & theatre within AREA_MILES),
    up to MAX_EVENTS. Skipped while the key isn't in SSM.
Rows expire after EXPIRE_DAYS (DynamoDB TTL on expiresAt).

Table rows (eabu-discovered, key areaZip + ID):
  ID 0                  the area: centre ZIP, member ZIPs, weekOf, shownPlaces {name key: epoch}
  ID yyyymmdd*100 + n   one card of the week starting yyyymmdd (Monday, UTC)

Card fields: category (eat | lifestyle), topic, kind (place | event), plus the 1.7 fields
(headline, place, addressOrArea, what, when, whyNew, sourceUrl, sourceSite, sourceDate) so older
app builds still show eat places sensibly.

Only ZIP codes (and the ZIP's centre point, for Ticketmaster) leave AWS, never anything about
users. Run by hand for one area:
  aws lambda invoke --function-name <name> --invocation-type Event \
      --cli-binary-format raw-in-base64-out --payload '{"areaZip":"19060"}' /dev/null
"""
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
TM_KEY_PARAM = os.environ.get("TICKETMASTER_KEY_PARAM", "/eabu/ticketmaster-api-key")
TM_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
IMAGE_WIDTH = 640  # event picture size for the card thumbnail
MAX_AREAS = int(os.environ.get("DISCOVER_MAX_AREAS", "40"))
AREA_MILES = 25
POST_DAYS = 60
VIEWED_DAYS = 28
SKIP_DAYS = 14          # a place shown in the last 2 weeks isn't shown again
EAT_PLACES = 12
LIFESTYLE_PER_TOPIC = 5
MAX_EVENTS = 4
EVENT_DAYS = 7
MIN_RATING = 3.0
MIN_REVIEWS = {"eat": 50, "lifestyle": 15}   # small studios and vets have fewer reviews
TOPIC_MIN_REVIEWS = {"dogparks": 5}          # public parks get far fewer reviews than businesses
MAX_SEARCHES = 30
MAX_FETCHES = 16
MAX_PER_SOURCE = 3      # a "best yoga near X" list page may back a few places
EXPIRE_DAYS = 8
AREA_ROW = 0
POST_TABLES = [f"{c}_{r}" for c in ("EAT", "LIVE", "MOVE", "LIFESTYLE") for r in ("CONSUMER", "PROVIDER")]
LIMITS = {"name": 100, "place_type": 60, "address_or_area": 150, "what": 300, "rating_site": 40}

# (topic, what to search for). All of them every week.
LIFESTYLE_TOPICS = [
    ("fitness", "gyms and fitness studios (strength, boxing, cycling, martial arts)"),
    ("yoga", "yoga and pilates studios"),
    ("beauty", "hair salons, nail salons, barbers and day spas"),
    ("pets", "veterinarians, pet groomers, dog daycare and pet boarding"),
    ("dogparks", "dog parks and off-leash dog areas (public parks count; fenced areas preferred)"),
]
EAT_WHAT = ("restaurants of different cuisines (sit-down or takeout), with at most one cafe, bakery "
            "or dessert shop")

# The app's built-in stock photo for a card, by topic. Claude picks one per place; anything else is
# dropped and the app guesses from place_type. Keep in step with the app's components/stockPhotos.js.
PHOTO_KEYS = {
    "eat": ["italian", "indian", "chinese", "mexican", "japanese", "thai", "vietnamese", "korean",
            "mediterranean", "middle_eastern", "american", "bbq", "seafood", "french", "caribbean",
            "southern", "pizza", "cafe", "bakery", "dessert", "breakfast", "deli", "vegan", "restaurant"],
    "fitness": ["gym", "yoga", "boxing", "cycling", "swim"],
    "yoga": ["yoga", "gym"],
    "beauty": ["salon", "barber", "nails", "spa"],
    "pets": ["vet", "groomer", "pet"],
    "dogparks": ["pet"],
}
ALL_PHOTO_KEYS = sorted({k for keys in PHOTO_KEYS.values() for k in keys})

SAVE_TOOL = {
    "name": "save_places",
    "description": "Save the final list of top-rated places. Call this exactly once, after searching.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "places": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string", "enum": ["eat"] + [t for t, _ in LIFESTYLE_TOPICS]},
                        "name": {"type": "string", "description": "The business name"},
                        "place_type": {"type": "string", "description": "Short type, e.g. 'Thai restaurant', 'Yoga studio', 'Veterinarian'"},
                        "address_or_area": {"type": "string", "description": "Street address with number, town and state, e.g. '119 E Sellers Ave, Ridley Park, PA'; only the town if no street address is shown"},
                        "what": {"type": "string", "description": "One or two sentences in your own words on what people like about it"},
                        "rating": {"type": "number", "description": "The star rating exactly as shown on the source page, out of 5"},
                        "rating_count": {"type": "integer", "description": "Number of reviews shown with that rating"},
                        "rating_site": {"type": "string", "description": "Where the rating is from, e.g. 'Yelp', 'Tripadvisor', 'Google'"},
                        "source_url": {"type": "string", "description": "The page where you saw the rating"},
                        "photo": {"type": "string", "enum": ALL_PHOTO_KEYS,
                                  "description": "The closest picture for the card: for eat the cuisine or kind of place "
                                                 "('restaurant' if none fits); fitness gym/yoga/boxing/cycling/swim; "
                                                 "yoga yoga; beauty salon/barber/nails/spa; pets vet/groomer/pet; "
                                                 "dogparks pet"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": ["topic", "name", "place_type", "photo", "address_or_area", "what", "rating",
                                 "rating_count", "rating_site", "source_url", "confidence"],
                    "additionalProperties": False,
                },
            },
            "notes": {"type": "string"},
        },
        "required": ["places", "notes"],
        "additionalProperties": False,
    },
}

SYSTEM = """You pick top-rated local places for an app's "Discovered near you" feed.

Search the public web for well-reviewed places near the given postal code (a US ZIP code, or a 6-digit PIN code in
India) in the categories asked for.

Rules:
- Only places with a star rating of 3.0 or higher out of 5 AND a stated number of reviews, both of which
  you actually saw on a page (Yelp, Tripadvisor, a review site, or a list page that shows them; in India also
  Google Maps, Zomato or Justdial pages). Copy the
  rating and review count exactly. Never estimate, round up or guess a rating; if you can't see one, leave
  the place out.
- source_url is the page that shows that rating: a business page or a list page, never a site's homepage.
- Within about 25 miles (40 km) of the postal code. Prefer independent local businesses; at most one national chain per
  category, and only if it is clearly loved locally.
- The place must still be open (skip anything marked closed or permanently closed).
- Dog parks are usually public parks rather than businesses: the independent-business and chain rule doesn't
  apply to them. Give the park's street address or entrance, and say in "what" whether it is fenced, has
  separate areas for small and large dogs, water or shade, when the page shows it.
- Spread your picks across the area, not all on one street, and across different kinds within a category.
- Write descriptions in your own words; don't copy review text. No prices or promises you didn't see.
- Give each place's street address when the page shows it (it puts the place on the app's map).
- Nothing sexual: no adult stores, strip clubs or "massage" businesses of doubtful kind. No bars whose main
  business is alcohol, no smoke or vape shops, no cannabis dispensaries.
- Save fetches for places you'll include. Fewer solid picks beat more weak ones.
- Try to fill each category: if you have fewer places than asked for, search again (another cuisine or
  kind of place, or another town nearby) before stopping. Every pick must still meet the rules above.

When done searching, call save_places once with your picks, best first within each category."""

_client = None
_tm_key = None


def _claude():
    """The Anthropic client, with the key from SSM (SecureString), made once per container."""
    global _client
    if _client is None:
        import anthropic  # only area runs need it
        key = ssm.get_parameter(Name=API_KEY_PARAM, WithDecryption=True)["Parameter"]["Value"]
        _client = anthropic.Anthropic(api_key=key, timeout=480, max_retries=1)
    return _client


def _ticketmaster_key():
    """The Ticketmaster key from SSM, or "" while it hasn't been stored (events are skipped)."""
    global _tm_key
    if _tm_key is None:
        try:
            _tm_key = ssm.get_parameter(Name=TM_KEY_PARAM, WithDecryption=True)["Parameter"]["Value"].strip()
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ParameterNotFound":
                logger.warning("Could not read the Ticketmaster key")
                return ""
            _tm_key = ""
    return _tm_key


# ---------------------------------------------------------------------------
# Weekly run: areas
# ---------------------------------------------------------------------------
def week_start(today=None):
    today = today or datetime.now(timezone.utc).date()
    return today - timedelta(days=today.weekday())


def topics_for_week(week):
    """This week's Lifestyle topics: all of them every week (until 2026-11-23 they alternated two by two)."""
    return list(LIFESTYLE_TOPICS)


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
# Area run, places: ask Claude, check
# ---------------------------------------------------------------------------
def min_reviews(topic):
    """Fewest reviews a place in this topic needs."""
    if topic in TOPIC_MIN_REVIEWS:
        return TOPIC_MIN_REVIEWS[topic]
    return MIN_REVIEWS["eat" if topic == "eat" else "lifestyle"]


def build_ask(area_zip, topics, avoid, today):
    where = f"PIN code {area_zip}, India" if helpers.country_of_zip(area_zip) == "IN" else f"ZIP {area_zip}"
    lines = [f"Today is {today:%A, %B %d, %Y}. Find top-rated places near {where}:",
             f"- eat: up to {EAT_PLACES} {EAT_WHAT} (at least {MIN_REVIEWS['eat']} reviews)"]
    lines += [f"- {t}: up to {LIFESTYLE_PER_TOPIC} {what} (at least {min_reviews(t)} reviews)"
              for t, what in topics]
    if avoid:
        lines.append("These were shown recently, so pick different places: " + "; ".join(avoid[:40]))
    return "\n".join(lines)


def ask_claude(area_zip, topics, avoid, today):
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_SEARCHES},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": MAX_FETCHES, "max_content_tokens": 8000},
        SAVE_TOOL,
    ]
    messages = [{"role": "user", "content": build_ask(area_zip, topics, avoid, today)}]
    usage = {"input": 0, "output": 0, "searches": 0}
    for _ in range(6):  # pause_turn resumes
        res = _claude().messages.create(model=MODEL, max_tokens=24000, output_config={"effort": "medium"},
                                        system=SYSTEM, tools=tools, messages=messages)
        usage["input"] += res.usage.input_tokens or 0
        usage["output"] += res.usage.output_tokens or 0
        stu = getattr(res.usage, "server_tool_use", None)
        usage["searches"] += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        saved = next((b for b in res.content if b.type == "tool_use" and b.name == "save_places"), None)
        if saved:
            return saved.input.get("places") or [], usage
        if res.stop_reason != "pause_turn":
            logger.warning("Area %s: no places saved (stop_reason=%s)", area_zip, res.stop_reason)
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


def place_key(name):
    """'Joe's Café & Bar' -> 'joes cafe bar': how a place is recognised from week to week."""
    import unicodedata
    plain = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", plain.replace("'", "")).split())[:80]


def maps_url(name, address):
    """A plain Google Maps search link (no API, no key)."""
    q = ", ".join(x for x in (name, address) if x)
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(q)


CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
MAX_PIN_MILES = 40   # a geocoded pin this far from the area centre is a wrong match


def _miles(lat1, lon1, lat2, lon2):
    import math
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3958.8 * 2 * math.asin(min(1.0, math.sqrt(h)))


def pin_fields(lat, lng, area_zip):
    """{"addressLat", "addressLng"} (strings, 6 places) for the map, or {} when the point is
    missing, invalid or implausibly far from the area."""
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return {}
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return {}
    centre = helpers._zip_centroids().get(area_zip)
    if centre and _miles(lat, lng, centre[0], centre[1]) > MAX_PIN_MILES:
        return {}
    return {"addressLat": f"{lat:.6f}", "addressLng": f"{lng:.6f}"}


def geocode(address, area_zip):
    """Map pin for a street address via the US Census geocoder (free, no key; public data).
    {} for a town-only address (no house number) or no match: better no pin than a wrong one."""
    if helpers.country_of_zip(area_zip) != "US":   # the Census geocoder only knows US addresses
        return {}
    if not re.match(r"^\s*\d+[A-Za-z]?\s+\S", str(address or "")):
        return {}
    url = f"{CENSUS_URL}?{urllib.parse.urlencode({'address': address, 'benchmark': 'Public_AR_Current', 'format': 'json'})}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=10) as res:
            matches = ((json.load(res) or {}).get("result") or {}).get("addressMatches") or []
    except (urllib.error.URLError, TimeoutError, ValueError):
        logger.warning("Census geocoder failed for an address in %s", area_zip)
        return {}
    if not matches:
        return {}
    c = matches[0].get("coordinates") or {}
    return pin_fields(c.get("y"), c.get("x"), area_zip)


def _rating(value):
    try:
        r = round(float(value), 1)
    except (TypeError, ValueError):
        return None
    return r if MIN_RATING <= r <= 5.0 else None


def check_places(places, topics, recent=(), today=None):
    """Keep the places that pass every check, in Claude's order, within each topic's cap."""
    caps = {"eat": EAT_PLACES, **{t: LIFESTYLE_PER_TOPIC for t, _ in topics}}
    kept, per_topic, per_source, seen = [], {}, {}, set(recent)
    for p in places if isinstance(places, list) else []:
        if not isinstance(p, dict) or p.get("confidence") not in ("high", "medium"):
            continue
        topic = p.get("topic")
        if topic not in caps or per_topic.get(topic, 0) >= caps[topic]:
            continue
        category = "eat" if topic == "eat" else "lifestyle"
        rating = _rating(p.get("rating"))
        try:
            count = int(p.get("rating_count"))
        except (TypeError, ValueError):
            continue
        if rating is None or count < min_reviews(topic):
            continue
        site = _specific_https(p.get("source_url"))
        if not site:
            continue
        row = {k: _clean(p.get(k), n) for k, n in LIMITS.items()}
        if not row["name"] or any(content_filter.text_is_blocked(v) for v in row.values()):
            continue
        key = place_key(row["name"])
        url = str(p["source_url"]).strip()
        if not key or key in seen or per_source.get(url, 0) >= MAX_PER_SOURCE:
            continue
        seen.add(key)
        per_source[url] = per_source.get(url, 0) + 1
        per_topic[topic] = per_topic.get(topic, 0) + 1
        photo = p.get("photo") if p.get("photo") in PHOTO_KEYS.get(topic, ()) else ""
        kept.append({**row, "topic": topic, "category": category, "rating": rating, "ratingCount": count,
                     "photo": photo,
                     "sourceUrl": url, "sourceSite": site, "key": key})
    order = ["eat"] + [t for t, _ in topics]
    return sorted(kept, key=lambda k: order.index(k["topic"]))  # stable: Claude's order within a topic


def place_row(p, today):
    """The saved card for a place, with the 1.7 fields filled in for older app builds."""
    site = p["rating_site"] or p["sourceSite"]
    rated = f"Rated {p['rating']} on {site} ({p['ratingCount']:,} reviews)"
    return {"category": p["category"], "topic": p["topic"], "kind": "place",
            "headline": f"⭐ {p['rating']} · {p['place_type'] or 'Local favourite'}",
            "place": p["name"], "addressOrArea": p["address_or_area"], "what": p["what"], "when": "",
            "whyNew": rated, "sourceUrl": p["sourceUrl"], "sourceSite": p["sourceSite"],
            "sourceDate": today.isoformat(), "placeType": p["place_type"], "rating": Decimal(str(p["rating"])),
            "ratingCount": p["ratingCount"], "ratingSite": site,
            "mapsUrl": maps_url(p["name"], p["address_or_area"]), "photo": p.get("photo", ""),
            "findKey": p["key"]}


# ---------------------------------------------------------------------------
# Area run, events: Ticketmaster
# ---------------------------------------------------------------------------
def fetch_events(area_zip, now):
    """Raw Ticketmaster events (music, arts & theatre) within AREA_MILES over the next EVENT_DAYS days."""
    key = _ticketmaster_key()
    centre = helpers._zip_centroids().get(area_zip)
    if not key or not centre or helpers.country_of_zip(area_zip) != "US":   # Ticketmaster: US only
        return []
    params = {
        "apikey": key, "latlong": f"{centre[0]:.4f},{centre[1]:.4f}", "radius": str(AREA_MILES), "unit": "miles",
        "startDateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": (now + timedelta(days=EVENT_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "classificationName": "music,arts & theatre", "sort": "date,asc", "size": "100", "locale": "*",
    }
    req = urllib.request.Request(f"{TM_URL}?{urllib.parse.urlencode(params)}", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            body = json.load(res)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        logger.warning("Ticketmaster request for %s failed: %s", area_zip, type(e).__name__)
        return []
    return ((body or {}).get("_embedded") or {}).get("events") or []


def _event_when(start):
    try:
        d = datetime.strptime(start.get("localDate", ""), "%Y-%m-%d")
    except ValueError:
        return None, ""
    when = f"{d:%a, %b} {d.day}"
    if start.get("localTime") and not start.get("timeTBA"):
        try:
            t = datetime.strptime(start["localTime"][:5], "%H:%M")
            when += " · " + t.strftime("%I:%M %p").lstrip("0")
        except ValueError:
            pass
    return d.date(), when


def event_image(e):
    """The event's 16:9 Ticketmaster picture closest to IMAGE_WIDTH px wide (https only), else "".

    Ticketmaster's API terms allow showing these with the event; the app hotlinks the URL."""
    best, best_gap = "", None
    for img in e.get("images") or []:
        if not isinstance(img, dict) or img.get("ratio") != "16_9" or img.get("fallback"):
            continue
        url = str(img.get("url") or "")
        if not url.startswith("https://") or len(url) > 500:
            continue
        try:
            gap = abs(int(img.get("width") or 0) - IMAGE_WIDTH)
        except (TypeError, ValueError):
            continue
        if best_gap is None or gap < best_gap:
            best, best_gap = url, gap
    return best


def pick_events(raw, recent=(), today=None, area_zip=None):
    """Up to MAX_EVENTS cards: one per show name and per venue, soonest first, filtered."""
    cards, names, venues = [], set(recent), set()
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict) or (e.get("dates") or {}).get("status", {}).get("code") in ("cancelled", "offsale"):
            continue
        name = _clean(e.get("name"), 100)
        url = str(e.get("url") or "")
        venue = (((e.get("_embedded") or {}).get("venues") or [{}])[0]) or {}
        venue_name = _clean(venue.get("name"), 100)
        day, when = _event_when((e.get("dates") or {}).get("start") or {})
        if not name or not day or not _specific_https(url):
            continue
        key = place_key(name)
        if not key or key in names or (venue_name and venue_name in venues):
            continue
        cls = (e.get("classifications") or [{}])[0] or {}
        kind = " · ".join(x for x in (_clean((cls.get("segment") or {}).get("name"), 30),
                                       _clean((cls.get("genre") or {}).get("name"), 30)) if x and x != "Undefined")
        town = ", ".join(x for x in (_clean((venue.get("city") or {}).get("name"), 60),
                                     _clean((venue.get("state") or {}).get("stateCode"), 4)) if x)
        address = ", ".join(x for x in (_clean((venue.get("address") or {}).get("line1"), 100), town) if x)
        if any(content_filter.text_is_blocked(v) for v in (name, venue_name, kind)):
            continue
        names.add(key)
        if venue_name:
            venues.add(venue_name)
        local_time = (((e.get("dates") or {}).get("start") or {}).get("localTime") or "")[:5]
        loc = venue.get("location") or {}
        cards.append({"category": "lifestyle", "topic": "events", "kind": "event",
                      "headline": name, "place": venue_name or town, "addressOrArea": address or town,
                      "what": kind or "Live event", "when": when, "whyNew": "Coming up this week",
                      "sourceUrl": url, "sourceSite": "ticketmaster.com",
                      "sourceDate": (today or day).isoformat(), "eventDate": day.isoformat(),
                      "eventTime": local_time, "ticketUrl": url, "imageUrl": event_image(e),
                      "mapsUrl": maps_url(venue_name, address or town), "findKey": key,
                      **(pin_fields(loc.get("latitude"), loc.get("longitude"), area_zip) if area_zip else {})})
        if len(cards) >= MAX_EVENTS:
            break
    return cards


# ---------------------------------------------------------------------------
# Area run: save
# ---------------------------------------------------------------------------
def recent_keys(previous, now_ts):
    """Place keys shown in the last SKIP_DAYS days (from the area row's shownPlaces map)."""
    shown = previous.get("shownPlaces") or {}
    return {k: int(v) for k, v in shown.items() if int(v) >= now_ts - SKIP_DAYS * 86400}


def save_area(area_zip, members, cards, week, now_ts, shown):
    """Replace this week's cards for the area and record which places were shown."""
    week_id = int(week.strftime("%Y%m%d")) * 100
    old = helpers.query_all(discovered, KeyConditionExpression=Key("areaZip").eq(area_zip)
                            & Key("ID").between(week_id, week_id + 99)).get("Items", [])
    expires = now_ts + EXPIRE_DAYS * 86400
    created = datetime.fromtimestamp(now_ts, timezone.utc).isoformat()
    new_ids = {week_id + n for n in range(1, len(cards) + 1)}
    with discovered.batch_writer() as batch:
        for item in old:  # leftovers from an earlier run this week (never a key rewritten below)
            if int(item["ID"]) not in new_ids:
                batch.delete_item(Key={"areaZip": area_zip, "ID": item["ID"]})
        for n, c in enumerate(cards, 1):
            batch.put_item(Item={"areaZip": area_zip, "ID": week_id + n, "weekOf": week.isoformat(),
                                 "createdAt": created, "expiresAt": expires, **c})
    # The area row is updated, not replaced, so lastViewedAt (set by GET /discovered) survives.
    discovered.update_item(
        Key={"areaZip": area_zip, "ID": AREA_ROW},
        UpdateExpression="SET zips = :z, weekOf = :w, #c = :c, shownPlaces = :k, updatedAt = :t, "
                         "expiresAt = if_not_exists(expiresAt, :e) REMOVE pending, shownKeys",
        ExpressionAttributeNames={"#c": "count"},
        ExpressionAttributeValues={":z": members or [area_zip], ":w": week.isoformat(), ":c": len(cards),
                                   ":k": shown, ":t": now_ts, ":e": now_ts + VIEWED_DAYS * 86400})


def run_area(event, now=None):
    now = now or datetime.now(timezone.utc)
    area_zip = helpers._norm_zip(event.get("areaZip"))
    if not area_zip:
        logger.warning("Area run without a valid areaZip")
        return {"saved": 0}
    members = [z for z in (helpers._norm_zip(x) for x in event.get("zips") or []) if z][:2000]
    previous = discovered.get_item(Key={"areaZip": area_zip, "ID": AREA_ROW}).get("Item") or {}
    now_ts, today = int(now.timestamp()), now.date()
    week = week_start(today)
    shown = recent_keys(previous, now_ts)
    # A re-run in the same week may pick this week's places again.
    this_week = {k for k, v in shown.items() if v >= int(datetime.combine(week, datetime.min.time(), timezone.utc).timestamp())}
    recent = set(shown) - this_week

    events = pick_events(fetch_events(area_zip, now), today=today, area_zip=area_zip)
    topics = topics_for_week(week)
    avoid = sorted(recent)
    raw, usage = ask_claude(area_zip, topics, avoid, today)
    places = [{**place_row(p, today), **geocode(p["address_or_area"], area_zip)}
              for p in check_places(raw, topics, recent, today)]
    cards = places + events
    for c in places:
        shown[c["findKey"]] = now_ts
    save_area(area_zip, members or previous.get("zips"), cards, week, now_ts, shown)
    pinned = sum(1 for c in cards if c.get("addressLat"))
    logger.info("Discover area %s: %d of %d place(s) kept, %d event(s), %d on the map; %d searches, %d in / %d out tokens",
                area_zip, len(places), len(raw), len(events), pinned, usage["searches"], usage["input"], usage["output"])
    return {"saved": len(cards)}


def lambda_handler(event, context):
    event = event or {}
    if event.get("areaZip"):
        return run_area(event)
    return dispatch(context)
