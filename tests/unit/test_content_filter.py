"""Sexual content filter: blocked words in text, nudity in pictures (Rekognition), User Ids."""
import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from my_utils import content_filter
from test_security import load, post, jpeg_b64
from test_lifestyle import make_tables, body, ZIP, LOCATION, post_request, offer_payload
from test_listing_pictures import setup as listing_setup, upload, keys, BUCKET


@pytest.mark.parametrize("text", [
    "Sexy massage", "SEX tonight", "s3x", "$ex", "p0rn", "Pornography for sale", "xxx videos",
    "Send nudes", "Happy ending massage", "happy   ending", "body-rub", "Find me on OnlyFans",
    "only fans", "o.n.l.y.f.a.n.s", "visit xxpornhubxx", "escorts", "Erótica", "sugar daddy wanted",
    "https://pornhub.com/x", "milfs", "f*ck", "fuck",
])
def test_blocked_text(text):
    if text == "f*ck":
        assert not content_filter.text_is_blocked(text), "masked swearing isn't decoded"
        return
    assert content_filter.text_is_blocked(text), text


@pytest.mark.parametrize("text", [
    "Fresh peaches and cucumbers", "Middlesex and Sussex county", "Essex Street", "Data analysis tutor",
    "Nude lipstick and nail art", "Naked cake for weddings", "Kinky twist braids", "Baby bottle nipples",
    "Pet anal gland expression", "Magna cum laude math tutor", "Concrete vibrator rental",
    "Deep tissue massage", "Full service moving", "Cocktail bartender", "Same day delivery 555-0100",
    "Shih tzu grooming", "Scunthorpe pickup or nearby", "Therapist", "Cockpit cleaning", "", None, 42,
])
def test_allowed_text(text):
    assert not content_filter.text_is_blocked(text), text


def test_message_names_the_field_not_the_word():
    field = content_filter.blocked_text_field({"title": "Cakes", "offerDescription": "sexy cakes"})
    assert field == "offerDescription"
    msg = content_filter.blocked_text_message(field)
    assert "description" in msg and "sexy" not in msg


@mock_aws
@pytest.mark.parametrize("fn,payload", [
    ("provider-post-eat", {"title": "Sexy cakes", "description": "Fresh", "phone": "6105550100"}),
    ("provider-post-lifestyle", {"title": "Massage", "description": "Happy ending included", "phone": "6105550100"}),
    ("consumer-post-move", {"title": "Ride", "description": "Need a s3x partner"}),
    ("consumer-post-live", {"title": "Escort needed", "description": "Tonight"}),
])
def test_posts_with_blocked_words_are_refused(fn, payload):
    tables = make_tables()
    res = load(fn).lambda_handler(post({"zipcode": ZIP, "userId": "u1", **LOCATION, **payload}), None)
    assert res["statusCode"] == 400
    assert "aren't allowed" in body(res)["msg"]
    category, role = fn.split("-")[2], "provider" if fn.startswith("provider") else "consumer"
    assert tables[f"{category}-{role}"].scan()["Items"] == [], "nothing is saved"


@mock_aws
def test_clean_post_still_works():
    make_tables()
    res = load("provider-post-eat").lambda_handler(post(
        {"zipcode": ZIP, "userId": "u1", "title": "Naked cakes", "description": "Fresh peaches",
         "phone": "6105550100", **LOCATION}), None)
    assert res["statusCode"] == 200


@mock_aws
def test_offer_with_blocked_words_is_refused():
    tables = make_tables()
    item_id = post_request()
    res = load("provider-offer-lifestyle").lambda_handler(
        post({**offer_payload(item_id), "offerDescription": "I offer nuru massage"}), None)
    assert res["statusCode"] == 400
    assert tables["lifestyle-offer"].scan()["Items"] == []


# ---- pictures ------------------------------------------------------------------------

