import os
import json
import base64
import functools
import hashlib
import math
import re
import logging
from datetime import datetime, timezone
import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from my_utils import content_filter

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Longest value accepted for each client-supplied field.
FIELD_LIMITS = {
    "zipcode": 10, "zip": 10, "userId": 128, "pUserId": 128, "cUserId": 128,
    "title": 200, "description": 5000, "offerDescription": 5000, "message": 5000,
    "phone": 32, "phoneNumber": 32, "email": 254, "price": 32, "unit": 40, "serviceType": 100,
    "latitude": 32, "longitude": 32, "address": 200, "addressLat": 32, "addressLng": 32,
}
COORDINATE_FIELDS = ("latitude", "longitude")
# Where the poster's optional address is (looked up by the app). Unlike latitude/longitude
# (the poster's phone), these are never rounded: the poster chose to publish the address.
ADDRESS_COORDINATE_FIELDS = ("addressLat", "addressLng")
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
    _validate_content(payload)


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")


def seller_contact(payload: dict, phone_key: str = "phone", email_key: str = "email"):
    """A seller's (phone, email): either may be left out, but not both (stripped strings)."""
    phone = str(payload.get(phone_key) or "").strip()
    email = str(payload.get(email_key) or "").strip()
    if not phone and not email:
        raise ValueError("Enter a phone number or an email address, so buyers can reach you")
    if email and not EMAIL_RE.match(email):
        raise ValueError("Enter a valid email address")
    return phone, email


def _validate_content(payload: dict):
    """Reject sexual or adult words in any user-visible text (see content_filter)."""
    field = content_filter.blocked_text_field(payload)
    if field:
        # The message names the field, never the matched word, so it's safe to log.
        raise ValueError(content_filter.blocked_text_message(field))


def _validate_fields(payload: dict):
    """Reject values that are too long, or of a type DynamoDB can't store (floats, lists...)."""
    for field, limit in FIELD_LIMITS.items():
        value = payload.get(field)
        if value is None:
            continue
        numeric = field in COORDINATE_FIELDS or field in ADDRESS_COORDINATE_FIELDS
        allowed = (str, int, float) if numeric else (str, int)
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
# Per-user read limit (anti-scraping)
#
# The read routes that show other people's posts and profiles (category lists, Home feed,
# Discovered, profiles, ratings, ads) count each signed-in caller's requests per UTC hour in
# the eabu-rate-limit table (key "<userId>#<yyyymmddhh>", TTL expiresAt). Past
# READ_LIMIT_PER_HOUR the caller gets 429 RATE_LIMITED until the next hour. Guests (the public
# /guest/... routes) are counted per IP address instead ("ip#<address>#<hour>"). A real user never
# gets near it; a script copying the feed is stopped quickly. If the counter can't be updated
# the request goes through (never break the app over the limiter).
# ---------------------------------------------------------------------------
_rate_table = None


def _rate_limit_table():
    global _rate_table
    if _rate_table is None:
        _rate_table = boto3.resource("dynamodb", region_name=os.environ.get("REGION_NAME", "us-east-1")).Table(
            os.environ.get("RATE_LIMIT_TABLE_NAME", "eabu-rate-limit"))
    return _rate_table


def _int_env(name, default) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def read_limit_per_hour() -> int:
    return _int_env("READ_LIMIT_PER_HOUR", 600)


def guest_limit_per_hour() -> int:
    """Reads per IP address per hour on the guest routes (no account to count against). Higher
    than the per-user limit because phone carriers put many people behind one address."""
    return _int_env("GUEST_LIMIT_PER_HOUR", 1200)


def is_guest_route(event) -> bool:
    """True for the public /guest/... routes (browsing before sign-up; no Cognito authorizer)."""
    return str((event or {}).get("resource") or "").startswith("/guest/")


def source_ip(event):
    return (((event or {}).get("requestContext") or {}).get("identity") or {}).get("sourceIp") or ""


