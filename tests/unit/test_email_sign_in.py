"""Sign in with email (2.1): email + password -> User Id only when the password is right, the
same answer for an unknown email and a wrong password, password reset by email, per-email limits,
and one account per email at sign-up."""
import json

import boto3
import pytest
from moto import mock_aws

from test_rate_limit import make_rate_table
from test_security import load

PASSWORD = "Str0ng!Passw0rd"


def setup(monkeypatch):
    idp = boto3.client("cognito-idp", region_name="us-east-1")
    pool = idp.create_user_pool(PoolName="eabu")["UserPool"]["Id"]
    client = idp.create_user_pool_client(UserPoolId=pool, ClientName="server",
                                         ExplicitAuthFlows=["ALLOW_ADMIN_USER_PASSWORD_AUTH"])["UserPoolClient"]["ClientId"]
    for user, email in (("star-nite", "Star@Example.com"), ("sunny-day", "sunny@example.com")):
        idp.admin_create_user(UserPoolId=pool, Username=user, MessageAction="SUPPRESS",
                              UserAttributes=[{"Name": "email", "Value": email},
                                              {"Name": "email_verified", "Value": "true"}])
        idp.admin_set_user_password(UserPoolId=pool, Username=user, Password=PASSWORD, Permanent=True)
    make_rate_table()
    monkeypatch.setenv("USER_POOL_ID", pool)
    monkeypatch.setenv("SIGN_IN_CLIENT_ID", client)
    return idp, pool, load("eabu-email-sign-in")


def call(fn, path, body, ip="203.0.113.5"):
    res = fn.lambda_handler({"httpMethod": "POST", "resource": path, "body": json.dumps(body),
                             "requestContext": {"identity": {"sourceIp": ip}}}, None)
    return res["statusCode"], json.loads(res["body"])


@mock_aws
def test_right_password_gives_the_user_id_any_case_of_email(monkeypatch):
    _, _, fn = setup(monkeypatch)
    assert call(fn, "/guest/sign-in-name", {"email": "sunny@example.com", "password": PASSWORD}) == \
        (200, {"userId": "sunny-day"})
    status, body = call(fn, "/guest/sign-in-name", {"email": " Star@Example.com ", "password": PASSWORD})
    assert status == 200 and body["userId"] == "star-nite"


@mock_aws
def test_unknown_email_and_wrong_password_look_the_same(monkeypatch):
    _, _, fn = setup(monkeypatch)
    wrong = call(fn, "/guest/sign-in-name", {"email": "sunny@example.com", "password": "nope"})
    unknown = call(fn, "/guest/sign-in-name", {"email": "nobody@example.com", "password": PASSWORD})
    junk = call(fn, "/guest/sign-in-name", {"email": "not-an-email", "password": PASSWORD})
    assert wrong == unknown == junk == (400, {"msg": "Incorrect email or password.", "code": "BAD_CREDENTIALS"})
    assert "userId" not in json.dumps(wrong)


@mock_aws
def test_tries_are_limited_per_email(monkeypatch):
    _, _, fn = setup(monkeypatch)
    monkeypatch.setattr(fn, "SIGN_IN_TRIES_PER_HOUR", 3)
    codes = [call(fn, "/guest/sign-in-name", {"email": "sunny@example.com", "password": "x"}, ip=f"198.51.100.{i}")[0]
             for i in range(5)]
    assert codes == [400, 400, 400, 429, 429]
    # The right password is refused too while the email is over its limit.
    assert call(fn, "/guest/sign-in-name", {"email": "sunny@example.com", "password": PASSWORD})[0] == 429


@mock_aws
def test_forgot_password_answers_the_same_and_only_sends_for_real_accounts(monkeypatch):
    _, _, fn = setup(monkeypatch)
    sent = []
    monkeypatch.setattr(fn.cognito, "forgot_password", lambda **kw: sent.append(kw["Username"]) or {})
    real = call(fn, "/guest/forgot-password", {"email": "sunny@example.com"})
    fake = call(fn, "/guest/forgot-password", {"email": "nobody@example.com"})
    assert real == fake and real[0] == 200 and sent == ["sunny-day"]


@mock_aws
def test_reset_password_by_email(monkeypatch):
    _, _, fn = setup(monkeypatch)
    done = []
    monkeypatch.setattr(fn.cognito, "confirm_forgot_password", lambda **kw: done.append(kw) or {})
    status, body = call(fn, "/guest/reset-password", {"email": "sunny@example.com", "code": "123456",
                                                      "password": "N3w!Password"})
    assert status == 200 and done[0]["Username"] == "sunny-day" and done[0]["ConfirmationCode"] == "123456"
    assert call(fn, "/guest/reset-password", {"email": "nobody@example.com", "code": "1", "password": "x"})[0] == 400


@mock_aws
def test_other_methods_and_paths_are_not_found(monkeypatch):
    _, _, fn = setup(monkeypatch)
    assert fn.lambda_handler({"httpMethod": "GET", "resource": "/guest/sign-in-name"}, None)["statusCode"] == 404
    assert fn.lambda_handler({"httpMethod": "POST", "resource": "/guest/other", "body": "{}"}, None)["statusCode"] == 404


@mock_aws
def test_sign_up_refuses_an_email_another_account_uses(monkeypatch):
    idp, pool, _ = setup(monkeypatch)
    pre = load("eabu-pre-signup")
    monkeypatch.setattr(pre, "cognito", idp)
    monkeypatch.setattr(pre, "_check_email", lambda email: None)   # the deleted-email table isn't under test

    def event(user, email):
        return {"triggerSource": "PreSignUp_SignUp", "userPoolId": pool, "userName": user,
                "request": {"userAttributes": {"email": email}}}

    with pytest.raises(pre.EmailInUse):
        # Real Cognito matches the email filter in any letter case (checked 2026-11-24); moto doesn't.
        pre.lambda_handler(event("new-person", "Star@Example.com"), None)
    assert pre.lambda_handler(event("new-person", "fresh@example.com"), None)["userName"] == "new-person"
