"""Up to four pictures per seller service listing or ad: upload (replacing that slot) or delete.

POST   /listing-picture
       body {"category": "eat|live|move|lifestyle|ad", "zipcode", "itemId", "slot": "1"-"4",
             "picture": <base64 image>}
DELETE /listing-picture/{category}/{zipcode}/{itemId}/{slot}

Only the seller who owns the listing may change its pictures. Each picture is checked with
Amazon Rekognition first (content_filter); nudity or sexual content gets a 400 IMAGE_REJECTED
and is never stored. Pictures are stored at
listings/{category}/{itemId}/{slot}.jpg in the private picture bucket (unversioned, so an
upload overwrites that slot and a delete removes it for good), and the listing row records
picture{slot} = upload time so the read functions know which links to sign. The listing
itself is never deleted here: sellers can't delete posts (posts count toward the monthly
limit).

Category "ad" is an advertiser's ad (the ad table, owner = ownerUserId), stored at
ads/{ID}/{slot}.jpg; the same Rekognition check applies.
"""
import base64
import io
import logging
import os
import warnings
from datetime import datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image, ImageOps

from my_utils import content_filter, helpers

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

BUCKET_NAME = os.environ.get("IDENTITY_BUCKET_NAME", "eabu-identity-pictures")
TABLE_ENV = {
    "eat": ("EAT_PROVIDER_TABLE_NAME", "eabu-eat-provider"),
    "live": ("LIVE_PROVIDER_TABLE_NAME", "eabu-live-provider"),
    "move": ("MOVE_PROVIDER_TABLE_NAME", "eabu-move-provider"),
    "lifestyle": ("LIFESTYLE_PROVIDER_TABLE_NAME", "eabu-lifestyle-provider"),
    helpers.AD_PICTURE_CATEGORY: ("AD_TABLE_NAME", "eabu-advertisement"),
}
OWNER_FIELD = {helpers.AD_PICTURE_CATEGORY: "ownerUserId"}  # listings: userId
MAX_FILE_SIZE = 8 * 1024 * 1024
MAX_DIMENSION = 1600
ALLOWED_IMAGE_FORMATS = ["JPEG", "PNG", "WEBP"]

# Guard against decompression bombs: refuse anything over ~50 megapixels.
Image.MAX_IMAGE_PIXELS = 50_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)

region = os.environ.get("REGION_NAME", "us-east-1")
s3_client = boto3.client("s3", region_name=region)
dynamodb = boto3.resource("dynamodb", region_name=region)


def process_image(base64_data: str) -> bytes:
    """Validate the upload and re-encode it as a JPEG (no EXIF/GPS), at most 1600px."""
    try:
        if "," in base64_data:
            base64_data = base64_data.split(",", 1)[1]
        raw = base64.b64decode(base64_data)
        if len(raw) > MAX_FILE_SIZE:
            raise ValueError("too large")
        Image.open(io.BytesIO(raw), formats=ALLOWED_IMAGE_FORMATS).verify()
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw), formats=ALLOWED_IMAGE_FORMATS))
        img = img.convert("RGB")
        img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85, optimize=True)
        return out.getvalue()
    except Exception as e:  # never store bytes that failed validation
        logger.warning("Rejected listing picture: %s", type(e).__name__)
        raise ValueError("Invalid picture")


def check_picture(image: bytes, item_id):
    """Return an error response if the picture has nudity or sexual content (or can't be checked)."""
    try:
        reason = content_filter.picture_block_reason(image)
    except content_filter.ModerationUnavailable:
        return helpers._response(503, {"msg": content_filter.PICTURE_CHECK_FAILED_MESSAGE,
                                       "code": "IMAGE_CHECK_FAILED"})
    if reason:
        logger.warning("Rejected listing picture for item %s: %s", item_id, reason)
        return helpers._response(400, {"msg": content_filter.PICTURE_REJECTED_MESSAGE,
                                       "code": "IMAGE_REJECTED"})
    return None