def over_read_limit(user_id, now=None, limit=None) -> bool:
    """Count one read for user_id this hour; True once they're past the hourly limit."""
    if not user_id:
        return False
    now = now or datetime.now(timezone.utc)
    hour = now.strftime("%Y%m%d%H")
    try:
        res = _rate_limit_table().update_item(
            Key={"key": f"{str(user_id).lower()}#{hour}"},
            UpdateExpression="ADD n :one SET expiresAt = if_not_exists(expiresAt, :exp)",
            ExpressionAttributeValues={":one": 1, ":exp": int(now.timestamp()) + 2 * 3600},
            ReturnValues="UPDATED_NEW",
        )
        return int(res["Attributes"]["n"]) > (limit or read_limit_per_hour())
    except Exception:  # noqa: BLE001 - fail open, never block real users over the limiter
        logger.warning("Read limit check failed")
        return False


def rate_limited(handler):
    """Answer 429 RATE_LIMITED when the signed-in caller (or, on a guest route, the caller's IP
    address) is past their hourly read limit."""
    @functools.wraps(handler)
    def wrapper(event, context):
        caller = get_caller_id(event)
        if caller:
            over = over_read_limit(caller)
        elif is_guest_route(event) and source_ip(event):
            over = over_read_limit(f"ip#{source_ip(event)}", limit=guest_limit_per_hour())
        else:
            over = False
        if over:
            logger.info("Read limit reached")
            return _response(429, {"error": "Too many requests. Please try again later.",
                                   "code": "RATE_LIMITED"})
        return handler(event, context)
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


def get_caller_email(event):
    """The signed-in caller's account email from the verified Cognito ID token, or None."""
    authorizer = ((event or {}).get("requestContext") or {}).get("authorizer") or {}
    claims = authorizer.get("claims") or (authorizer.get("jwt") or {}).get("claims") or {}
    value = claims.get("email")
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
                         "address", "addressLat", "addressLng", "picture1", "picture2", "picture3", "picture4", "pictures",
                         "totalstars", "ratingscount", "createdAt")


# Sellers may add up to four pictures to each service listing. They live in the
# (private) picture bucket at listings/{category}/{ID}/{slot}.jpg, and the listing
# row records each one as picture1..picture4 = upload time.
LISTING_CATEGORIES = ("eat", "live", "move", "lifestyle")
PICTURE_SLOTS = ("1", "2", "3", "4")
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


def refresh_listing_pictures(item, category: str):
    """Restart the expiry clock on a listing's pictures after a repost.

    The bucket deletes listings/ objects POST_TTL_DAYS after they were last written (S3
    lifecycle), matching the listing's own expiry. Copying a picture onto itself counts as a
    write, so a reposted listing keeps its pictures. Failures are logged, not raised: the
    repost itself has already succeeded."""
    global _s3_client
    bucket = os.environ.get("IDENTITY_BUCKET_NAME", "eabu-identity-pictures")
    for slot in PICTURE_SLOTS:
        if not item.get(f"picture{slot}") or "ID" not in item:
            continue
        if _s3_client is None:
            _s3_client = boto3.client("s3", region_name=os.environ.get("REGION_NAME", "us-east-1"))
        key = listing_picture_key(category, item["ID"], slot)
        try:
            _s3_client.copy_object(Bucket=bucket, Key=key, CopySource={"Bucket": bucket, "Key": key},
                                   MetadataDirective="REPLACE", ContentType="image/jpeg")
        except (BotoCoreError, ClientError, TypeError, ValueError):
            logger.exception("Could not refresh a listing picture")


# Posts in the 8 consumer/provider tables carry expiresAt (epoch seconds); DynamoDB TTL
# deletes them that long after they were created or last reposted, and the picture bucket's
# lifecycle rule deletes listings/ pictures on the same schedule.
POST_TTL_DAYS = 60

# After an account is deleted its email can't be used to sign up again for this long.
# The block stores only a SHA-256 of the normalised email, never the address itself.
EMAIL_BLOCK_DAYS = 30


def email_hash(email: str) -> str:
    return hashlib.sha256(str(email).strip().lower().encode("utf-8")).hexdigest()


