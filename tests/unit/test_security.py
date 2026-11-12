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
    extra = {}
    if name.endswith("-provider"):  # like production: seller posts query userId-index (post limit)
        ad.append({"AttributeName": "userId", "AttributeType": "S"})
        extra["GlobalSecondaryIndexes"] = [{"IndexName": "userId-index", "Projection": {"ProjectionType": "ALL"},
                                            "KeySchema": [{"AttributeName": "userId", "KeyType": "HASH"}]}]
    table = ddb.create_table(TableName=name, KeySchema=ks, AttributeDefinitions=ad, BillingMode="PAY_PER_REQUEST", **extra)
    if name == "eabu-eat-provider":
        # Seller posts count this month's free posts across all four seller tables, so
        # (as in production) the others exist too.
        existing = ddb.meta.client.list_tables()["TableNames"]
        for other in ("eabu-live-provider", "eabu-move-provider", "eabu-lifestyle-provider"):
            if other not in existing:
                mk(ddb, other, "zipcode", "S", "ID", "N")
    return table


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
def test_profile_picture_upload_replace_delete():
    s3 = boto3.client("s3", region_name="us-east-1"); s3.create_bucket(Bucket="eabu-identity-pictures")
    m = load("eabu-profile-picture")
    keys = lambda: sorted(o["Key"] for o in s3.list_objects_v2(Bucket="eabu-identity-pictures").get("Contents", []))
    picture = lambda uid="user123": s3.get_object(Bucket="eabu-identity-pictures", Key=f"{uid}/profile.jpg")["Body"].read()
    up = lambda uid, img: post({"user_id": uid, "picture": img})

    res = m.lambda_handler(up("user123", jpeg_b64()), None)
    check("upload accepted", res["statusCode"] == 200)
    check("only the profile picture is stored", keys() == ["user123/profile.jpg"])
    check("response hides bucket name", "eabu-identity-pictures" not in res["body"])

    # The old identity payload (selfie + licence images) must not store anything.
    res = m.lambda_handler(post({"user_id": "old", "selfie": jpeg_b64(), "license_front": jpeg_b64(),
                                 "license_back": jpeg_b64()}), None)
    check("old identity payload rejected", res["statusCode"] == 400)
    check("no selfie or licence stored", not any(k.startswith("old/") for k in keys()))

    first = picture()
    res = m.lambda_handler(up("user123", jpeg_b64((32, 32))), None)
    check("re-upload replaces the picture", res["statusCode"] == 200 and picture() != first)
    check("still exactly one picture for the user", [k for k in keys() if k.startswith("user123/")] == ["user123/profile.jpg"])

    big = io.BytesIO(); Image.new("RGB", (3000, 2000)).save(big, format="JPEG")
    m.lambda_handler(up("big", base64.b64encode(big.getvalue()).decode()), None)
    check("large pictures shrunk to 1024px", max(Image.open(io.BytesIO(picture("big"))).size) <= 1024)

    check("missing picture -> 400", m.lambda_handler(post({"user_id": "u9"}), None)["statusCode"] == 400)
    check("non-image -> 400", m.lambda_handler(up("u9", base64.b64encode(b"<?php evil ?>").decode()), None)["statusCode"] == 400)
    gif = io.BytesIO(); Image.new("RGB", (8, 8)).save(gif, format="GIF")
    check("disallowed format (GIF) -> 400", m.lambda_handler(up("u9", base64.b64encode(gif.getvalue()).decode()), None)["statusCode"] == 400)
    check("path traversal user_id -> 400", m.lambda_handler(up("../x", jpeg_b64()), None)["statusCode"] == 400)
    check("rejected uploads store nothing", not any(k.startswith("u9/") for k in keys()))

    delete = lambda uid: {"httpMethod": "DELETE", "pathParameters": {"userId": uid}}
    m.lambda_handler(up("other", jpeg_b64()), None)
    res = m.lambda_handler(delete("user123"), None)
    check("delete succeeds", res["statusCode"] == 200 and "user123/profile.jpg" not in keys())
    check("deleting again is harmless", m.lambda_handler(delete("user123"), None)["statusCode"] == 200)
    check("other users' pictures untouched", "other/profile.jpg" in keys())
    check("can upload again after delete", m.lambda_handler(up("user123", jpeg_b64()), None)["statusCode"] == 200)
    check("unsupported method -> 405", m.lambda_handler({"httpMethod": "GET"}, None)["statusCode"] == 405)

    os.environ["AUTH_MODE"] = "enforce"
    try:
        check("[enforce] upload for someone else -> 403", m.lambda_handler(as_user(up("other", jpeg_b64()), "mallory"), None)["statusCode"] == 403)
        check("[enforce] delete someone else's picture -> 403", m.lambda_handler(as_user(delete("other"), "mallory"), None)["statusCode"] == 403)
        check("[enforce] unauthenticated delete -> 401", m.lambda_handler(delete("other"), None)["statusCode"] == 401)
        check("[enforce] rejected delete leaves picture", "other/profile.jpg" in keys())
        check("[enforce] owner can delete", m.lambda_handler(as_user(delete("other"), "other"), None)["statusCode"] == 200 and "other/profile.jpg" not in keys())
    finally:
        os.environ["AUTH_MODE"] = "off"


