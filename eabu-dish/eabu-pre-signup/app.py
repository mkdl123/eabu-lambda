"""Cognito pre sign-up trigger: refuse sign-ups with an email whose account was deleted recently.

eabu-delete-user-info stores a hash of the deleted account's email in eabu-blocked-emails with
expiresAt = deletion + helpers.EMAIL_BLOCK_DAYS. While that's in the future, a self-service
sign-up with the same email is rejected; Cognito shows the user the message we raise (the app
strips Cognito's "PreSignUp failed with error" prefix).

Accounts created by an admin (PreSignUp_AdminCreateUser) are allowed, so support can override.
If the lookup itself fails, the sign-up is allowed: a broken check must never block everyone.
This function has no API route; it's attached to the user pool by
scripts/attach-pre-signup-trigger.py.
"""
import logging
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from my_utils import helpers

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

blocked_emails = boto3.resource("dynamodb", region_name=os.environ.get("REGION_NAME", "us-east-1")).Table(
    os.environ.get("BLOCKED_EMAILS_TABLE_NAME", "eabu-blocked-emails"))


class EmailBlocked(Exception):
    pass


def lambda_handler(event, context):
    if event.get("triggerSource") != "PreSignUp_SignUp":
        return event
    email = (event.get("request", {}).get("userAttributes") or {}).get("email")
    if not email:
        return event

    try:
        row = blocked_emails.get_item(Key={"emailHash": helpers.email_hash(email)}).get("Item")
    except (BotoCoreError, ClientError):
        logger.exception("Blocked-email lookup failed; allowing sign-up")
        return event

    # TTL can take a day or two to remove expired rows, so check the date ourselves.
    until = int(row.get("expiresAt", 0)) if row else 0
    if until > time.time():
        day = datetime.fromtimestamp(until, timezone.utc).strftime("%B %-d, %Y")
        logger.info("Sign-up refused: email belongs to a recently deleted account")
        raise EmailBlocked(f"This email was used by an account that was deleted recently. "
                           f"You can sign up with it again from {day}.")
    return event
