"""Daily job for paid ads (EventBridge schedule, no API route).

1. Emails each advertiser whose subscription will renew within the next 5 days,
   once per billing period, so they hear about the next $9.99 charge 5 days ahead.
2. Hides paid ads whose paid period ended more than a day ago, in case Apple's
   EXPIRED notification never arrived.

Apple does the charging; this only warns. Advertisers who turned auto-renew off get
no reminder, because nothing will be charged.
"""
import logging
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import BotoCoreError, ClientError

from my_utils.apple_iap import PAID_STATUS, EXPIRED_STATUS

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REGION = os.environ.get("REGION_NAME", "us-east-1")
table = boto3.resource("dynamodb", region_name=REGION).Table(os.environ.get("AD_TABLE_NAME", "eabu-advertisement"))
ses = boto3.client("ses", region_name=REGION)

DAY = 86400
REMIND_DAYS = int(os.environ.get("REMIND_DAYS", "5"))
SENDER = f"Eabu <{os.environ.get('SENDER_EMAIL', 'eabu@eabu.net')}>"
PRICE = os.environ.get("AD_PRICE_TEXT", "$9.99")
MANAGE_URL = "https://apps.apple.com/account/subscriptions"


def _live_paid_ads():
    kwargs = {"FilterExpression": Attr("paid").eq(True) & Attr("status").eq(PAID_STATUS)}
    found = []
    while True:
        res = table.scan(**kwargs)
        found += res.get("Items", [])
        if "LastEvaluatedKey" not in res:
            return found
        kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]


def _email(ad, renews_on):
    title = str(ad.get("title") or "your ad")[:120]
    text = (f"Hello {ad.get('name') or ''},\n\n"
            f"Your Eabu ad \"{title}\" renews automatically on {renews_on} for {PRICE} "
            f"for another 3 months, charged by Apple to your App Store account.\n\n"
            f"Nothing to do if you'd like it to keep running. To stop it, cancel before then: "
            f"on your iPhone open Settings > your name > Subscriptions > Eabu, or visit {MANAGE_URL}\n\n"
            f"Thank you for advertising with Eabu.")
    ses.send_email(
        Source=SENDER,
        Destination={"ToAddresses": [ad["email"]]},
        Message={"Subject": {"Data": f"Your Eabu ad renews on {renews_on}"},
                 "Body": {"Text": {"Data": text}}})


def lambda_handler(event, context, now=None):
    now = now or int(datetime.now(timezone.utc).timestamp())
    sent = hidden = failed = 0
    try:
        ads = _live_paid_ads()
    except (BotoCoreError, ClientError):
        logger.exception("Could not list paid ads")
        raise

    for ad in ads:
        key = {"zipcode": ad["zipcode"], "ID": ad["ID"]}
        paid_through = int(ad.get("paidThrough") or 0)
        try:
            if paid_through and paid_through < now - DAY:
                table.update_item(Key=key, UpdateExpression="SET #s = :e",
                                  ExpressionAttributeNames={"#s": "status"},
                                  ExpressionAttributeValues={":e": EXPIRED_STATUS})
                hidden += 1
                continue
            due = now < paid_through <= now + REMIND_DAYS * DAY
            already = int(ad.get("reminderSentFor") or 0) == paid_through
            if due and not already and ad.get("autoRenew") is not False and ad.get("email"):
                renews_on = datetime.fromtimestamp(paid_through, timezone.utc).strftime("%B %-d, %Y")
                _email(ad, renews_on)
                table.update_item(Key=key, UpdateExpression="SET reminderSentFor = :p",
                                  ExpressionAttributeValues={":p": paid_through})
                sent += 1
        except (BotoCoreError, ClientError):
            # One bad address must not stop the others; it is retried tomorrow.
            logger.exception("Reminder step failed for ad %s", ad["ID"])
            failed += 1

    logger.info("Paid ads: %d checked, %d reminded, %d hidden, %d failed", len(ads), sent, hidden, failed)
    return {"checked": len(ads), "reminded": sent, "hidden": hidden, "failed": failed}
