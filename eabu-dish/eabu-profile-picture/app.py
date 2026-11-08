"""Optional profile picture: upload (replacing any earlier one) or delete.

POST   /profile-picture            body {"user_id", "picture": <base64 image>}
DELETE /profile-picture/{userId}

Each user has at most one profile picture, stored at {userId}/profile.jpg. The bucket is
not versioned, so an upload overwrites the previous picture and a delete removes it for
good. No other images are accepted or stored.
"""
import json
import os
from my_utils import helpers

import boto3
import logging
import base64
import io
import re
import warnings
from PIL import Image, ExifTags
from botocore.exceptions import BotoCoreError, ClientError

# Set up logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Initialize S3 client
s3_client = boto3.client('s3')
BUCKET_NAME = os.environ.get('IDENTITY_BUCKET_NAME', 'eabu-identity-pictures')
MAX_FILE_SIZE = 10 * 1024 * 1024
ALLOWED_IMAGE_FORMATS = ["JPEG", "PNG", "WEBP"]
# userId becomes an S3 key prefix, so restrict it to a safe character set (no '/', no '..').
USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_@.+:-]{1,128}$")

# Guard against decompression bombs: refuse anything over ~50 megapixels.
Image.MAX_IMAGE_PIXELS = 50_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)

def validate_image_base64(base64_data: str, image_type: str) -> bytes:
    """Validate and convert base64 image data"""
    try:
        # Remove data URL prefix if present
        if ',' in base64_data:
            base64_data = base64_data.split(',')[1]
            
        # Decode base64
        image_data = base64.b64decode(base64_data)
        
        # Check file size
        if len(image_data) > MAX_FILE_SIZE:
            raise ValueError(f"{image_type} image exceeds maximum size of 10MB")
        
        # Verify it's a valid image in an allowed format
        img = Image.open(io.BytesIO(image_data), formats=ALLOWED_IMAGE_FORMATS)
        img.verify()

        return image_data
    except Exception as e:
        logger.warning("Rejected %s image: %s", image_type, e)
        raise ValueError(f"Invalid {image_type} image")

def optimize_image(image_data: bytes, max_size: tuple = (1920, 1080)) -> bytes:
    """Optimize image before upload"""
    try:
        img = Image.open(io.BytesIO(image_data))
        
        # Convert to RGB if necessary
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        
        # Resize if larger than max_size
        if img.size[0] > max_size[0] or img.size[1] > max_size[1]:
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
        #fix any rotation issue
        for orientation in ExifTags.TAGS.keys():
            if ExifTags.TAGS[orientation] == 'Orientation':
                break

        image = img;
        exif = img.getexif()
        if exif:
            orientation_value = exif.get(orientation)
            if orientation_value == 3:
                image = img.rotate(180, expand=True)
            elif orientation_value == 6:
                image = img.rotate(270, expand=True)
            elif orientation_value == 8:
                image = img.rotate(90, expand=True)
        # Save optimized image (re-encoding also strips EXIF metadata such as GPS location)
        if image.mode != 'RGB':
            image = image.convert('RGB')
        buffer = io.BytesIO()
        image.save(buffer, format='JPEG', quality=90, optimize=True)
        return buffer.getvalue()
    except Exception as e:
        # Never upload the raw, unprocessed bytes if re-encoding failed.
        logger.error("Image optimization failed: %s", e)
        raise ValueError("Image could not be processed")

def picture_key(user_id: str) -> str:
    return f"{user_id}/profile.jpg"


def _authorized_user_id(event, user_id):
    """Return (user_id, None) when the caller may manage this user's picture, else (None, response)."""
    if not isinstance(user_id, str) or not USER_ID_PATTERN.match(user_id) or ".." in user_id:
        return None, helpers._response(400, {"msg": "Invalid user_id"})
    denied = helpers.authorize_user(event, user_id)
    if denied:
        return None, denied
    return user_id, None


def upload_picture(event):
    if not event.get('body'):
        return helpers._response(400, {"msg": "Request body is required"})
    try:
        body = helpers._parse_body(event)
    except ValueError:
        return helpers._response(400, {"msg": "Invalid JSON format"})
    if not isinstance(body, dict):
        return helpers._response(400, {"msg": "Invalid JSON format"})

    user_id, denied = _authorized_user_id(event, body.get('user_id'))
    if denied:
        return denied

    base64_data = body.get('picture')
    if not isinstance(base64_data, str) or not base64_data:
        return helpers._response(400, {"msg": "picture is required"})
    try:
        image_data = optimize_image(validate_image_base64(base64_data, 'picture'), max_size=(1024, 1024))
    except ValueError:
        return helpers._response(400, {"msg": "Error processing picture"})

    try:
        s3_client.put_object(Bucket=BUCKET_NAME, Key=picture_key(user_id), Body=image_data,
                             ContentType='image/jpeg')
    except (BotoCoreError, ClientError):
        logger.exception("Failed to upload profile picture")
        return helpers._response(500, {"msg": "Failed to upload picture"})

    return helpers._response(200, {"message": "Profile picture saved", "userId": user_id})


def delete_picture(event):
    user_id, denied = _authorized_user_id(event, (event.get('pathParameters') or {}).get('userId'))
    if denied:
        return denied
    try:
        # Succeeds whether or not a picture exists, so deleting twice is harmless.
        s3_client.delete_object(Bucket=BUCKET_NAME, Key=picture_key(user_id))
    except (BotoCoreError, ClientError):
        logger.exception("Failed to delete profile picture")
        return helpers._response(500, {"msg": "Failed to delete picture"})
    return helpers._response(200, {"message": "Profile picture deleted", "userId": user_id})


@helpers.cors
def lambda_handler(event, context):
    try:
        method = (event.get('httpMethod') or '').upper()
        if method == 'DELETE':
            return delete_picture(event)
        if method == 'POST':
            return upload_picture(event)
        return helpers._response(405, {"msg": "Method not allowed"})
    except Exception:
        logger.exception("Lambda execution failed")
        return helpers._response(500, {"msg": "Internal server error"})