def _listing_target(event, category, zipcode, item_id, slot):
    """Return ((table, key, item_id, slot), None) when the caller owns the listing, else (None, response)."""
    if category not in TABLE_ENV:
        return None, helpers._response(400, {"msg": "Invalid category"})
    if str(slot) not in helpers.PICTURE_SLOTS:
        return None, helpers._response(400, {"msg": "slot must be 1 to 4"})
    if not isinstance(zipcode, str) or not zipcode or len(zipcode) > 10:
        return None, helpers._response(400, {"msg": "Invalid zipcode"})
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return None, helpers._response(400, {"msg": "Invalid itemId"})
    if item_id <= 0 or item_id == 100:  # the ID counter row lives at ID 100
        return None, helpers._response(400, {"msg": "Invalid itemId"})

    denied = helpers.require_authenticated(event)
    if denied:
        return None, denied

    env, default = TABLE_ENV[category]
    table = dynamodb.Table(os.environ.get(env, default))
    key = {"zipcode": zipcode, "ID": item_id}
    owner_field = OWNER_FIELD.get(category, "userId")
    try:
        item = table.get_item(Key=key, ProjectionExpression=f"ID, {owner_field}").get("Item")
    except (BotoCoreError, ClientError):
        logger.exception("Failed to look up listing")
        return None, helpers._response(500, {"msg": "Failed to update picture"})
    if not item:
        return None, helpers._response(404, {"msg": "Listing not found"})

    # Only the seller who posted the listing (or the ad's owner) may change its pictures.
    if not item.get(owner_field):
        return None, helpers._response(403, {"msg": "Forbidden"})
    denied = helpers.authorize_user(event, item.get(owner_field))
    if denied:
        return None, denied
    return (table, key, item_id, str(slot)), None


def upload_picture(event):
    try:
        body = helpers._parse_body(event)
    except ValueError:
        return helpers._response(400, {"msg": "Invalid JSON format"})

    picture = body.get("picture")
    if not isinstance(picture, str) or not picture:
        return helpers._response(400, {"msg": "picture is required"})

    target, denied = _listing_target(event, body.get("category"), body.get("zipcode"),
                                     body.get("itemId"), body.get("slot"))
    if denied:
        return denied
    table, key, item_id, slot = target

    try:
        image = process_image(picture)
    except ValueError:
        return helpers._response(400, {"msg": "Error processing picture"})
    rejected = check_picture(image, item_id)
    if rejected:
        return rejected

    uploaded_at = datetime.now(timezone.utc).isoformat()
    try:
        s3_client.put_object(Bucket=BUCKET_NAME, ContentType="image/jpeg", Body=image,
                             Key=helpers.listing_picture_key(body["category"], item_id, slot))
        table.update_item(
            Key=key,
            UpdateExpression=f"SET picture{slot} = :t",
            ConditionExpression="attribute_exists(ID)",
            ExpressionAttributeValues={":t": uploaded_at},
        )
    except (BotoCoreError, ClientError):
        logger.exception("Failed to save listing picture")
        return helpers._response(500, {"msg": "Failed to upload picture"})

    return helpers._response(200, {"message": "Picture saved", "itemId": item_id, "slot": slot})


def delete_picture(event):
    path = event.get("pathParameters") or {}
    category = path.get("category")
    target, denied = _listing_target(event, category, path.get("zipcode"),
                                     path.get("itemId"), path.get("slot"))
    if denied:
        return denied
    table, key, item_id, slot = target

    try:
        # Succeeds whether or not a picture exists, so deleting twice is harmless.
        s3_client.delete_object(Bucket=BUCKET_NAME,
                                Key=helpers.listing_picture_key(category, item_id, slot))
        table.update_item(Key=key, UpdateExpression=f"REMOVE picture{slot}",
                          ConditionExpression="attribute_exists(ID)")
    except (BotoCoreError, ClientError):
        logger.exception("Failed to delete listing picture")
        return helpers._response(500, {"msg": "Failed to delete picture"})

    return helpers._response(200, {"message": "Picture deleted", "itemId": item_id, "slot": slot})


@helpers.cors
def lambda_handler(event, context):
    try:
        method = (event.get("httpMethod") or "").upper()
        if method == "POST":
            return upload_picture(event)
        if method == "DELETE":
            return delete_picture(event)
        return helpers._response(405, {"msg": "Method not allowed"})
    except Exception:
        logger.exception("Lambda execution failed")
        return helpers._response(500, {"msg": "Internal server error"})
