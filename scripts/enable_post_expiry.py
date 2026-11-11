#!/usr/bin/env python3
"""Turn on 60-day expiry for posts and their pictures. Safe to re-run.

1. Gives every existing post in the 8 consumer/provider tables an expiresAt (epoch seconds)
   of createdAt + 60 days, if it has none yet. New and reposted posts get it from the code.
2. Restarts the S3 clock on pictures of seller posts that are still live, so a listing
   that was reposted after its pictures were uploaded doesn't lose them early.
3. Enables DynamoDB TTL on expiresAt for those 8 tables. DynamoDB then deletes expired
   posts for free, usually within a day or two of expiry.
4. Adds an S3 lifecycle rule deleting listings/ pictures 60 days after their last write,
   keeping the bucket's other rules.

    python3 scripts/enable_post_expiry.py --dry-run   # show what would change
    python3 scripts/enable_post_expiry.py             # do it

Posts whose createdAt is already more than 60 days old get an expiresAt in the past, so
DynamoDB deletes them soon after TTL is on. The counter row (ID 100, zipcode 11111) is never touched.
"""
import argparse
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = "us-east-1"
TTL_DAYS = 60
CATEGORIES = ("eat", "live", "move", "lifestyle")
TABLES = [f"eabu-{c}-{r}" for c in CATEGORIES for r in ("consumer", "provider")]
BUCKET = "eabu-identity-pictures"
RULE_ID = "ExpireListingPicturesAfter60Days"


def created_epoch(value):
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # older posts stored naive UTC times
    return int(dt.timestamp())


def scan_all(table):
    kwargs, items = {}, []
    while True:
        page = table.scan(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    dry = ap.parse_args().dry_run
    ddb = boto3.resource("dynamodb", region_name=REGION)
    # DescribeTimeToLive has a low rate limit; retry with backoff instead of failing.
    ddb_client = boto3.client("dynamodb", region_name=REGION,
                              config=Config(retries={"max_attempts": 10, "mode": "adaptive"}))
    s3 = boto3.client("s3", region_name=REGION)
    now = int(time.time())
    totals = {"posts": 0, "set": 0, "already": 0, "expired": 0, "pictures": 0}

    for name in TABLES:
        table = ddb.Table(name)
        category, role = name.split("-")[1], name.split("-")[2]
        set_n = expired_n = pics_n = 0
        for item in scan_all(table):
            if int(item.get("ID", 0)) == 100 and item.get("zipcode") == "11111":
                continue  # the ID counter row
            totals["posts"] += 1
            expires = item.get("expiresAt")
            if expires is None:
                created = created_epoch(item.get("createdAt"))
                expires = (created if created is not None else now) + TTL_DAYS * 86400
                set_n += 1
                if not dry:
                    try:
                        table.update_item(Key={"zipcode": item["zipcode"], "ID": item["ID"]},
                                          UpdateExpression="SET expiresAt = :e",
                                          ConditionExpression="attribute_not_exists(expiresAt)",
                                          ExpressionAttributeValues={":e": int(expires)})
                    except ClientError as e:
                        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                            raise
            else:
                totals["already"] += 1
            if int(expires) <= now:
                expired_n += 1
            elif role == "provider":
                for slot in ("1", "2"):
                    if item.get(f"picture{slot}"):
                        key = f"listings/{category}/{int(item['ID'])}/{slot}.jpg"
                        pics_n += 1
                        if not dry:
                            try:
                                s3.copy_object(Bucket=BUCKET, Key=key, CopySource={"Bucket": BUCKET, "Key": key},
                                               MetadataDirective="REPLACE", ContentType="image/jpeg")
                            except ClientError as e:
                                if e.response["Error"]["Code"] not in ("NoSuchKey", "404"):
                                    raise
        totals["set"] += set_n; totals["expired"] += expired_n; totals["pictures"] += pics_n

        status = ddb_client.describe_time_to_live(TableName=name)["TimeToLiveDescription"].get("TimeToLiveStatus")
        if status in ("ENABLED", "ENABLING"):
            ttl_note = f"TTL already {status.lower()}"
        else:
            ttl_note = "TTL would be enabled" if dry else "TTL enabled"
            if not dry:
                ddb_client.update_time_to_live(TableName=name, TimeToLiveSpecification={
                    "Enabled": True, "AttributeName": "expiresAt"})
        print(f"{name:26} expiresAt {'to set' if dry else 'set'}: {set_n:3}  "
              f"past 60 days (deleted soon): {expired_n:3}  live pictures refreshed: {pics_n:3}  {ttl_note}")

    try:
        rules = s3.get_bucket_lifecycle_configuration(Bucket=BUCKET).get("Rules", [])
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchLifecycleConfiguration":
            raise
        rules = []
    if any(r.get("ID") == RULE_ID for r in rules):
        print(f"S3: rule {RULE_ID} already present")
    else:
        rules = [r for r in rules if r.get("ID") != RULE_ID] + [{
            "ID": RULE_ID, "Status": "Enabled", "Filter": {"Prefix": "listings/"},
            "Expiration": {"Days": TTL_DAYS}}]
        if not dry:
            s3.put_bucket_lifecycle_configuration(Bucket=BUCKET, LifecycleConfiguration={"Rules": rules})
        print(f"S3: {'would add' if dry else 'added'} rule {RULE_ID} (keeping {len(rules) - 1} existing rule(s))")

    print(f"\nPosts: {totals['posts']}  expiresAt {'to set' if dry else 'set'}: {totals['set']}  "
          f"already had it: {totals['already']}  older than 60 days: {totals['expired']}  "
          f"pictures refreshed: {totals['pictures']}{'  (dry run, nothing changed)' if dry else ''}")


if __name__ == "__main__":
    main()
