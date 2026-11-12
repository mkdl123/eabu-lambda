"""Delete a user's account and their data (POST /eabu-delete, the app's Delete Account tile).

The signed-in user can delete only themselves. In one go this removes:
  - their buyer requests and seller services in all four categories, and the seller pictures;
  - offers they made, and offers made on their requests;
  - their interest and rating records (others' listings keep their anonymous counts);
  - their favourites, and other people's favourites of them;
  - their ads, post-credit balance, legacy identity record, service requests and feedback;
  - their profile picture;
  - their Cognito sign-in account (last, so a failure part-way can be retried by tapping again).

Their email is then blocked from signing up again for helpers.EMAIL_BLOCK_DAYS (the
eabu-pre-signup Cognito trigger checks it). Only a hash of the email is kept, and DynamoDB
TTL removes it when the block ends.

Apple purchase records (tx# rows) are kept so a replayed purchase can't be credited twice,
but they lose their link to the user. A paid ad subscription keeps renewing with Apple
until the user cancels it in their Apple ID settings; the app tells them so.

The request is recorded in the delete table first and removed when everything is done, so a
failed run leaves a visible request behind. scripts/delete_user.py runs the same deletion for
a request that came in by email.
"""
import logging
import os
import time

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError

from my_utils import helpers

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REGION = os.environ.get("REGION_NAME", "us-east-1")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "us-east-1_G0VobzUMd")
BUCKET = os.environ.get("IDENTITY_BUCKET_NAME", "eabu-identity-pictures")
CATEGORIES = ("eat", "live", "move", "lifestyle")