def post_expires_at(now=None) -> int:
    now = now or datetime.now(timezone.utc)
    return int(now.timestamp()) + POST_TTL_DAYS * 86400


# A seller may post this many services each calendar month for free, in total across
# all categories; the count resets on the 1st (US Eastern time). The server enforces it.
MONTHLY_POST_LIMIT = 10
POST_LIMIT_TIMEZONE = "America/New_York"


def _month_start(now=None):
    """Midnight on the 1st of the current month, US Eastern."""
    from zoneinfo import ZoneInfo
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(POST_LIMIT_TIMEZONE))
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def post_limit_cutoff(now=None) -> str:
    """createdAt values at or after this string count toward this month's free posts.

    createdAt is UTC (ISO 8601, with or without "+00:00"), so the cutoff is given in UTC
    and the strings compare correctly."""
    return _month_start(now).astimezone(timezone.utc).isoformat()


def free_posts_reset_on(now=None) -> str:
    """The date the free posts next reset, e.g. "2026-12-01"."""
    start = _month_start(now)
    return (start.replace(year=start.year + 1, month=1) if start.month == 12
            else start.replace(month=start.month + 1)).date().isoformat()


# The seller post tables; together they hold everything that counts toward the free posts.
PROVIDER_TABLE_ENV = {
    "eat": ("EAT_PROVIDER_TABLE_NAME", "eabu-eat-provider"),
    "live": ("LIVE_PROVIDER_TABLE_NAME", "eabu-live-provider"),
    "move": ("MOVE_PROVIDER_TABLE_NAME", "eabu-move-provider"),
    "lifestyle": ("LIFESTYLE_PROVIDER_TABLE_NAME", "eabu-lifestyle-provider"),
}
_provider_tables = {}


def provider_tables() -> dict:
    """{category: Table} for the four seller post tables (created once per container)."""
    if not _provider_tables:
        ddb = boto3.resource("dynamodb", region_name=os.environ.get("REGION_NAME", "us-east-1"))
        for category, (env, default) in PROVIDER_TABLE_ENV.items():
            _provider_tables[category] = ddb.Table(os.environ.get(env, default))
    return _provider_tables


def monthly_post_counts(user_id: str) -> dict:
    """This month's posts per category, e.g. {"eat": 3, "live": 0, "move": 1, "lifestyle": 0}."""
    return {category: recent_post_count(t, user_id) for category, t in provider_tables().items()}


def recent_post_count(table, user_id: str) -> int:
    """How many listings this user created in the table this month."""
    from boto3.dynamodb.conditions import Attr
    cutoff = post_limit_cutoff()
    kwargs = {
        "IndexName": "userId-index",
        "KeyConditionExpression": Key("userId").eq(str(user_id)),
        "FilterExpression": Attr("createdAt").gte(cutoff),
        "Select": "COUNT",
    }
    count = 0
    while True:
        page = table.query(**kwargs)
        count += page.get("Count", 0)
        if "LastEvaluatedKey" not in page:
            return count
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def post_limit_reached(table, user_id: str):
    """None while the user may still post here, else a 429 response.

    Past the free limit, a post uses one of the user's extra posts (bought in packs of
    POSTS_PER_PACK with Apple in-app purchase) when they have any."""
    return post_allowance(table, user_id)[0]


# Extra posts: a seller past the free monthly limit spends one per post. They are
# bought in packs (the $0.99 consumable `net.eabu.app.posts15`), never expire and
# work in every category. One row per user {userId, balance}; each credited Apple
# transaction is also stored ({userId: "tx#<id>"}) so it is never counted twice.
POSTS_PER_PACK = 15
_credits_table = None


def post_credits_table():
    global _credits_table
    if _credits_table is None:
        _credits_table = boto3.resource("dynamodb", region_name=os.environ.get("REGION_NAME", "us-east-1")).Table(
            os.environ.get("POST_CREDITS_TABLE_NAME", "eabu-post-credits"))
    return _credits_table


