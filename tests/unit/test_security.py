"""Regression tests for the security fixes (run: python -m pytest tests/unit -v)."""
import base64, importlib.util, io, json, os, sys
os.environ.update(AUTH_MODE="off", AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x", AWS_DEFAULT_REGION="us-east-1")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "eabu-dish/python"))

import boto3
from moto import mock_aws
from PIL import Image


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), os.path.join(ROOT, "eabu-dish", name, "app.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def mk(ddb, name, hk, hkt, rk=None, rkt=None):
    ks = [{"AttributeName": hk, "KeyType": "HASH"}]; ad = [{"AttributeName": hk, "AttributeType": hkt}]
    if rk:
        ks.append({"AttributeName": rk, "KeyType": "RANGE"}); ad.append({"AttributeName": rk, "AttributeType": rkt})
    return ddb.create_table(TableName=name, KeySchema=ks, AttributeDefinitions=ad, BillingMode="PAY_PER_REQUEST")


def post(body):
    return {"httpMethod": "POST", "body": json.dumps(body)}


def check(label, cond):
    assert cond, label


@mock_aws
def test_ratings_and_interest():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    prov = mk(ddb, "eabu-eat-provider", "zipcode", "S", "ID", "N")
    mk(ddb, "eabu-eat-rated", "ID", "N", "cUserId", "S")
    mk(ddb, "eabu-eat-interested", "ID", "N", "cUserId", "S")
    prov.put_item(Item={"zipcode": "19060", "ID": 101, "totalstars": 0, "ratingscount": 0, "interested_by": 0})
    r = load("eabu-update-ratings")
    base = {"zipcode": "19060", "ID": 101, "providerType": "eat"}

    res = r.lambda_handler(post({**base, "totalstars": 5, "ratingscount": 1, "cUserId": "a"}), None)
    check("first legit 5-star rating accepted", res["statusCode"] == 200)
    res = r.lambda_handler(post({**base, "totalstars": 9, "ratingscount": 2, "cUserId": "b"}), None)
    check("second legit 4-star rating accepted", res["statusCode"] == 200)
    res = r.lambda_handler(post({**base, "totalstars": 1000, "ratingscount": 3, "cUserId": "c"}), None)
    check("inflated total rejected (400)", res["statusCode"] == 400)
    res = r.lambda_handler(post({**base, "totalstars": 15, "ratingscount": 3, "cUserId": "d"}), None)
    check("+6 stars in one vote rejected (409)", res["statusCode"] == 409)
    res = r.lambda_handler(post({**base, "totalstars": 50, "ratingscount": 10, "cUserId": "e"}), None)
    check("skipping ratingscount rejected (409)", res["statusCode"] == 409)
    res = r.lambda_handler(post({**base, "totalstars": 12, "ratingscount": 3, "cUserId": "d"}), None)
    check("rejected voter can retry after rollback", res["statusCode"] == 200)
    res = r.lambda_handler(post({**base, "totalstars": 15, "ratingscount": 4, "cUserId": "a"}), None)
    check("duplicate voter still blocked", "already rated" in json.loads(res["body"])["message"])
    item = prov.get_item(Key={"zipcode": "19060", "ID": 101})["Item"]
    check(f"stored totals are 12/3 (got {item['totalstars']}/{item['ratingscount']})",
          item["totalstars"] == 12 and item["ratingscount"] == 3)
    res = r.lambda_handler(post({**base, "ID": 999, "totalstars": 5, "ratingscount": 1, "cUserId": "a"}), None)
    check("missing item -> 409 and no table name leaked", res["statusCode"] == 409 and "eabu-" not in res["body"])
    res = r.lambda_handler(post({**base, "totalstars": "abc", "ratingscount": 1, "cUserId": "z"}), None)
    check("non-numeric rating -> 400 not crash", res["statusCode"] == 400)

    i = load("eabu-update-interested")
    ib = {"zipcode": "19060", "ID": 101, "providerType": "eat"}
    check("interest 0->1 accepted", i.lambda_handler(post({**ib, "interested": 1, "cUserId": "a"}), None)["statusCode"] == 200)
    check("interest jump to 500 rejected", i.lambda_handler(post({**ib, "interested": 500, "cUserId": "b"}), None)["statusCode"] == 409)
    check("interest 1->2 accepted after rollback", i.lambda_handler(post({**ib, "interested": 2, "cUserId": "b"}), None)["statusCode"] == 200)


def jpeg_b64(size=(64, 64)):
    buf = io.BytesIO(); Image.new("RGB", size, (200, 10, 10)).save(buf, format="JPEG"); return base64.b64encode(buf.getvalue()).decode()


@mock_aws
def test_identity():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    tbl = mk(ddb, "eabu-identity", "userId", "S")
    s3 = boto3.client("s3", region_name="us-east-1"); s3.create_bucket(Bucket="eabu-identity-pictures")
    m = load("eabu-identity")
    img = jpeg_b64()
    body = {"user_id": "user123", "selfie": img, "license_front": img, "license_back": img,
            "address": "1 Main", "full_name": "A B", "email": "a@b.c", "phone": "123"}

    res = m.lambda_handler(post(body), None)
    check("valid identity submission accepted", res["statusCode"] == 200)
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket="eabu-identity-pictures")["Contents"])
    check("three images uploaded under user prefix", keys == ["user123/license_back.jpg", "user123/license_front.jpg", "user123/selfie.jpg"])

    res = m.lambda_handler(post({**body, "user_id": "../other"}), None)
    check("path-like user_id rejected", res["statusCode"] == 400)
    res = m.lambda_handler(post({**body, "user_id": "a/b"}), None)
    check("user_id with slash rejected", res["statusCode"] == 400)

    res = m.lambda_handler(post({**body, "user_id": "u2", "license_back": base64.b64encode(b"<?php evil ?>").decode()}), None)
    check("non-image rejected with no details leaked", res["statusCode"] == 400 and "details" not in res["body"])
    check("no partial upload for rejected submission",
          not any(o["Key"].startswith("u2/") for o in s3.list_objects_v2(Bucket="eabu-identity-pictures")["Contents"]))

    gif = io.BytesIO(); Image.new("RGB", (8, 8)).save(gif, format="GIF")
    res = m.lambda_handler(post({**body, "user_id": "u3", "selfie": base64.b64encode(gif.getvalue()).decode()}), None)
    check("disallowed format (GIF) rejected", res["statusCode"] == 400)

    tbl.update_item(Key={"userId": "user123"}, UpdateExpression="SET verified = :t", ExpressionAttributeValues={":t": True})
    before = s3.get_object(Bucket="eabu-identity-pictures", Key="user123/selfie.jpg")["ETag"]
    res = m.lambda_handler(post({**body, "selfie": jpeg_b64((32, 32))}), None)
    after = s3.get_object(Bucket="eabu-identity-pictures", Key="user123/selfie.jpg")["ETag"]
    check("verified identity cannot be overwritten (409)", res["statusCode"] == 409)
    check("verified user's selfie left untouched", before == after)
    check("verified flag preserved", tbl.get_item(Key={"userId": "user123"})["Item"]["verified"] is True)