dynamodb = boto3.resource("dynamodb", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
cognito = boto3.client("cognito-idp", region_name=REGION)


def _table(env, default):
    return dynamodb.Table(os.environ.get(env, default))


def _tables(kind):
    return {c: _table(f"{c.upper()}_{kind.upper()}_TABLE_NAME", f"eabu-{c}-{kind}") for c in CATEGORIES}


consumer_tables = _tables("consumer")
provider_tables = _tables("provider")
offer_tables = _tables("offer")
interested_tables = _tables("interested")
rated_tables = _tables("rated")
favourites = _table("FAVOURITES_TABLE_NAME", "eabu-favourites")
ads = _table("AD_TABLE_NAME", "eabu-advertisement")
credits = _table("POST_CREDITS_TABLE_NAME", "eabu-post-credits")
identity = _table("EABU_IDENTITY_TABLE_NAME", "eabu-identity")
service_requests = _table("EABU_SERVICE_REQUEST_TABLE", "eabu-service-request")
feedback = _table("EABU_FEEDBACK_TABLE_NAME", "eabu-feedback")
delete_requests = _table("EABU_DELETE_TABLE", "eabu-delete")
blocked_emails = _table("BLOCKED_EMAILS_TABLE_NAME", "eabu-blocked-emails")


def _scan_all(table, **kwargs):
    items = []
    while True:
        page = table.scan(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _delete_rows(table, rows, key_names):
    with table.batch_writer(overwrite_by_pkeys=list(key_names)) as batch:
        for row in rows:
            batch.delete_item(Key={k: row[k] for k in key_names})
    return len(rows)


def _own_posts(table, user_id):
    rows = helpers.query_all(table, IndexName="userId-index",
                             KeyConditionExpression=Key("userId").eq(user_id))["Items"]
    return [r for r in rows if str(r.get("userId")) == user_id and int(r.get("ID", 0)) != 100]


def _delete_listing_pictures(category, item_id):
    keys = [{"Key": helpers.listing_picture_key(category, item_id, slot)} for slot in helpers.PICTURE_SLOTS]
    s3.delete_objects(Bucket=BUCKET, Delete={"Objects": keys, "Quiet": True})


def _account_email(user_id):
    try:
        user = cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=user_id)
    except cognito.exceptions.UserNotFoundException:
        return None
    attrs = {a["Name"]: a["Value"] for a in user.get("UserAttributes", [])}
    return attrs.get("email")


def delete_user(user_id: str) -> dict:
    """Delete everything belonging to user_id. Safe to run again after a partial failure."""
    user_id = str(user_id)
    counts = {}
    email = _account_email(user_id)

    for category in CATEGORIES:
        # Buyer requests, and the offers sellers made on them.
        requests = _own_posts(consumer_tables[category], user_id)
        counts[f"{category}_requests"] = _delete_rows(consumer_tables[category], requests, ("zipcode", "ID"))
        received = helpers.query_all(offer_tables[category], IndexName="userId-index",
                                     KeyConditionExpression=Key("userId").eq(user_id))["Items"]
        # Offers this user made as a seller (no index on pUserId; offer tables are small).
        made = _scan_all(offer_tables[category], FilterExpression=Attr("pUserId").eq(user_id))
        offers = {(o["zipcode"], o["ID"]): o for o in received + made if int(o.get("ID", 0)) != 100}
        counts[f"{category}_offers"] = _delete_rows(offer_tables[category], list(offers.values()), ("zipcode", "ID"))

        # Seller services, their pictures, and the interest/rating records on them.
        services = _own_posts(provider_tables[category], user_id)
        for service in services:
            _delete_listing_pictures(category, service["ID"])
            for side_table in (interested_tables[category], rated_tables[category]):
                rows = helpers.query_all(side_table, KeyConditionExpression=Key("ID").eq(service["ID"]))["Items"]
                _delete_rows(side_table, rows, ("ID", "cUserId"))
        counts[f"{category}_services"] = _delete_rows(provider_tables[category], services, ("zipcode", "ID"))

        # Interest and ratings this user gave on other people's listings.
        for name, side_table in (("interested", interested_tables[category]), ("rated", rated_tables[category])):
            rows = _scan_all(side_table, FilterExpression=Attr("cUserId").eq(user_id))
            counts[f"{category}_{name}"] = _delete_rows(side_table, rows, ("ID", "cUserId"))

    # Favourites: the user's own list, and other people's favourites of this user.
    mine = helpers.query_all(favourites, KeyConditionExpression=Key("userId").eq(user_id))["Items"]
    theirs = _scan_all(favourites, FilterExpression=Attr("favUserId").eq(user_id))
    counts["favourites"] = _delete_rows(favourites, mine + theirs, ("userId", "favKey"))

    owned_ads = _scan_all(ads, FilterExpression=Attr("ownerUserId").eq(user_id))
    counts["ads"] = _delete_rows(ads, owned_ads, ("zipcode", "ID"))

    # Post credits: delete the balance; keep purchase records (replay protection) but unlink them.
    credits.delete_item(Key={"userId": user_id})
    for tx in _scan_all(credits, FilterExpression=Attr("ownerUserId").eq(user_id)):
        credits.update_item(Key={"userId": tx["userId"]}, UpdateExpression="REMOVE ownerUserId")

    identity.delete_item(Key={"userId": user_id})
    rows = helpers.query_all(service_requests, KeyConditionExpression=Key("userId").eq(user_id))["Items"]
    counts["service_requests"] = _delete_rows(service_requests, rows, ("userId", "serviceType"))
    if email:
        rows = [r for r in _scan_all(feedback)
                if str(r.get("email", "")).strip().lower() == email.strip().lower()]
        counts["feedback"] = _delete_rows(feedback, rows, ("createdAt", "ID"))

    s3.delete_object(Bucket=BUCKET, Key=f"{user_id}/profile.jpg")

    if email:
        now = int(time.time())
        blocked_emails.put_item(Item={"emailHash": helpers.email_hash(email), "blockedAt": now,
                                      "expiresAt": now + helpers.EMAIL_BLOCK_DAYS * 86400})

    # Last: the sign-in account. Until this succeeds the user can sign in and try again.
    try:
        cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=user_id)
    except cognito.exceptions.UserNotFoundException:
        pass
    delete_requests.delete_item(Key={"userId": user_id})
    return counts


def lambda_handler(event, context):
    if event.get("httpMethod") != "POST":
        return helpers._response(400, {"msg": "Bad Request: only POST is supported"})

    try:
        payload = helpers._parse_body(event)
        helpers._validate_payload(payload, ["userId"])
    except ValueError as ve:
        logger.warning("Validation error: %s", ve)
        return helpers._response(400, {"msg": str(ve)})

    # Only the signed-in user may delete themselves.
    denied = helpers.authorize_user(event, payload.get("userId"))
    if denied:
        return denied
    user_id = str(payload["userId"])

    try:
        # Record the request first, so a failure part-way leaves it visible.
        delete_requests.put_item(Item={"userId": user_id})
        counts = delete_user(user_id)
    except (BotoCoreError, ClientError):
        logger.exception("Account deletion failed")
        return helpers._response(500, {"msg": "Your account could not be deleted. Please try again."})

    logger.info("Account deleted: %s", {k: v for k, v in counts.items() if v})
    return helpers._response(200, {"message": "Your account has been deleted", "deleted": True})