def _credits_row(user_id: str) -> dict:
    return post_credits_table().get_item(Key={"userId": str(user_id)}).get("Item") or {}


def post_credit_balance(user_id: str) -> int:
    return int(_credits_row(user_id).get("balance") or 0)


def free_post_limit(user_id: str) -> int:
    """The user's free posts per month (all categories). A `freeLimit` on their credits row
    overrides MONTHLY_POST_LIMIT (set by hand, e.g. 1 for a test account)."""
    try:
        override = _credits_row(user_id).get("freeLimit")
    except (BotoCoreError, ClientError):
        logger.exception("Could not read the free post limit; using the default")
        return MONTHLY_POST_LIMIT
    return int(override) if override is not None else MONTHLY_POST_LIMIT


def spend_post_credit(user_id: str) -> bool:
    """Take one extra post from the user's balance; False when they have none."""
    try:
        post_credits_table().update_item(
            Key={"userId": str(user_id)},
            UpdateExpression="ADD balance :minus",
            ConditionExpression="balance >= :one",
            ExpressionAttributeValues={":minus": -1, ":one": 1})
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def refund_post_credit(user_id: str) -> None:
    """Give back an extra post spent on a post that then failed to save."""
    try:
        post_credits_table().update_item(Key={"userId": str(user_id)},
                                         UpdateExpression="ADD balance :one",
                                         ExpressionAttributeValues={":one": 1})
    except (BotoCoreError, ClientError):
        logger.exception("Could not refund an extra post")


# Invite friends: a new user may name who invited them at sign-up (eabu-pre-signup saves
# {userId: "ref#<new user, lower case>", invitedBy, emailHash} in the credits table). When
# the new user's first post or request is saved, both get REFERRAL_POSTS extra posts. The
# inviter is rewarded at most REFERRAL_MONTHLY_CAP times a calendar month (UTC), and each
# email earns the reward once ever ({userId: "refemail#<hash>"}, kept after deletion).
# COMMUNITY_BUILDER_INVITES successful invites earn the Community Builder badge.
REFERRAL_POSTS = 5
REFERRAL_MONTHLY_CAP = 10
COMMUNITY_BUILDER_INVITES = 3


def referral_key(user_id) -> str:
    return f"ref#{str(user_id).strip().lower()}"


def _conditional_failed(e) -> bool:
    return isinstance(e, ClientError) and e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _credit_inviter(inviter: str, month: str) -> bool:
    """Give the inviter REFERRAL_POSTS unless they reached this month's cap."""
    table = post_credits_table()
    values = {":n": REFERRAL_POSTS, ":one": 1, ":m": month}
    try:
        table.update_item(
            Key={"userId": inviter},
            UpdateExpression="ADD balance :n, referralPosts :n, invitesTotal :one, invitesThisMonth :one",
            ConditionExpression="invitesMonth = :m AND invitesThisMonth < :cap",
            ExpressionAttributeValues={**values, ":cap": REFERRAL_MONTHLY_CAP})
        return True
    except ClientError as e:
        if not _conditional_failed(e):
            raise
    try:
        # First reward this month (or ever): start the month's count.
        table.update_item(
            Key={"userId": inviter},
            UpdateExpression="ADD balance :n, referralPosts :n, invitesTotal :one "
                             "SET invitesMonth = :m, invitesThisMonth = :one",
            ConditionExpression="attribute_not_exists(invitesMonth) OR invitesMonth <> :m",
            ExpressionAttributeValues=values)
        return True
    except ClientError as e:
        if _conditional_failed(e):
            return False  # this month's cap is reached
        raise


