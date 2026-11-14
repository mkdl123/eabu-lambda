"""scripts/moderate.py: list reports, remove a post, ban and unban a user."""
import importlib.util
import os

import boto3
import pytest
from moto import mock_aws

from test_security import mk

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def load_script():
    spec = importlib.util.spec_from_file_location("moderate", os.path.join(ROOT, "scripts", "moderate.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def setup():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    t = {}
    for cat in ("eat", "live", "move", "lifestyle"):
        for role in ("provider", "consumer", "offer"):
            name = f"eabu-{cat}-{role}"
            ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                             KeySchema=[{"AttributeName": "zipcode", "KeyType": "HASH"},
                                        {"AttributeName": "ID", "KeyType": "RANGE"}],
                             AttributeDefinitions=[{"AttributeName": "zipcode", "AttributeType": "S"},
                                                   {"AttributeName": "ID", "AttributeType": "N"},
                                                   {"AttributeName": "userId", "AttributeType": "S"}],
                             GlobalSecondaryIndexes=[{"IndexName": "userId-index",
                                                      "KeySchema": [{"AttributeName": "userId", "KeyType": "HASH"}],
                                                      "Projection": {"ProjectionType": "ALL"}}])
            t[name] = ddb.Table(name)
    t["ads"] = mk(ddb, "eabu-advertisement", "zipcode", "S", "ID", "N")
    t["feedback"] = mk(ddb, "eabu-feedback", "createdAt", "S", "ID", "N")
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="eabu-identity-pictures")
    cognito = boto3.client("cognito-idp", region_name="us-east-1")
    pool = cognito.create_user_pool(PoolName="p")["UserPool"]["Id"]
    for user in ("bad", "good"):
        cognito.admin_create_user(UserPoolId=pool, Username=user)
    m = load_script()
    m.POOL_ID = pool
    return t, m, cognito, pool


@mock_aws
def test_reports_remove_post_ban_unban_resolve(capsys):
    t, m, cognito, pool = setup()
    s3 = boto3.client("s3", region_name="us-east-1")
    t["eabu-eat-provider"].put_item(Item={"zipcode": "19060", "ID": 100, "counter": 7})
    t["eabu-eat-provider"].put_item(Item={"zipcode": "19060", "ID": 5, "userId": "bad", "title": "Bad cakes", "picture1": "y"})
    t["eabu-live-consumer"].put_item(Item={"zipcode": "19060", "ID": 6, "userId": "bad", "title": "Bad request"})
    t["eabu-move-provider"].put_item(Item={"zipcode": "19060", "ID": 7, "userId": "good", "title": "Fine"})
    t["eabu-eat-offer"].put_item(Item={"zipcode": "19060", "ID": 8, "userId": "good", "pUserId": "bad", "itemId": 3})
    t["ads"].put_item(Item={"zipcode": "19060", "ID": 9, "ownerUserId": "bad", "status": "active", "title": "Ad"})
    s3.put_object(Bucket="eabu-identity-pictures", Key="listings/eat/5/1.jpg", Body=b"x")
    t["feedback"].put_item(Item={"createdAt": "2026-11-12T10:00", "ID": 42, "type": "report", "status": "open",
                                 "reason": "offensive", "contentType": "post", "reportedUserId": "bad",
                                 "reporterUserId": "good", "category": "eat", "zipcode": "19060", "itemId": "5"})
    t["feedback"].put_item(Item={"createdAt": "2026-11-11T10:00", "ID": 1, "feedback": "nice app"})

    assert [r["ID"] for r in m.list_reports()] == [42], "plain feedback is not a report"

    # remove-post shows first, deletes only with --yes, and takes the pictures too
    assert m.main(["remove-post", "eat", "listing", "19060", "5"]) == 0
    assert "Item" in t["eabu-eat-provider"].get_item(Key={"zipcode": "19060", "ID": 5})
    assert m.main(["remove-post", "eat", "listing", "19060", "100", "--yes"]) == 1, "never the counter row"
    assert m.main(["remove-post", "eat", "listing", "19060", "5", "--yes"]) == 0
    assert "Item" not in t["eabu-eat-provider"].get_item(Key={"zipcode": "19060", "ID": 5})
    assert s3.list_objects_v2(Bucket="eabu-identity-pictures").get("KeyCount") == 0

    # ban without --yes changes nothing
    assert m.main(["ban", "bad"]) == 0
    assert cognito.admin_get_user(UserPoolId=pool, Username="bad")["Enabled"] is True
    assert m.main(["ban", "bad", "--yes"]) == 0
    assert cognito.admin_get_user(UserPoolId=pool, Username="bad")["Enabled"] is False
    assert "Item" not in t["eabu-live-consumer"].get_item(Key={"zipcode": "19060", "ID": 6})
    assert "Item" not in t["eabu-eat-offer"].get_item(Key={"zipcode": "19060", "ID": 8})
    assert t["ads"].get_item(Key={"zipcode": "19060", "ID": 9})["Item"]["status"] == "removed"
    assert "Item" in t["eabu-move-provider"].get_item(Key={"zipcode": "19060", "ID": 7}), "others untouched"
    assert "Item" in t["eabu-eat-provider"].get_item(Key={"zipcode": "19060", "ID": 100}), "counter untouched"
    assert cognito.admin_get_user(UserPoolId=pool, Username="good")["Enabled"] is True
    assert m.main(["ban", "nobody", "--yes"]) == 1

    assert m.main(["unban", "bad", "--yes"]) == 0
    assert cognito.admin_get_user(UserPoolId=pool, Username="bad")["Enabled"] is True

    assert m.main(["resolve", "42", "--note", "post removed, user banned", "--yes"]) == 0
    assert m.list_reports() == []
    row = m.list_reports(include_resolved=True)[0]
    assert (row["status"], row["resolution"]) == ("resolved", "post removed, user banned")

    assert m.main(["user", "bad"]) == 0
    out = capsys.readouterr().out
    assert "Account bad: enabled" in out and "Reports about them (1)" in out