@mock_aws
def test_counter_not_burned():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    t = mk(ddb, "eabu-eat-provider", "zipcode", "S", "ID", "N")
    m = load("provider-post-eat")
    m.lambda_handler({"httpMethod": "GET"}, None)
    m.lambda_handler(post({"zipcode": "1"}), None)
    check("invalid requests do not consume IDs", "Item" not in t.get_item(Key={"ID": 100, "zipcode": "11111"}))


def as_user(ev, uid):
    ev = dict(ev); ev["requestContext"] = {"authorizer": {"claims": {"sub": uid}}}; return ev


@mock_aws
def test_auth():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    prov = mk(ddb, "eabu-eat-provider", "zipcode", "S", "ID", "N")
    cons = mk(ddb, "eabu-eat-consumer", "zipcode", "S", "ID", "N")
    ident = mk(ddb, "eabu-identity", "userId", "S")
    mk(ddb, "eabu-eat-rated", "ID", "N", "cUserId", "S")
    ident.put_item(Item={"userId": "alice", "verified": True, "submitted": True, "email": "a@x.com", "phone": "555", "address": "1 Main", "fullname": "Alice"})
    prov.put_item(Item={"zipcode": "1", "ID": 5, "userId": "alice", "totalstars": 0, "ratingscount": 0})
    cons.put_item(Item={"zipcode": "1", "ID": 7, "userId": "alice", "createdAt": "x"})
    post_body = {"zipcode": "1", "title": "t", "description": "d", "phone": "1", "userId": "alice", "latitude": 0, "longitude": 0}

    os.environ["AUTH_MODE"] = "off"
    pp = load("provider-post-eat")
    check("[off] unauthenticated post still works", pp.lambda_handler(post(post_body), None)["statusCode"] == 200)
    ic = load("eabu-identity-check")
    r = ic.lambda_handler({"pathParameters": {"userId": "alice"}}, None)
    check("[off] identity-check unchanged", "a@x.com" in r["body"])

    os.environ["AUTH_MODE"] = "enforce"
    check("[enforce] unauthenticated post -> 401", pp.lambda_handler(post(post_body), None)["statusCode"] == 401)
    check("[enforce] posting as someone else -> 403", pp.lambda_handler(as_user(post(post_body), "mallory"), None)["statusCode"] == 403)
    check("[enforce] posting as yourself -> 200", pp.lambda_handler(as_user(post(post_body), "alice"), None)["statusCode"] == 200)
    check("[enforce] forged claims in body do not help",
          pp.lambda_handler(post({**post_body, "requestContext": {"authorizer": {"claims": {"sub": "alice"}}}}), None)["statusCode"] == 401)

    r = ic.lambda_handler(as_user({"pathParameters": {"userId": "alice"}}, "mallory"), None)
    check("[enforce] other user sees only verified flag", r["statusCode"] == 200 and "a@x.com" not in r["body"] and "555" not in r["body"] and '"verified": true' in r["body"])
    r = ic.lambda_handler(as_user({"pathParameters": {"userId": "alice"}}, "alice"), None)
    check("[enforce] owner sees own full record", "a@x.com" in r["body"])
    check("[enforce] identity-check unauthenticated -> 401", ic.lambda_handler({"pathParameters": {"userId": "alice"}}, None)["statusCode"] == 401)

    eu = load("eabu-user")
    check("[enforce] selfie URL needs sign-in", eu.lambda_handler({"pathParameters": {"pUserId": "alice"}}, None)["statusCode"] == 401)

    ur = load("eabu-update-ratings")
    rb = {"zipcode": "1", "ID": 5, "providerType": "eat", "totalstars": 5, "ratingscount": 1, "cUserId": "alice2"}
    check("[enforce] rating with someone else's cUserId -> 403", ur.lambda_handler(as_user(post(rb), "mallory"), None)["statusCode"] == 403)
    check("[enforce] rating as yourself -> 200", ur.lambda_handler(as_user(post({**rb, "cUserId": "bob"}), "bob"), None)["statusCode"] == 200)

    rp = load("consumer-repost")
    rpb = {"itemId": 7, "serviceCategory": "Eat", "zipcode": "1"}
    check("[enforce] repost of someone else's item rejected", rp.lambda_handler(as_user(post(rpb), "mallory"), None)["statusCode"] == 404)
    check("[enforce] owner can repost", rp.lambda_handler(as_user(post(rpb), "alice"), None)["statusCode"] == 200)

    co = load("consumer-eat-offers")
    check("[enforce] reading another user's offers -> 403",
          co.lambda_handler(as_user({"pathParameters": {"userId": "alice", "itemId": "NA"}}, "mallory"), None)["statusCode"] == 403)
    os.environ["AUTH_MODE"] = "off"



