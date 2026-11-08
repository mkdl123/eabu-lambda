"""GET /eabu-user/{pUserId}: a short-lived link to the user's optional selfie.

Returns {"url": "<presigned link>"} when the user has a selfie and {"url": null} when
they have none (it is optional, and they may have deleted it).
"""
import logging
import os
from my_utils import helpers
import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

s3 = boto3.client("s3")
BUCKET = os.environ.get("IDENTITY_BUCKET_NAME", "eabu-identity-pictures")


def _selfie_exists(key):
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except ClientError as e:
        # Without s3:ListBucket a missing object reports 403 rather than 404.
        if e.response.get("Error", {}).get("Code") in ("404", "403", "NoSuchKey", "NotFound"):
            return False
        raise


@helpers.cors
def lambda_handler(event, context):
    denied = helpers.require_authenticated(event)
    if denied:
        return denied

    userid = (event.get("pathParameters") or {}).get("pUserId")
    if not userid:
        return helpers._response(400, {"msg": "pUserId is required"})
    key = f"{userid}/selfie.jpg"

    try:
        if not _selfie_exists(key):
            return helpers._response(200, {"url": None})
        url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": BUCKET, "Key": key},
            ExpiresIn=300,  # URL valid for 5 minutes
        )
        return helpers._response(200, {"url": url})
    except (BotoCoreError, ClientError):
        logger.exception("Failed to look up selfie")
        return helpers._response(500, {"msg": "Failed to generate presigned URL"})
