"""Delete Account: removes the user's sign-in and data everywhere, and nobody else's."""
import boto3
import pytest
from moto import mock_aws

from test_security import as_user, load, post

REGION = "us-east-1"
BUCKET = "eabu-identity-pictures"
CATS = ("eat", "live", "move", "lifestyle")


@pytest.fixture(autouse=True)
def enforce(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "enforce")


def make_table(ddb, name, keys, attrs, indexes=()):
    kwargs = {"GlobalSecondaryIndexes": [
        {"IndexName": n, "KeySchema": [{"AttributeName": a, "KeyType": t} for a, t in k],
         "Projection": {"ProjectionType": "ALL"}} for n, k in indexes]} if indexes else {}
    return ddb.create_table(
        TableName=name, BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": a, "KeyType": t} for a, t in keys],
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": t} for a, t in attrs], **kwargs)


def setup(monkeypatch):
    ddb = boto3.resource("dynamodb", region_name=REGION)
    post_keys, user_idx = [("zipcode", "HASH"), ("ID", "RANGE")], ("userId-index", [("userId", "HASH")])
    post_attrs = [("zipcode", "S"), ("ID", "N"), ("userId", "S")]
    t = {}
    for c in CATS:
        t[f"{c}-consumer"] = make_table(ddb, f"eabu-{c}-consumer", post_keys, post_attrs, [user_idx])
        t[f"{c}-provider"] = make_table(ddb, f"eabu-{c}-provider", post_keys, post_attrs, [user_idx])
        t[f"{c}-offer"] = make_table(ddb, f"eabu-{c}-offer", post_keys, post_attrs, [user_idx])
        for kind in ("interested", "rated"):
            t[f"{c}-{kind}"] = make_table(ddb, f"eabu-{c}-{kind}", [("ID", "HASH"), ("cUserId", "RANGE")],
                                          [("ID", "N"), ("cUserId", "S")])
    t["favourites"] = make_table(ddb, "eabu-favourites", [("userId", "HASH"), ("favKey", "RANGE")],
                                 [("userId", "S"), ("favKey", "S")])
    t["ads"] = make_table(ddb, "eabu-advertisement", post_keys, post_attrs[:2])
    t["credits"] = make_table(ddb, "eabu-post-credits", [("userId", "HASH")], [("userId", "S")])
    t["identity"] = make_table(ddb, "eabu-identity", [("userId", "HASH")], [("userId", "S")])
    t["service"] = make_table(ddb, "eabu-service-request", [("userId", "HASH"), ("serviceType", "RANGE")],
                              [("userId", "S"), ("serviceType", "S")])
    t["feedback"] = make_table(ddb, "eabu-feedback", [("createdAt", "HASH"), ("ID", "RANGE")],
                               [("createdAt", "S"), ("ID", "N")])
    t["delete"] = make_table(ddb, "eabu-delete", [("userId", "HASH")], [("userId", "S")])
    t["blocked"] = make_table(ddb, "eabu-blocked-emails", [("emailHash", "HASH")], [("emailHash", "S")])

    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    cognito = boto3.client("cognito-idp", region_name=REGION)
    pool = cognito.create_user_pool(PoolName="eabu")["UserPool"]["Id"]
    monkeypatch.setenv("USER_POOL_ID", pool)
    for user, email in (("alice", "Alice@Example.com"), ("bob", "bob@example.com")):
        cognito.admin_create_user(UserPoolId=pool, Username=user,
                                  UserAttributes=[{"Name": "email", "Value": email}])

    # Alice (being deleted) and Bob (must be untouched) each have data everywhere.
    for n, user in enumerate(("alice", "bob")):
        base = 10 * (n + 1)
        for c in CATS:
            t[f"{c}-consumer"].put_item(Item={"zipcode": "19060", "ID": base, "userId": user})
            t[f"{c}-provider"].put_item(Item={"zipcode": "19060", "ID": base + 1, "userId": user, "picture1": "t"})
            s3.put_object(Bucket=BUCKET, Key=f"listings/{c}/{base + 1}/1.jpg", Body=b"x")
        s3.put_object(Bucket=BUCKET, Key=f"{user}/profile.jpg", Body=b"x")
        t["credits"].put_item(Item={"userId": user, "balance": 3})
        t["credits"].put_item(Item={"userId": f"tx#{user}", "ownerUserId": user, "posts": 15})
        t["identity"].put_item(Item={"userId": user})
        t["service"].put_item(Item={"userId": user, "serviceType": "x"})
        t["ads"].put_item(Item={"zipcode": "19060", "ID": base, "ownerUserId": user})
    other = {"alice": "bob", "bob": "alice"}
    for user in ("alice", "bob"):
        mine, theirs = (10, 21) if user == "alice" else (20, 11)
        for c in CATS:
            # An offer this user made on the other's request, and interest/rating on the other's service.
            t[f"{c}-offer"].put_item(Item={"zipcode": "19060", "ID": mine, "userId": other[user], "pUserId": user,
                                           "itemId": 10 if user == "bob" else 20})
            t[f"{c}-interested"].put_item(Item={"ID": theirs, "cUserId": user})
            t[f"{c}-rated"].put_item(Item={"ID": theirs, "cUserId": user})
        t["favourites"].put_item(Item={"userId": user, "favKey": f"seller#{other[user]}", "favUserId": other[user]})
    t["feedback"].put_item(Item={"createdAt": "2026-11-01", "ID": 1, "email": "alice@example.com "})
    t["feedback"].put_item(Item={"createdAt": "2026-11-02", "ID": 2, "email": "bob@example.com"})
    t["eat-consumer"].put_item(Item={"zipcode": "11111", "ID": 100, "total_count": 30})
    return t, s3, cognito, pool


def users_in(cognito, pool):
    return sorted(u["Username"] for u in cognito.list_users(UserPoolId=pool)["Users"])


def owners(table, field="userId"):
    return sorted(str(i.get(field)) for i in table.scan()["Items"] if i.get(field) is not None)


@mock_aws
def test_delete_account_removes_only_the_callers_data(monkeypatch):
    t, s3, cognito, pool = setup(monkeypatch)
    fn = load("eabu-delete-user-info")

    res = fn.lambda_handler(as_user(post({"userId": "alice"}), "alice"), None)
    assert res["statusCode"] == 200

    assert users_in(cognito, pool) == ["bob"]
    for c in CATS:
        assert owners(t[f"{c}-consumer"]) == ["bob"]
        assert owners(t[f"{c}-provider"]) == ["bob"]
        assert t[f"{c}-offer"].scan()["Count"] == 0, "offers alice made and offers made on her requests are gone"
        assert owners(t[f"{c}-interested"], "cUserId") == [], "bob's interest was on alice's deleted service"
        assert owners(t[f"{c}-rated"], "cUserId") == []
    assert owners(t["favourites"]) == [], "alice's favourites and bob's favourite of alice are gone"
    assert owners(t["ads"], "ownerUserId") == ["bob"]
    assert owners(t["identity"]) == ["bob"]
    assert owners(t["service"]) == ["bob"]
    assert [i["email"] for i in t["feedback"].scan()["Items"]] == ["bob@example.com"]
    credits = {i["userId"]: i for i in t["credits"].scan()["Items"]}
    assert "alice" not in credits and "bob" in credits
    assert "ownerUserId" not in credits["tx#alice"], "purchase record kept but unlinked"
    assert credits["tx#bob"]["ownerUserId"] == "bob"
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", []))
    assert keys == ["bob/profile.jpg"] + [f"listings/{c}/21/1.jpg" for c in sorted(CATS)]
    assert t["delete"].scan()["Count"] == 0
    assert t["eat-consumer"].get_item(Key={"zipcode": "11111", "ID": 100}).get("Item"), "counter row kept"

    blocked = t["blocked"].scan()["Items"]
    assert [b["emailHash"] for b in blocked] == [fn.helpers.email_hash("alice@example.com")]
    assert "alice" not in str(blocked).lower(), "only a hash of the email is stored"
    assert 29 * 86400 < int(blocked[0]["expiresAt"]) - int(blocked[0]["blockedAt"]) <= 30 * 86400

    again = fn.lambda_handler(as_user(post({"userId": "alice"}), "alice"), None)
    assert again["statusCode"] == 200, "deleting again is harmless"


@mock_aws
def test_cannot_delete_someone_else(monkeypatch):
    t, s3, cognito, pool = setup(monkeypatch)
    fn = load("eabu-delete-user-info")
    assert fn.lambda_handler(as_user(post({"userId": "bob"}), "alice"), None)["statusCode"] == 403
    assert fn.lambda_handler(post({"userId": "bob"}), None)["statusCode"] == 401
    assert users_in(cognito, pool) == ["alice", "bob"]
    assert owners(t["eat-provider"]) == ["alice", "bob"]


def signup_event(email, source="PreSignUp_SignUp"):
    return {"triggerSource": source, "request": {"userAttributes": {"email": email}}, "response": {}}


@mock_aws
def test_deleted_email_cannot_sign_up_for_30_days(monkeypatch):
    t, s3, cognito, pool = setup(monkeypatch)
    load("eabu-delete-user-info").lambda_handler(as_user(post({"userId": "alice"}), "alice"), None)
    trigger = load("eabu-pre-signup")

    with pytest.raises(trigger.EmailBlocked) as refused:
        trigger.lambda_handler(signup_event(" ALICE@example.com"), None)
    assert "sign up with it again from" in str(refused.value)
    assert trigger.lambda_handler(signup_event("bob2@example.com"), None)["triggerSource"] == "PreSignUp_SignUp"
    assert trigger.lambda_handler(signup_event("alice@example.com", "PreSignUp_AdminCreateUser"), None), \
        "support can still create the account"

    # Once the 30 days are over (TTL may not have removed the row yet) the email works again.
    row = t["blocked"].scan()["Items"][0]
    t["blocked"].put_item(Item={**row, "expiresAt": int(row["blockedAt"]) - 1})
    assert trigger.lambda_handler(signup_event("alice@example.com"), None)


def test_signup_allowed_if_the_check_breaks(monkeypatch):
    trigger = load("eabu-pre-signup")
    from botocore.exceptions import ClientError

    def broken(**kwargs):
        raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, "GetItem")
    monkeypatch.setattr(trigger.blocked_emails, "get_item", broken)
    assert trigger.lambda_handler(signup_event("anyone@example.com"), None)
