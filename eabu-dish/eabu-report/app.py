"""Report a post, request, offer, ad or profile (App Store guideline 1.2).

POST /report  body {"userId", "reportedUserId", "contentType", "reason",
                    "category"?, "itemId"?, "zipcode"?, "title"?, "details"?}

The report is saved in the feedback table (type "report", status "open") and emailed to
REPORT_EMAIL so it can be reviewed within 24 hours. The app also blocks the reported user for
the reporter, so their content disappears straight away. A failed email doesn't fail the report.
"""
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from my_utils import helpers

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REGION = os.environ.get("REGION_NAME", "us-east-1")
table = boto3.resource("dynamodb", region_name=REGION).Table(os.environ.get("EABU_FEEDBACK_TABLE_NAME", "eabu-feedback"))
ses = boto3.client("ses", region_name=REGION)
SENDER = f"Eabu <{os.environ.get('SENDER_EMAIL', 'eabu@eabu.net')}>"
REPORT_EMAIL = os.environ.get("REPORT_EMAIL", "")

CONTENT_TYPES = ("post", "request", "offer", "ad", "profile")
REASONS = {
    "spam": "Spam or misleading",
    "offensive": "Offensive, hateful or sexual content",
    "scam": "Scam or fraud",
    "unsafe": "Harassment or unsafe behaviour",
    "other": "Something else",
}
CATEGORIES = ("eat", "live", "move", "lifestyle", "")
USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_@.+:-]{1,128}$")
MAX_DETAILS = 1000
MAX_TITLE = 200


def _text(value, limit):
    return str(value or "").replace("\r", " ").strip()[:limit]


def _email(item):
    reason = REASONS[item["reason"]]
    where = " / ".join(x for x in (item.get("category"), item.get("zipcode"), item.get("itemId")) if x)
    lines = [
        f"Reported {item['contentType']}: {item.get('title') or '(no title)'}",
        f"Reason: {reason}",
        f"Reported user: {item['reportedUserId']}",
        f"Reported by: {item['reporterUserId']}",
    ]
    if where:
        lines.append(f"Where: {where} (category / ZIP / item ID)")
    if item.get("details"):
        lines += ["", "Details from the reporter:", item["details"]]
    lines += ["", f"Report ID {item['ID']}, {item['createdAt']}.",
              "Review within 24 hours: remove the content and, if needed, the user's account."]
    ses.send_email(
        Source=SENDER,
        Destination={"ToAddresses": [REPORT_EMAIL]},
        Message={"Subject": {"Data": f"eabu report: {reason} ({item['contentType']})"},
                 "Body": {"Text": {"Data": "\n".join(lines)}}},
    )


@helpers.cors
def lambda_handler(event, context):
    if (event.get("httpMethod") or "").upper() != "POST":
        return helpers._response(405, {"msg": "Method not allowed"})
    try:
        body = helpers._parse_body(event)
    except ValueError:
        return helpers._response(400, {"msg": "Invalid JSON format"})
    if not isinstance(body, dict):
        return helpers._response(400, {"msg": "Invalid request"})

    user_id, reported = body.get("userId"), body.get("reportedUserId")
    for value in (user_id, reported):
        if not isinstance(value, str) or not USER_ID_PATTERN.match(value):
            return helpers._response(400, {"msg": "Invalid userId"})
    denied = helpers.authorize_user(event, user_id)
    if denied:
        return denied
    if reported == user_id:
        return helpers._response(400, {"msg": "You can't report yourself"})
    content_type, reason = body.get("contentType"), body.get("reason")
    category = _text(body.get("category"), 20).lower()
    if content_type not in CONTENT_TYPES or reason not in REASONS or category not in CATEGORIES:
        return helpers._response(400, {"msg": "Invalid report"})

    now = datetime.now(timezone.utc)
    item = {
        "createdAt": now.isoformat(),
        "ID": uuid.uuid4().int % 10**15,
        "type": "report",
        "status": "open",
        "reporterUserId": user_id,
        "reportedUserId": reported,
        "contentType": content_type,
        "reason": reason,
    }
    extra = {
        "category": category,
        "itemId": _text(body.get("itemId"), 40),
        "zipcode": _text(body.get("zipcode"), 10),
        "title": _text(body.get("title"), MAX_TITLE),
        "details": _text(body.get("details"), MAX_DETAILS),
    }
    item.update({k: v for k, v in extra.items() if v})

    try:
        table.put_item(Item=item)
    except (BotoCoreError, ClientError):
        logger.exception("Failed to save report")
        return helpers._response(500, {"msg": "Could not send the report. Please try again."})
    logger.info("Report %s saved: type=%s reason=%s", item["ID"], content_type, reason)

    if REPORT_EMAIL:
        try:
            _email(item)
        except (BotoCoreError, ClientError):
            logger.exception("Report %s saved but the email failed", item["ID"])
    return helpers._response(200, {"message": "Report received", "ID": item["ID"]})