def reward_referral(user_id: str) -> int:
    """Call after a user's post or request is saved. Returns the extra posts the user just
    got for having been invited (0 almost always). Never raises: a failed reward must
    never fail the post."""
    table = post_credits_table()
    now = datetime.now(timezone.utc)
    try:
        ref = table.update_item(
            Key={"userId": referral_key(user_id)},
            UpdateExpression="SET rewardedAt = :now",
            ConditionExpression="attribute_exists(invitedBy) AND attribute_not_exists(rewardedAt)",
            ExpressionAttributeValues={":now": now.isoformat()},
            ReturnValues="ALL_NEW")["Attributes"]
    except ClientError as e:
        if not _conditional_failed(e):  # not invited, or already rewarded: the usual case
            logger.exception("Referral lookup failed")
        return 0
    except BotoCoreError:
        logger.exception("Referral lookup failed")
        return 0
    try:
        if ref.get("emailHash"):
            try:
                table.update_item(Key={"userId": f"refemail#{ref['emailHash']}"},
                                  UpdateExpression="SET usedAt = :now",
                                  ConditionExpression="attribute_not_exists(usedAt)",
                                  ExpressionAttributeValues={":now": now.isoformat()})
            except ClientError as e:
                if _conditional_failed(e):
                    logger.info("Referral skipped: this email was already rewarded")
                    return 0
                raise
        table.update_item(Key={"userId": str(user_id)},
                          UpdateExpression="ADD balance :n, referralPosts :n",
                          ExpressionAttributeValues={":n": REFERRAL_POSTS})
        inviter_paid = _credit_inviter(str(ref["invitedBy"]), now.strftime("%Y-%m"))
        table.update_item(Key={"userId": referral_key(user_id)},
                          UpdateExpression="SET inviterRewarded = :b",
                          ExpressionAttributeValues={":b": inviter_paid})
        logger.info("Referral rewarded (inviter credited: %s)", inviter_paid)
        return REFERRAL_POSTS
    except (BotoCoreError, ClientError):
        logger.exception("Referral reward failed part way")
        return 0


def invite_stats(user_id: str) -> dict:
    """For GET /post-credits: the user's invite progress."""
    row = _credits_row(user_id)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    total = int(row.get("invitesTotal") or 0)
    return {"invitesTotal": total,
            "invitesThisMonth": int(row.get("invitesThisMonth") or 0) if row.get("invitesMonth") == month else 0,
            "inviteMonthlyCap": REFERRAL_MONTHLY_CAP, "referralPosts": REFERRAL_POSTS,
            "communityBuilderAt": COMMUNITY_BUILDER_INVITES,
            "communityBuilder": total >= COMMUNITY_BUILDER_INVITES}


def is_community_builder(user_id: str) -> bool:
    try:
        return int(_credits_row(user_id).get("invitesTotal") or 0) >= COMMUNITY_BUILDER_INVITES
    except (BotoCoreError, ClientError):
        logger.exception("Could not read invite count")
        return False


def post_allowance(table, user_id: str):
    """(None, spent) while the user may post here, else (429 or 500 response, False).

    The free posts are counted across all categories, so `table` (the category being
    posted to) doesn't change the answer. `spent` is True when the post uses an extra
    post; if saving the post then fails, call refund_post_credit so the user doesn't
    lose it."""
    try:
        limit = free_post_limit(user_id)
        if sum(monthly_post_counts(user_id).values()) < limit:
            return None, False
        if spend_post_credit(user_id):
            return None, True
    except (BotoCoreError, ClientError):
        logger.exception("Could not check the post limit")
        return _response(500, {"msg": "Could not create the post. Please try again."}), False
    return _response(429, {
        "msg": f"You already used your {limit} free posts this month.",
        "code": "POST_LIMIT",
        "canBuyMore": True,
        "postsPerPack": POSTS_PER_PACK,
        "resetsOn": free_posts_reset_on(),
    }), False


def optional_address(payload: dict):
    """The poster's optional pickup/service address, trimmed; None when not given."""
    value = payload.get("address")
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value or None


def address_location(payload: dict) -> dict:
    """{"addressLat", "addressLng"} (strings, 6 places) for the post's address pin on the map;
    {} unless both are valid coordinates. Bad values are dropped, not fatal: the post still saves."""
    try:
        lat = float(payload.get("addressLat"))
        lng = float(payload.get("addressLng"))
    except (TypeError, ValueError):
        return {}
    if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
        return {}
    if lat == 0 and lng == 0:  # what a failed lookup tends to send
        return {}
    return {"addressLat": f"{lat:.6f}", "addressLng": f"{lng:.6f}"}