EXPLICIT = [{"Name": "Explicit", "ParentName": "", "Confidence": 98.0, "TaxonomyLevel": 1}]
PARTIAL = [{"Name": "Partially Exposed Female Breast", "ParentName": "Non-Explicit Nudity",
            "Confidence": 91.0, "TaxonomyLevel": 3}]
SWIMWEAR = [{"Name": "Swimwear or Underwear", "ParentName": "", "Confidence": 95.0, "TaxonomyLevel": 1},
            {"Name": "Female Swimwear or Underwear", "ParentName": "Swimwear or Underwear",
             "Confidence": 95.0, "TaxonomyLevel": 2}]
KISS = [{"Name": "Non-Explicit Nudity of Intimate parts and Kissing", "ParentName": "",
         "Confidence": 90.0, "TaxonomyLevel": 1},
        {"Name": "Kissing on the Lips", "ParentName": "Non-Explicit Nudity of Intimate parts and Kissing",
         "Confidence": 90.0, "TaxonomyLevel": 2}]
OLD_MODEL = [{"Name": "Graphic Female Nudity", "ParentName": "Explicit Nudity", "Confidence": 99.0}]


@mock_aws
@pytest.mark.parametrize("labels", [EXPLICIT, PARTIAL, OLD_MODEL])
def test_listing_picture_with_nudity_is_rejected(rekognition, labels, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "enforce")
    tables, s3, item_id = listing_setup()
    rekognition.labels = labels
    res = load("eabu-listing-picture").lambda_handler(upload(item_id), None)
    assert res["statusCode"] == 400 and body(res)["code"] == "IMAGE_REJECTED"
    assert keys(s3) == [], "the picture is never stored"
    assert "picture1" not in tables["eat-provider"].get_item(Key={"zipcode": ZIP, "ID": item_id})["Item"]


@mock_aws
@pytest.mark.parametrize("labels", [[], SWIMWEAR, KISS])
def test_listing_picture_allowed(rekognition, labels, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "enforce")
    _, s3, item_id = listing_setup()
    rekognition.labels = labels
    assert load("eabu-listing-picture").lambda_handler(upload(item_id), None)["statusCode"] == 200
    assert rekognition.calls == 1 and len(keys(s3)) == 1


@mock_aws
def test_picture_is_refused_when_it_cant_be_checked(rekognition, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "enforce")
    _, s3, item_id = listing_setup()
    rekognition.error = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                                    "DetectModerationLabels")
    res = load("eabu-listing-picture").lambda_handler(upload(item_id), None)
    assert res["statusCode"] == 503 and body(res)["code"] == "IMAGE_CHECK_FAILED"
    assert keys(s3) == []


@mock_aws
@pytest.mark.parametrize("labels,status", [(EXPLICIT, 400), (SWIMWEAR, 200)])
def test_profile_picture_moderation(rekognition, labels, status):
    s3 = boto3.client("s3", region_name="us-east-1"); s3.create_bucket(Bucket=BUCKET)
    rekognition.labels = labels
    res = load("eabu-profile-picture").lambda_handler(post({"user_id": "u1", "picture": jpeg_b64()}), None)
    assert res["statusCode"] == status
    stored = [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])]
    assert stored == ([] if status == 400 else ["u1/profile.jpg"])


# ---- User Ids ------------------------------------------------------------------------

def signup(user_name):
    return {"triggerSource": "PreSignUp_SignUp", "userName": user_name,
            "request": {"userAttributes": {}}, "response": {}}


@pytest.mark.parametrize("name", ["sexy_jane", "pornking", "Escort4u", "onlyfans.girl"])
def test_sexual_user_id_cannot_sign_up(name):
    trigger = load("eabu-pre-signup")
    with pytest.raises(trigger.UserIdBlocked):
        trigger.lambda_handler(signup(name), None)


@mock_aws
@pytest.mark.parametrize("name", ["essex_mike", "jane99", "therapist_sam"])
def test_normal_user_id_signs_up(name):
    trigger = load("eabu-pre-signup")
    assert trigger.lambda_handler(signup(name), None)["userName"] == name
