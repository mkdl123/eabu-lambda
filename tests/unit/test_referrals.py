"""Invite friends: the new user names an inviter at sign-up; both get extra posts on the
new user's first post or request; 3 rewarded invites earn the Community Builder badge."""
import json

import boto3
import pytest
from moto import mock_aws

from test_lifestyle import make_tables, LOCATION, ZIP
from test_security import load, post
from my_utils import helpers

REGION = "us-east-1"


@pytest.fixture(autouse=True)
def enforce(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "enforce")
    monkeypatch.setenv("AUTH_USER_ID_CLAIM", "sub")


def as_user(event, user):
    return {**event, "requestContext": {"authorizer": {"claims": {"sub": user}}}}


def setup():
    tables = make_tables()
    cognito = boto3.client("cognito-idp", region_name=REGION)
    pool = cognito.create_user_pool(PoolName="eabu")["UserPool"]["Id"]
    for user in ("alice", "disabled"):
        cognito.admin_create_user(UserPoolId=pool, Username=user)
    cognito.admin_disable_user(UserPoolId=pool, Username="disabled")
    return tables, pool


def sign_up(pool, user, invited_by=None, email=None):
    event = {"triggerSource": "PreSignUp_SignUp", "userPoolId": pool, "userName": user,
             "request": {"userAttributes": {"email": email or f"{user}@example.com"},
                         "clientMetadata": {"invitedBy": invited_by} if invited_by is not None else {}},
             "response": {}}
    return load("eabu-pre-signup").lambda_handler(event, None)


def first_post(user, kind="consumer", category="eat"):
    payload = {"zipcode": ZIP, "title": "T", "description": "D", "userId": user, **LOCATION}
    if kind == "provider":
        payload["phone"] = "6105550100"
    res = load(f"{kind}-post-{category}").lambda_handler(as_user(post(payload), user), None)
    assert res["statusCode"] == 200
    return json.loads(res["body"])


def balance(user):
    return helpers.post_credit_balance(user)


@mock_aws
def test_invited_user_and_inviter_both_get_five_posts_once():
    tables, pool = setup()
    sign_up(pool, "bob", invited_by=" alice ")  # the real pool also ignores case
    ref = tables["post-credits"].get_item(Key={"userId": "ref#bob"})["Item"]
    assert ref["invitedBy"] == "alice" and "bob" not in ref["emailHash"]

    assert first_post("bob")["referralPosts"] == 5, "a buyer request counts"
    assert balance("bob") == 5 and balance("alice") == 5
    assert "referralPosts" not in first_post("bob", "provider"), "only the first post"
    assert balance("alice") == 5
    assert "referralPosts" not in first_post("alice", "provider"), "people nobody invited"


@mock_aws
def test_bad_invited_by_is_refused_with_a_message():
    tables, pool = setup()
    for name, msg in (("zed", "couldn't find anyone"), ("disabled", "couldn't find anyone"), ("BOB", "your own User Id")):
        with pytest.raises(Exception) as refused:
            sign_up(pool, "bob", invited_by=name)
        assert msg in str(refused.value)
    assert sign_up(pool, "bob", invited_by="")["userName"] == "bob", "blank is fine"
    assert "Item" not in tables["post-credits"].get_item(Key={"userId": "ref#bob"})


@mock_aws
def test_one_reward_per_email_and_monthly_cap(monkeypatch):
    tables, pool = setup()
    sign_up(pool, "bob", invited_by="alice", email="Bob@example.com")
    first_post("bob")
    # Bob deletes his account and signs up again as bob2 with the same email: no second reward.
    sign_up(pool, "bob2", invited_by="alice", email="bob@example.com")
    assert "referralPosts" not in first_post("bob2")
    assert balance("bob2") == 0 and balance("alice") == 5

    monkeypatch.setattr(helpers, "REFERRAL_MONTHLY_CAP", 2)
    sign_up(pool, "carol", invited_by="alice")
    assert first_post("carol")["referralPosts"] == 5
    sign_up(pool, "dan", invited_by="alice")
    assert first_post("dan")["referralPosts"] == 5, "the new user is still rewarded"
    assert balance("alice") == 10, "alice hit her cap of 2 this month"
    assert tables["post-credits"].get_item(Key={"userId": "ref#dan"})["Item"]["inviterRewarded"] is False


@mock_aws
def test_community_builder_badge_after_three_invites():
    tables, pool = setup()
    boto3.client("s3", region_name=REGION).create_bucket(Bucket="eabu-identity-pictures")
    badge = lambda user: json.loads(load("eabu-user").lambda_handler(
        as_user({"pathParameters": {"pUserId": user}}, "viewer"), None)["body"])["communityBuilder"]
    stats = lambda: json.loads(load("eabu-post-credits").lambda_handler(
        as_user({"httpMethod": "GET", "pathParameters": {"userId": "alice"}}, "alice"), None)["body"])["invites"]

    for n, friend in enumerate(("b1", "b2", "b3"), start=1):
        assert not badge("alice")
        sign_up(pool, friend, invited_by="alice")
        first_post(friend)
        assert stats()["invitesTotal"] == n and stats()["invitesThisMonth"] == n
    assert badge("alice") and stats()["communityBuilder"] is True
    assert stats()["referralPosts"] == 5 and stats()["inviteMonthlyCap"] == 10


@mock_aws
def test_reward_failure_never_fails_the_post(monkeypatch):
    tables, pool = setup()
    sign_up(pool, "bob", invited_by="alice")
    from botocore.exceptions import ClientError
    real = helpers.post_credits_table().update_item

    def flaky(**kwargs):
        if kwargs["Key"]["userId"] == "alice":
            raise ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "UpdateItem")
        return real(**kwargs)
    monkeypatch.setattr(helpers.post_credits_table(), "update_item", flaky)
    body = first_post("bob")
    assert "ID" in body and "referralPosts" not in body
