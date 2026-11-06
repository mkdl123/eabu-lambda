"""Regression tests for the security fixes (run: python -m pytest tests/unit -v)."""
import base64, importlib.util, io, json, os, sys
from datetime import datetime, timezone
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
    check("[enforce] rating your own listing -> 409", ur.lambda_handler(as_user(post({**rb, "cUserId": "alice"}), "alice"), None)["statusCode"] == 409)
    check("[enforce] rating as yourself -> 200", ur.lambda_handler(as_user(post({**rb, "cUserId": "bob"}), "bob"), None)["statusCode"] == 200)
    check("[enforce] owner can still be rated after a rejected self-rating",
          prov.get_item(Key={"zipcode": "1", "ID": 5})["Item"]["ratingscount"] == 1)

    rp = load("consumer-repost")
    rpb = {"itemId": 7, "serviceCategory": "Eat", "zipcode": "1"}
    check("[enforce] repost of someone else's item rejected", rp.lambda_handler(as_user(post(rpb), "mallory"), None)["statusCode"] == 404)
    ap = load("eabu-advertisement-post")
    ad = {"address": "a", "name": "n", "email": "e@x.com", "phone": "1", "zipcode": "1", "title": "t", "subtitle": "s", "description": "d"}
    check("[enforce] ad submission needs sign-in", ap.lambda_handler(post(ad), None)["statusCode"] == 401)

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
    body = json.loads(res["body"])
    check("admin-approved ad is listed", res["statusCode"] == 200 and body["Count"] == 1)
    check("public fields kept", body["Items"][0]["title"] == "t" and body["Items"][0]["url"] == "https://example.com/promo")
    check("submitter contact details never listed",
          not {"name", "email", "phone", "address"} & set(body["Items"][0]) and "e@x.com" not in res["body"])
    check("no AWS response metadata leaked", "ResponseMetadata" not in body)
    for hidden in ("draft", "rejected", "anything-else"):
        check(f"unlisted status '{hidden}' -> 403",
              ag.lambda_handler({"headers": {}, "pathParameters": {"status": hidden}}, None)["statusCode"] == 403)
    os.environ["AD_PUBLIC_STATUSES"] = "live"
    check("allowed statuses are configurable",
          ag.lambda_handler({"headers": {}, "pathParameters": {"status": "LIVE"}}, None)["statusCode"] == 200)
    del os.environ["AD_PUBLIC_STATUSES"]


@mock_aws
def test_get_user_only_finds_own_record():
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName="eabu-identity", BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "userId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("userId", "phone", "email")],
        GlobalSecondaryIndexes=[{"IndexName": f"{a}-index", "KeySchema": [{"AttributeName": a, "KeyType": "HASH"}],
                                 "Projection": {"ProjectionType": "ALL"}} for a in ("phone", "email")])
    ident = boto3.resource("dynamodb", region_name="us-east-1").Table("eabu-identity")
    ident.put_item(Item={"userId": "alice", "phone": "555", "email": "a@x.com"})
    ident.put_item(Item={"userId": "bob", "phone": "555", "email": "b@x.com"})
    gu = load("eabu-get-user")

    def lookup(param, uid=None):
        ev = {"pathParameters": {"param": param}}
        return gu.lambda_handler(as_user(ev, uid) if uid else ev, None)

    os.environ["AUTH_MODE"] = "enforce"
    try:
        check("[enforce] lookup needs sign-in", lookup("a@x.com")["statusCode"] == 401)
        check("[enforce] own email found", json.loads(lookup("a@x.com", "alice")["body"])["userId"] == "alice")
        check("[enforce] own phone found", json.loads(lookup("555", "alice")["body"])["userId"] == "alice")
        check("[enforce] shared phone returns the caller, not the first match",
              json.loads(lookup("555", "bob")["body"])["userId"] == "bob")
        other, missing = lookup("a@x.com", "mallory"), lookup("nobody@x.com", "mallory")
        check("[enforce] someone else's email looks the same as an unknown one",
              (other["statusCode"], other["body"]) == (missing["statusCode"], missing["body"]))
        check("[enforce] no userId revealed", "alice" not in other["body"])
    finally:
        os.environ["AUTH_MODE"] = "off"


