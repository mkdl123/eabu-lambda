"""Unsubscribe link in seller-recruitment emails (scripts/recruit.py send).

GET  /guest/unsubscribe?t=<emailHash>   (no sign-in) the link in the email footer: shows a page
POST /guest/unsubscribe?t=<emailHash>   one-click unsubscribe from the mail app (RFC 8058,
                                        List-Unsubscribe-Post), answers 200 with no page

The token is the sha256 of the lower-case email, which is also the row key in eabu-recruit, so
the email itself is never sent here or stored. Only rows the send step wrote can be marked
(attribute_exists), so the route can't be used to fill the table. Unknown or already
unsubscribed tokens get the same page: nothing to learn by guessing.
"""
import logging
import os
import re
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REGION = os.environ.get("REGION_NAME", "us-east-1")
TABLE_NAME = os.environ.get("RECRUIT_TABLE_NAME", "eabu-recruit")
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")

table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>eabu</title></head>
<body style="margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1f2937;background:#f4f6f9;">
<div style="max-width:480px;margin:60px auto;padding:32px;background:#fff;border-radius:12px;text-align:center;">
<h1 style="font-size:22px;margin:0 0 12px;">{title}</h1>
<p style="font-size:15px;line-height:1.6;margin:0;">{text}</p>
<p style="font-size:13px;margin:24px 0 0;"><a href="https://www.eabu.net" style="color:#e6402f;">www.eabu.net</a></p>
</div></body></html>"""


def _html(status, title, text):
    return {"statusCode": status,
            "headers": {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
            "body": PAGE.format(title=title, text=text)}


def unsubscribe(token):
    """Mark the row unsubscribed. True when the token is valid (row exists), else False."""
    try:
        table.update_item(
            Key={"emailHash": token},
            UpdateExpression="SET #s = :u, unsubscribedAt = if_not_exists(unsubscribedAt, :now)",
            ConditionExpression="attribute_exists(emailHash)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":u": "unsubscribed", ":now": int(time.time())},
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def lambda_handler(event, context):
    token = ((event.get("queryStringParameters") or {}).get("t") or "").strip().lower()
    method = event.get("httpMethod", "GET")
    if not TOKEN_RE.match(token):
        return _html(400, "Link not recognised",
                     "This unsubscribe link looks incomplete. Reply to the email with "
                     "\"unsubscribe\" and we'll take you off the list.")
    try:
        unsubscribe(token)
    except Exception:
        logger.exception("unsubscribe failed")
        return _html(503, "Please try again",
                     "Something went wrong. Please try the link again in a minute, or reply to the "
                     "email with \"unsubscribe\".")
    logger.info("unsubscribe via %s", method)  # never the token: it identifies an email
    if method == "POST":
        return {"statusCode": 200, "headers": {"Content-Type": "text/plain"}, "body": "unsubscribed"}
    return _html(200, "You're unsubscribed",
                 "You won't get any more emails from eabu. Sorry for the bother.")
