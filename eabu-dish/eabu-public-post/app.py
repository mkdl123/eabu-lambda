"""Shareable web pages for posts (www.eabu.net/p/<token>).

POST /share-link                 (signed in)  {"kind": "seller"|"buyer", "category", "zipcode", "ID"}
    Returns {"url": "https://www.eabu.net/p/<token>"} for a post that exists. The token is
    signed (HMAC, secret in SSM), so only posts someone chose to share get a web page;
    post IDs are sequential and could otherwise be stepped through.
GET  /public-post/{token}        (no sign-in: the website's preview function calls it)
    The post's safe fields only: kind, category, title, a short description, ZIP, date and
    a short-lived link to the first listing picture. Never the phone, email, address,
    location or User Id. 404 once the post is gone (deleted, removed or expired).
"""
import hashlib
import hmac
import logging
import os
import re
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from my_utils import helpers

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REGION = os.environ.get("REGION_NAME", "us-east-1")
SITE = os.environ.get("SHARE_SITE_URL", "https://www.eabu.net")
SECRET_PARAM = os.environ.get("SHARE_SECRET_PARAM", "/eabu/share-link-secret")
DESC_MAX = 300
KINDS = {"seller": ("s", "PROVIDER"), "buyer": ("b", "CONSUMER")}
KIND_BY_CODE = {code: kind for kind, (code, _) in KINDS.items()}
CATEGORY_LABEL = {"eat": "Food", "live": "Household", "move": "Transport", "lifestyle": "Lifestyle"}
ZIP_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")
TOKEN_RE = re.compile(r"^([sb])-(eat|live|move|lifestyle)-([A-Za-z0-9]{1,10})-([0-9]{1,12})-([0-9a-f]{20})$")

dynamodb = boto3.resource("dynamodb", region_name=REGION)
ssm = boto3.client("ssm", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
_secret = None


def _key():
    global _secret
    if _secret is None:
        _secret = ssm.get_parameter(Name=SECRET_PARAM, WithDecryption=True)["Parameter"]["Value"].encode()
    return _secret


def _sign(code, category, zipcode, item_id):
    msg = f"{code}-{category}-{zipcode}-{item_id}".encode()
    return hmac.new(_key(), msg, hashlib.sha256).hexdigest()[:20]


def make_token(kind, category, zipcode, item_id):
    code = KINDS[kind][0]
    return f"{code}-{category}-{zipcode}-{item_id}-{_sign(code, category, zipcode, item_id)}"


def _table(kind, category):
    role = KINDS[kind][1]
    return dynamodb.Table(os.environ.get(f"{category.upper()}_{role}_TABLE_NAME", f"eabu-{category}-{role.lower()}"))


def _live_post(kind, category, zipcode, item_id):
    """The post row, or None when it doesn't exist or has expired (TTL can lag a day or two)."""
    item = _table(kind, category).get_item(Key={"zipcode": zipcode, "ID": item_id}).get("Item")
    if not item or int(item.get("expiresAt") or 0) and int(item["expiresAt"]) < time.time():
        return None
    return item


def _short(text):
    t = " ".join(str(text or "").split())
    return t if len(t) <= DESC_MAX else t[:DESC_MAX - 1].rstrip() + "…"


def _share_link(event):
    denied = helpers.require_authenticated(event)
    if denied:
        return denied
    try:
        payload = helpers._parse_body(event)
    except ValueError:
        return helpers._response(400, {"msg": "Invalid JSON"})
    kind, category = payload.get("kind"), payload.get("category")
    zipcode = str(payload.get("zipcode") or "").strip()
    try:
        item_id = int(payload.get("ID"))
    except (TypeError, ValueError):
        item_id = None
    if kind not in KINDS or category not in CATEGORY_LABEL or not ZIP_RE.match(zipcode) or not item_id or item_id <= 0:
        return helpers._response(400, {"msg": "kind, category, zipcode and ID are required"})
    if item_id == 100:  # the counter row, not a post
        return helpers._response(404, {"msg": "Post not found"})
    if _live_post(kind, category, zipcode, item_id) is None:
        return helpers._response(404, {"msg": "Post not found"})
    return helpers._response(200, {"url": f"{SITE}/p/{make_token(kind, category, zipcode, item_id)}"})


def _public_post(event):
    token = str((event.get("pathParameters") or {}).get("token") or "")
    m = TOKEN_RE.match(token)
    if not m:
        return helpers._response(404, {"msg": "Post not found"})
    code, category, zipcode, item_id, sig = m.groups()
    if not hmac.compare_digest(sig, _sign(code, category, zipcode, int(item_id))):
        return helpers._response(404, {"msg": "Post not found"})
    kind = KIND_BY_CODE[code]
    item = _live_post(kind, category, zipcode, int(item_id))
    if item is None or int(item_id) == 100:
        return helpers._response(404, {"msg": "Post not found"})
    body = {"kind": kind, "category": category, "categoryLabel": CATEGORY_LABEL[category],
            "title": _short(item.get("title"))[:120], "description": _short(item.get("description")),
            "zipcode": zipcode, "createdAt": str(item.get("createdAt") or "")[:10], "picture": None}
    if kind == "seller":
        slot = next((s for s in helpers.PICTURE_SLOTS if item.get(f"picture{s}")), None)
        if slot:
            body["picture"] = s3.generate_presigned_url(
                "get_object", ExpiresIn=600,
                Params={"Bucket": os.environ.get("IDENTITY_BUCKET_NAME", "eabu-identity-pictures"),
                        "Key": helpers.listing_picture_key(category, item_id, slot)})
    return helpers._response(200, body)


@helpers.cors
def lambda_handler(event, context):
    method = (event.get("httpMethod") or "").upper()
    try:
        if method == "POST":
            return _share_link(event)
        if method == "GET":
            return _public_post(event)
    except (BotoCoreError, ClientError):
        logger.exception("Share link / public post failed")
        return helpers._response(500, {"msg": "Something went wrong, please try again"})
    return helpers._response(405, {"msg": "Method Not Allowed"})