@mock_aws
def test_ads_require_review():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ads = ddb.create_table(
        TableName="eabu-advertisement",
        KeySchema=[{"AttributeName": "zipcode", "KeyType": "HASH"}, {"AttributeName": "ID", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "zipcode", "AttributeType": "S"}, {"AttributeName": "ID", "AttributeType": "N"},
                              {"AttributeName": "status", "AttributeType": "S"}],
        GlobalSecondaryIndexes=[{"IndexName": "status-index", "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}],
                                 "Projection": {"ProjectionType": "ALL"}}],
        BillingMode="PAY_PER_REQUEST")
    ap = load("eabu-advertisement-post")
    ad = {"address": "1 Main", "name": "Eve", "email": "e@x.com", "phone": "1", "zipcode": "19060", "title": "t",
          "subtitle": "s", "description": "d", "status": "active", "country": "US", "url": "https://example.com/promo"}

    res = ap.lambda_handler(post(ad), None)
    check("ad request accepted", res["statusCode"] == 200)
    stored = ads.get_item(Key={"zipcode": "19060", "ID": json.loads(res["body"])["ID"]})["Item"]
    check("client-supplied 'active' status ignored -> pending", stored["status"] == "pending")

    for bad in ("javascript:alert(1)", "http://example.com", "data:text/html,hi", "https://", "https://ex ample.com"):
        check(f"bad url rejected: {bad}", ap.lambda_handler(post({**ad, "url": bad}), None)["statusCode"] == 400)
    check("missing optional url/country/status no longer crashes",
          ap.lambda_handler(post({k: v for k, v in ad.items() if k not in ("url", "country", "status")}), None)["statusCode"] == 200)
    check("over-long description rejected", ap.lambda_handler(post({**ad, "description": "x" * 5000}), None)["statusCode"] == 400)

    ag = load("eabu-advertisement-get")
    check("listing pending ads (private contact data) -> 403",
          ag.lambda_handler({"headers": {}, "pathParameters": {"status": "pending"}}, None)["statusCode"] == 403)
    check("listing PENDING (case) -> 403",
          ag.lambda_handler({"headers": {}, "pathParameters": {"status": "PENDING"}}, None)["statusCode"] == 403)
    ads.update_item(Key={"zipcode": "19060", "ID": stored["ID"]}, UpdateExpression="SET #s = :a",
                    ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":a": "active"})
    res = ag.lambda_handler({"headers": {}, "pathParameters": {"status": "active"}}, None)
    check("admin-approved ad is listed", res["statusCode"] == 200 and json.loads(res["body"])["Count"] == 1)