def has_address_pin(item: dict) -> bool:
    return bool(item.get("address")) and all(item.get(f) not in (None, "") for f in ADDRESS_COORDINATE_FIELDS)


def public_listings(items):
    """Copies of provider listings reduced to PUBLIC_LISTING_FIELDS."""
    return [{k: item[k] for k in PUBLIC_LISTING_FIELDS if k in item} for item in items]


def query_result(response: dict) -> dict:
    """A DynamoDB query response without the AWS metadata, safe to return to clients."""
    return {k: v for k, v in response.items() if k != "ResponseMetadata"}


def query_all(table, **kwargs) -> dict:
    """Run a DynamoDB query and follow LastEvaluatedKey until every page is read.

    A single query call stops at 1 MB, so without this a busy ZIP or user would silently lose
    results. Returns {"Items", "Count", "ScannedCount"} like query_result, for all pages."""
    items, scanned = [], 0
    while True:
        page = table.query(**kwargs)
        items += page.get("Items", [])
        scanned += page.get("ScannedCount", 0)
        if "LastEvaluatedKey" not in page:
            return {"Items": items, "Count": len(items), "ScannedCount": scanned}
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


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


def already_offered(offer_table, buyer_id: str, item_id: int, seller_id: str) -> bool:
    """True if seller_id has already made an offer on buyer_id's request item_id.

    Sellers may offer once per request; offers are found through the offer table's
    userId-itemId-index (userId is the buyer).
    """
    result = query_all(
        offer_table,
        IndexName="userId-itemId-index",
        KeyConditionExpression="userId = :u AND itemId = :i",
        ExpressionAttributeValues={":u": str(buyer_id), ":i": int(item_id)},
    )
    return any(str(o.get("pUserId")) == str(seller_id) for o in result.get("Items", []))


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



# ---------------------------------------------------------------------------
# Distance between ZIP codes (Home feed and ads "near me")
# ---------------------------------------------------------------------------
NEARBY_MILES = 50
_ZIP_CENTROIDS = None


# Centroid files bundled in the layer: US Census ZCTAs (5 digits) and India PIN codes (6 digits,
# GeoNames, district-level precision: fine for a 50-mile radius, too coarse to pick a PIN from a
# position, which /guest/zip asks Amazon Location for in India).
CENTROID_FILES = ("zip_centroids.csv", "in_pin_centroids.csv")


def _zip_centroids():
    """{postal code: (lat, lon)} from the bundled centroid files, loaded once per container."""
    global _ZIP_CENTROIDS
    if _ZIP_CENTROIDS is None:
        table = {}
        for name in CENTROID_FILES:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("#"):
                            continue
                        z, lat, lon = line.rstrip("\n").split(",")
                        table[z] = (float(lat), float(lon))
            except OSError:
                logger.exception("Postal centroid file %s missing; distance filtering falls back to exact codes", name)
        _ZIP_CENTROIDS = table
    return _ZIP_CENTROIDS


_US_ZIP = re.compile(r"^(\d{5})(?:-\d{4})?$")
_IN_PIN = re.compile(r"^([1-9]\d{2})\s?(\d{3})$")   # India: 6 digits, never starting with 0 ("560 001" ok)


def _norm_zip(zip_code):
    """The postal code eabu keys on: a US ZIP's 5 digits ('12345', '12345-6789') or an India PIN's
    6 digits ('560001', '560 001'); None for anything else. The two never collide (5 vs 6 digits)."""
    raw = str(zip_code or "").strip()
    m = _US_ZIP.match(raw)
    if m:
        return m.group(1)
    m = _IN_PIN.match(raw)
    return m.group(1) + m.group(2) if m else None


def country_of_zip(zip_code):
    """'US' for a ZIP, 'IN' for a PIN code, None otherwise."""
    z = _norm_zip(zip_code)
    return None if not z else ("US" if len(z) == 5 else "IN")