@mock_aws
def test_profile_picture_link_only_when_present():
    s3 = boto3.client("s3", region_name="us-east-1"); s3.create_bucket(Bucket="eabu-identity-pictures")
    eu = load("eabu-user")
    get = lambda uid: json.loads(eu.lambda_handler({"pathParameters": {"pUserId": uid}}, None)["body"])
    check("no picture -> url is null", get("nobody") == {"url": None})
    s3.put_object(Bucket="eabu-identity-pictures", Key="carol/selfie.jpg", Body=b"x")
    check("an old selfie is never served", get("carol") == {"url": None})
    s3.put_object(Bucket="eabu-identity-pictures", Key="alice/profile.jpg", Body=b"x")
    url = get("alice")["url"]
    check("picture -> presigned link to it", url and "alice/profile.jpg" in url)


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
    check("[enforce] profile picture URL needs sign-in", eu.lambda_handler({"pathParameters": {"pUserId": "alice"}}, None)["statusCode"] == 401)

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
def test_seller_profile_shows_public_listings():
    now = datetime.now(timezone.utc).isoformat()
    for cat in ("eat", "live", "move", "lifestyle"):
        mk_with_user_index(f"eabu-{cat}-provider").put_item(Item={
            "zipcode": "19060", "ID": 101, "userId": "alice", "title": "Meals", "description": "d",
            "totalstars": 9, "ratingscount": 2, "createdAt": now, "phone": "5551234567",
            "latitude": "39.8512345", "longitude": "-75.4312345", "interested_by": ["bob"]})
        ms = load(f"provider-my-{cat}-services")

        def get(caller=None):
            ev = {"pathParameters": {"userId": "alice"}}
            return ms.lambda_handler(as_user(ev, caller) if caller else ev, None)

        os.environ["AUTH_MODE"] = "enforce"
        try:
            check(f"[enforce] {cat}: anonymous -> 401", get()["statusCode"] == 401)
            own = json.loads(get("alice")["body"])["Items"][0]
            check(f"{cat}: owner sees phone and interest", own["phone"] == "5551234567" and own["interested_by"] == ["bob"])
            res = get("bob")
            check(f"[enforce] {cat}: buyer can view a seller's listings", res["statusCode"] == 200)
            item = json.loads(res["body"])["Items"][0]
            check(f"{cat}: profile fields kept", (item["title"], str(item["ID"]), item["zipcode"], str(item["ratingscount"])) == ("Meals", "101", "19060", "2"))
            for secret in ("5551234567", "39.85", "interested_by", "ResponseMetadata"):
                check(f"{cat}: {secret} hidden from other users", secret not in res["body"])
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


CORS_FUNCTIONS = ["eabu-user", "eabu-profile-picture", "eabu-listing-picture", "eabu-favourites", "eabu-identity-check", "eabu-advertisement-get", "eabu-get-user-ratings"] + [
    name.format(c) for c in ("eat", "live", "move")
    for name in ("consumer-get-{}", "provider-get-{}", "consumer-{}-offers", "provider-my-{}-services")] + [
    "provider-my-lifestyle-services"]


@mock_aws
def test_browser_clients_keep_cors_header():
    # No tables exist, so most of these take their error path: that response needs the header too.
    params = {k: "1" for k in ("zip", "userId", "itemId", "pUserId", "param", "status")}
    for name in CORS_FUNCTIONS:
        res = load(name).lambda_handler({"pathParameters": params, "httpMethod": "GET"}, None)
        check(f"{name} sends the CORS header", res["headers"]["Access-Control-Allow-Origin"] == "*")