def mk_with_user_index(name):
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName=name, BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "zipcode", "KeyType": "HASH"}, {"AttributeName": "ID", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "zipcode", "AttributeType": "S"}, {"AttributeName": "ID", "AttributeType": "N"},
                              {"AttributeName": "userId", "AttributeType": "S"}],
        GlobalSecondaryIndexes=[{"IndexName": "userId-index", "KeySchema": [{"AttributeName": "userId", "KeyType": "HASH"}],
                                 "Projection": {"ProjectionType": "ALL"}}])
    return boto3.resource("dynamodb", region_name="us-east-1").Table(name)


@mock_aws
def test_browsing_consumer_requests():
    now = datetime.now(timezone.utc).isoformat()
    for cat in ("eat", "live", "move"):
        mk_with_user_index(f"eabu-{cat}-consumer").put_item(Item={
            "zipcode": "19060", "ID": 101, "userId": "alice", "title": "t", "createdAt": now,
            "latitude": "39.8512345", "longitude": "-75.4312345"})
        pg = load(f"provider-get-{cat}")

        def get(user_path, caller=None):
            ev = {"pathParameters": {"zip": "19060", "userId": user_path}}
            return pg.lambda_handler(as_user(ev, caller) if caller else ev, None)

        os.environ["AUTH_MODE"] = "enforce"
        try:
            check(f"[enforce] {cat}: anonymous browsing -> 401", get("NA")["statusCode"] == 401)
            res = get("NA", "bob")
            item = json.loads(res["body"])["Items"][0]
            check(f"{cat}: others see the area only", (item["latitude"], item["longitude"]) == ("39.85", "-75.43"))
            check(f"{cat}: exact location not in the response", "39.8512345" not in res["body"])
            check(f"{cat}: no AWS metadata", "ResponseMetadata" not in res["body"])
            check(f"{cat}: userId kept so an offer can be made", item["userId"] == "alice")
            own = json.loads(get("alice", "alice")["body"])["Items"][0]
            check(f"{cat}: owner keeps the exact location", own["latitude"] == "39.8512345")
            check(f"[enforce] {cat}: someone else's list -> 403", get("alice", "bob")["statusCode"] == 403)
        finally:
            os.environ["AUTH_MODE"] = "off"


@mock_aws
def test_offers_need_a_real_request():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    offer = {"itemId": 101, "userId": "alice", "pUserId": "bob", "title": "t", "description": "d", "offerDescription": "o",
             "phoneNumber": "1", "email": "b@x.com", "price": "10", "zip": "19060", "latitude": 39.85, "longitude": "-75.43"}
    for cat in ("eat", "live", "move"):
        mk_with_user_index(f"eabu-{cat}-consumer").put_item(Item={"zipcode": "19060", "ID": 101, "userId": "alice"})
        offers = mk(ddb, f"eabu-{cat}-offer", "zipcode", "S", "ID", "N")
        m = load(f"provider-offer-{cat}")

        def send(**changes):
            return m.lambda_handler(post({**offer, **changes}), None)["statusCode"]

        check(f"{cat}: real offer accepted", send() == 200)
        check(f"{cat}: nonexistent item -> 404", send(itemId=999) == 404)
        check(f"{cat}: item of a different consumer -> 404", send(userId="carol") == 404)
        check(f"{cat}: non-numeric itemId -> 400 not crash", send(itemId="abc") == 400)
        check(f"{cat}: decimal price -> 400 not crash", send(price=9.5) == 400)
        check(f"{cat}: oversized offer -> 400", send(offerDescription="x" * 5001) == 400)
        missing = {k: v for k, v in offer.items() if k != "zip"}
        check(f"{cat}: missing zip -> 400 not crash", m.lambda_handler(post(missing), None)["statusCode"] == 400)
        check(f"{cat}: rejected offers are not stored or counted",
              [i["ID"] for i in offers.scan()["Items"] if i["ID"] != 100] == [1])


