"""Cognito pre sign-up trigger: refuse sexual User Ids, and emails whose account was deleted recently.

User Ids are shown on posts, so one with a blocked word (content_filter) is refused.

eabu-delete-user-info stores a hash of the deleted account's email in eabu-blocked-emails with
expiresAt = deletion + helpers.EMAIL_BLOCK_DAYS. While that's in the future, a self-service
sign-up with the same email is rejected; Cognito shows the user the message we raise (the app
strips Cognito's "PreSignUp failed with error" prefix).

A new user may name who invited them (the app sends clientMetadata.invitedBy). The inviter
must be an existing, enabled account other than the new one, or the sign-up is refused with
a message saying so. The link is saved in the post-credits table ({userId: "ref#<new user>",
invitedBy, emailHash}); helpers.reward_referral pays both when the new user first posts.

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

from my_utils import content_filter, helpers

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

blocked_emails = boto3.resource("dynamodb", region_name=os.environ.get("REGION_NAME", "us-east-1")).Table(
    os.environ.get("BLOCKED_EMAILS_TABLE_NAME", "eabu-blocked-emails"))
cognito = boto3.client("cognito-idp", region_name=os.environ.get("REGION_NAME", "us-east-1"))
MAX_INVITED_BY = 128


class EmailBlocked(Exception):
    pass


class EmailInUse(Exception):
    pass


class UserIdBlocked(Exception):
    pass


class InviterNotFound(Exception):
    pass


def _invited_by(event):
    meta = (event.get("request") or {}).get("clientMetadata") or {}
    return str(meta.get("invitedBy") or "").strip()[:MAX_INVITED_BY]


def _check_inviter(event, invited_by):
    """The inviter's real User Id, or None if the check itself failed (sign-up goes ahead)."""
    if invited_by.lower() == str(event.get("userName") or "").strip().lower():
        raise InviterNotFound("You can't enter your own User Id under Invited by. Leave it blank or enter your friend's.")
    not_found = InviterNotFound("We couldn't find anyone with the User Id under Invited by. "
                                "Check it with your friend, or leave it blank.")
    try:
        user = cognito.admin_get_user(UserPoolId=event["userPoolId"], Username=invited_by)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "UserNotFoundException":
            raise not_found
        logger.exception("Inviter lookup failed; signing up without the referral")
        return None
    except (BotoCoreError, KeyError):
        logger.exception("Inviter lookup failed; signing up without the referral")
        return None
    if user.get("Enabled") is False:
        raise not_found
    return user["Username"]


def _save_referral(event, inviter, email):
    item = {"userId": helpers.referral_key(event.get("userName")), "invitedBy": inviter,
            "createdAt": datetime.now(timezone.utc).isoformat()}
    if email:
        item["emailHash"] = helpers.email_hash(email)
    try:
        helpers.post_credits_table().put_item(Item=item, ConditionExpression="attribute_not_exists(userId)")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            logger.exception("Could not save the referral")
    except BotoCoreError:
        logger.exception("Could not save the referral")


def lambda_handler(event, context):
    if event.get("triggerSource") != "PreSignUp_SignUp":
        return event
    if content_filter.text_is_blocked(event.get("userName")):
        logger.info("Sign-up refused: User Id contains a blocked word")
        raise UserIdBlocked("This User Id isn't allowed. Please choose a different one.")
    email = (event.get("request", {}).get("userAttributes") or {}).get("email")
    invited_by = _invited_by(event)
    inviter = _check_inviter(event, invited_by) if invited_by else None
    _check_email(email)
    _check_email_unused(event, email)
    if inviter:
        _save_referral(event, inviter, email)
    return event


def _check_email_unused(event, email):
    """One account per email: people can sign in with their email (2.1), so it must name one account."""
    pool = event.get("userPoolId")
    if not email or not pool:
        return
    try:
        for candidate in dict.fromkeys([email.strip(), email.strip().lower()]):
            safe = candidate.replace("\\", "").replace('"', "")
            users = cognito.list_users(UserPoolId=pool, Filter=f'email = "{safe}"',
                                       Limit=2).get("Users", [])
            if any(u.get("Username", "").lower() != str(event.get("userName", "")).lower() for u in users):
                logger.info("Sign-up refused: email already used by another account")
                raise EmailInUse("An account already uses this email. Sign in with it, or use "
                                 "Forgot password if you don't remember the password.")
    except (BotoCoreError, ClientError):
        logger.exception("Email-in-use lookup failed; allowing sign-up")


def _check_email(email):
    if not email:
        return

    try:
        row = blocked_emails.get_item(Key={"emailHash": helpers.email_hash(email)}).get("Item")
    except (BotoCoreError, ClientError):
        logger.exception("Blocked-email lookup failed; allowing sign-up")
        return

    # TTL can take a day or two to remove expired rows, so check the date ourselves.
    until = int(row.get("expiresAt", 0)) if row else 0
    if until > time.time():
        day = datetime.fromtimestamp(until, timezone.utc).strftime("%B %-d, %Y")
        logger.info("Sign-up refused: email belongs to a recently deleted account")
        raise EmailBlocked(f"You can't create an account with this email for 30 days after its "
                           f"account was deleted. You can use it again from {day}.")