def zip_distance_miles(zip_a, zip_b):
    """Straight-line miles between two postal code centroids (US ZIP or India PIN), or None if either is unknown.
    The same code is always 0, even if it isn't in the centroid file (PO-box ZIPs, non-US codes)."""
    raw_a, raw_b = str(zip_a or "").strip(), str(zip_b or "").strip()
    if raw_a and raw_a == raw_b:
        return 0.0
    a, b = _norm_zip(raw_a), _norm_zip(raw_b)
    if not a or not b:
        return None
    if a == b:
        return 0.0
    table = _zip_centroids()
    if a not in table or b not in table:
        return None
    (lat1, lon1), (lat2, lon2) = table[a], table[b]
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3958.8 * 2 * math.asin(min(1.0, math.sqrt(h)))


NEAREST_ZIP_MAX_MILES = 30


def nearest_zip(lat, lon, max_miles=NEAREST_ZIP_MAX_MILES):
    """The postal code (US ZIP or India PIN) whose centroid is closest to (lat, lon), or None if none is within
    max_miles (outside the US, at sea). Turns a phone's position into a ZIP without any
    outside service. Nearest centroid, so near a boundary it can pick the neighbouring ZIP,
    which doesn't matter for a 50-mile feed."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    # Cheap equirectangular distance for the scan; exact enough at these ranges.
    cos_lat = math.cos(math.radians(lat))
    best, best_d2 = None, None
    for z, (zlat, zlon) in _zip_centroids().items():
        dy = zlat - lat
        dx = (zlon - lon) * cos_lat
        d2 = dx * dx + dy * dy
        if best_d2 is None or d2 < best_d2:
            best, best_d2 = z, d2
    if best is None or math.sqrt(best_d2) * 69.05 > max_miles:
        return None
    return best


PLACE_INDEX = os.environ.get("PLACE_INDEX_NAME", "placeIndex43161903-dev")
_geo = None


def in_india(lat, lon):
    """Rough bounding box of India (the PIN code lookup's area)."""
    return 6.0 <= lat <= 37.5 and 68.0 <= lon <= 97.5


def india_pin_at(lat, lon):
    """The India PIN code at a position from Amazon Location (street-level, unlike the bundled
    district-level centroids), or None. The result is used once and never stored (the place index
    is single-use)."""
    global _geo
    try:
        if _geo is None:
            _geo = boto3.client("location", region_name=os.environ.get("REGION_NAME", "us-east-1"))
        res = _geo.search_place_index_for_position(IndexName=PLACE_INDEX, Position=[float(lon), float(lat)],
                                                   MaxResults=3, Language="en")
    except Exception:
        logger.warning("PIN lookup failed", exc_info=True)
        return None
    for r in res.get("Results") or []:
        place = r.get("Place") or {}
        if place.get("Country") == "IND":
            pin = _norm_zip(place.get("PostalCode"))
            if pin and len(pin) == 6:
                return pin
    return None


def postal_code_at(lat, lon):
    """The postal code for a phone's position: India asks Amazon Location first (falling back to the
    nearest PIN centroid), everywhere else the nearest bundled centroid (US ZIPs)."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if in_india(lat, lon):
        return india_pin_at(lat, lon) or nearest_zip(lat, lon)
    return nearest_zip(lat, lon)


def is_known_zip(zip_code):
    z = _norm_zip(zip_code)
    return bool(z) and z in _zip_centroids()


def within_miles(origin_zip, items, miles=NEARBY_MILES, zip_field="zipcode"):
    """Items whose ZIP is within `miles` of origin_zip, each with a `distanceMiles` (1 decimal).
    If origin_zip isn't in the centroid file, only items in that exact ZIP are kept."""
    kept = []
    for item in items:
        d = zip_distance_miles(origin_zip, item.get(zip_field))
        if d is not None and d <= miles:
            kept.append({**item, "distanceMiles": round(d, 1)})
    return kept