@mock_aws
def test_input_limits():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    mk(ddb, "eabu-eat-provider", "zipcode", "S", "ID", "N")
    feedback_table = mk(ddb, "eabu-feedback", "ID", "N")
    listing = {"zipcode": "1", "title": "t", "description": "d", "phone": "1", "userId": "a", "latitude": 39.85, "longitude": -75.43}
    pp = load("provider-post-eat")
    check("coordinates may be decimal numbers", pp.lambda_handler(post(listing), None)["statusCode"] == 200)
    check("whole-number phone still accepted", pp.lambda_handler(post({**listing, "phone": 6105550100}), None)["statusCode"] == 200)
    check("oversized title -> 400", pp.lambda_handler(post({**listing, "title": "x" * 201}), None)["statusCode"] == 400)
    check("list as title -> 400", pp.lambda_handler(post({**listing, "title": ["x"]}), None)["statusCode"] == 400)
    for body in ("[1]", "5", '"zipcode title description phone"'):
        check(f"non-object body {body} -> 400", pp.lambda_handler({"httpMethod": "POST", "body": body}, None)["statusCode"] == 400)

    fb = load("eabu-feedback-submit")
    check("normal feedback saved", fb.lambda_handler(post({"feedback": "x" * 2000}), None)["statusCode"] == 200)
    check("oversized feedback -> 400", fb.lambda_handler(post({"feedback": "x" * 2001}), None)["statusCode"] == 400)
    check("oversized email -> 400", fb.lambda_handler(post({"feedback": "x", "email": "e" * 255}), None)["statusCode"] == 400)
    check("non-object feedback -> 400 not crash", fb.lambda_handler({"httpMethod": "POST", "body": "[1]"}, None)["statusCode"] == 400)
    check("only the valid feedback was stored", feedback_table.scan()["Count"] == 1)

    ap = load("eabu-advertisement-post")
    ad = {"address": "a", "name": "n", "email": "e@x.com", "phone": "1", "zipcode": "1", "title": "t", "subtitle": "s", "description": "d"}
    check("oversized ad url -> 400", ap.lambda_handler(post({**ad, "url": "https://x.co/" + "a" * 2048}), None)["statusCode"] == 400)


@mock_aws
def test_identity_response_hides_bucket():
    mk(boto3.resource("dynamodb", region_name="us-east-1"), "eabu-identity", "userId", "S")
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="eabu-identity-pictures")
    img = jpeg_b64()
    res = load("eabu-identity").lambda_handler(post({"user_id": "u1", "selfie": img, "license_front": img, "license_back": img,
                                                     "address": "a", "full_name": "n", "email": "e@x.com", "phone": "1"}), None)
    check("upload ok, bucket name not returned", res["statusCode"] == 200 and "eabu-identity-pictures" not in res["body"])


@mock_aws
def test_move_request_is_stored_and_owner_checked():
    t = mk(boto3.resource("dynamodb", region_name="us-east-1"), "eabu-move-consumer", "zipcode", "S", "ID", "N")
    m = load("consumer-post-move")
    body = {"zipcode": "1", "title": "t", "description": "d", "userId": "alice", "latitude": 1.5, "longitude": 2.5}
    os.environ["AUTH_MODE"] = "enforce"
    try:
        check("[enforce] posting as someone else -> 403", m.lambda_handler(as_user(post(body), "mallory"), None)["statusCode"] == 403)
        res = m.lambda_handler(as_user(post(body), "alice"), None)
        check("request stored", res["statusCode"] == 200 and t.get_item(Key={"zipcode": "1", "ID": 1})["Item"]["userId"] == "alice")
    finally:
        os.environ["AUTH_MODE"] = "off"


def test_auth_fails_closed_when_mode_is_missing():
    helpers = load("eabu-user").helpers
    del os.environ["AUTH_MODE"]
    try:
        check("missing AUTH_MODE means enforce", helpers.auth_enforced())
    finally:
        os.environ["AUTH_MODE"] = "off"


CORS_FUNCTIONS = ["eabu-get-user", "eabu-user", "eabu-identity", "eabu-identity-check", "eabu-advertisement-get", "eabu-get-user-ratings"] + [
    name.format(c) for c in ("eat", "live", "move")
    for name in ("consumer-get-{}", "provider-get-{}", "consumer-{}-offers", "provider-my-{}-services")]


@mock_aws
def test_browser_clients_keep_cors_header():
    # No tables exist, so most of these take their error path: that response needs the header too.
    params = {k: "1" for k in ("zip", "userId", "itemId", "pUserId", "param", "status")}
    for name in CORS_FUNCTIONS:
        res = load(name).lambda_handler({"pathParameters": params, "httpMethod": "GET"}, None)
        check(f"{name} sends the CORS header", res["headers"]["Access-Control-Allow-Origin"] == "*")
