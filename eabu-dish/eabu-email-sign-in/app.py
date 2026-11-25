"""Sign in with an email instead of the User Id (2.1), on the existing username-only user pool.

The pool can't take email as a sign-in alias (that's fixed when a pool is created), so the app
swaps the email for the User Id here and then signs in as usual with the User Id:

POST /guest/sign-in-name      {"email", "password"} -> {"userId"}
     Finds the account with that email and checks the password with Cognito
     (AdminInitiateAuth on a server-only app client; the tokens are thrown away). The User Id
     comes back only when the password is right; an unknown email and a wrong password get the
     same 400 BAD_CREDENTIALS, so nobody can test which emails have accounts.
POST /guest/forgot-password   {"email"} -> always 200 with the same message
     Sends the account's reset code to its email (Cognito ForgotPassword) if there is one.
POST /guest/reset-password    {"email", "code", "password"}
     Sets the new password (ConfirmForgotPassword) for the account with that email.

All three are public (PUBLIC_ROUTES) and limited per IP address and per email
(SIGN_IN_TRIES_PER_HOUR); Cognito's own lockout still applies. Emails and passwords are never
logged or stored. New sign-ups can't reuse an email (eabu-pre-signup), so an email names at most
one account; if old data has two, the oldest confirmed one wins.
"""
import hashlib
import logging
import os
import re

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from my_utils import helpers

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REGION = os.environ.get("REGION_NAME", "us-east-1")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
CLIENT_ID = os.environ.get("SIGN_IN_CLIENT_ID", "")
SIGN_IN_TRIES_PER_HOUR = int(os.environ.get("SIGN_IN_TRIES_PER_HOUR", "20"))

EMAIL_RE = re.compile(r"^[^@\s\"]+@[^@\s\"]+\.[^@\s\"]+$")
MAX_EMAIL, MAX_PASSWORD, MAX_CODE = 254, 256, 12

BAD_CREDENTIALS = {"msg": "Incorrect email or password.", "code": "BAD_CREDENTIALS"}
TOO_MANY = {"msg": "Too many tries. Please wait a while and try again.", "code": "RATE_LIMITED"}
CODE_SENT = {"msg": "If an account uses this email, we've sent a code to it. It can take a minute; "
                    "check your spam folder too."}
BAD_CODE = {"msg": "That code is wrong or has expired. Request a new one.", "code": "BAD_CODE"}

cognito = boto3.client("cognito-idp", region_name=REGION)


def _email(value):
    e = str(value or "").strip()
    return e if 3 <= len(e) <= MAX_EMAIL and EMAIL_RE.match(e) else None


def _email_key(email):
    return "signin-email#" + hashlib.sha256(email.lower().encode()).hexdigest()[:32]


def _limited(event, email):
    """True once this IP address or this email has used up its tries for the hour."""
    ip = helpers.source_ip(event)
    over = helpers.over_read_limit(_email_key(email), limit=SIGN_IN_TRIES_PER_HOUR)
    if ip:
        over = helpers.over_read_limit(f"signin-ip#{ip}", limit=SIGN_IN_TRIES_PER_HOUR * 3) or over
    return over


def user_for_email(email):
    """The User Id of the account using this email (exact, then lower case), or None."""
    found = []
    for candidate in dict.fromkeys([email, email.lower()]):
        safe = candidate.replace("\\", "").replace('"', "")
        res = cognito.list_users(UserPoolId=USER_POOL_ID, Filter=f'email = "{safe}"', Limit=10)
        found += res.get("Users", [])
    if not found:
        return None
    # Prefer a confirmed, enabled account; then the oldest.
    found.sort(key=lambda u: (u.get("UserStatus") != "CONFIRMED", not u.get("Enabled", True),
                              u.get("UserCreateDate")))
    return found[0]["Username"]


def sign_in_name(event, body):
    email = _email(body.get("email"))
    password = body.get("password")
    if not email or not isinstance(password, str) or not password or len(password) > MAX_PASSWORD:
        return helpers._response(400, BAD_CREDENTIALS)
    if _limited(event, email):
        return helpers._response(429, TOO_MANY)
    user_id = user_for_email(email)
    if not user_id:
        return helpers._response(400, BAD_CREDENTIALS)
    try:
        cognito.admin_initiate_auth(UserPoolId=USER_POOL_ID, ClientId=CLIENT_ID,
                                    AuthFlow="ADMIN_USER_PASSWORD_AUTH",
                                    AuthParameters={"USERNAME": user_id, "PASSWORD": password})
    except cognito.exceptions.UserNotConfirmedException:
        # Right password, sign-up not confirmed yet: the app's normal sign-in takes them there.
        return helpers._response(200, {"userId": user_id, "confirmed": False})
    except cognito.exceptions.PasswordResetRequiredException:
        return helpers._response(200, {"userId": user_id, "resetRequired": True})
    except (cognito.exceptions.NotAuthorizedException, cognito.exceptions.UserNotFoundException):
        return helpers._response(400, BAD_CREDENTIALS)
    except cognito.exceptions.TooManyRequestsException:
        return helpers._response(429, TOO_MANY)
    return helpers._response(200, {"userId": user_id})


def forgot_password(event, body):
    email = _email(body.get("email"))
    if not email:
        return helpers._response(400, {"msg": "Enter the email on your account."})
    if _limited(event, email):
        return helpers._response(429, TOO_MANY)
    user_id = user_for_email(email)
    if user_id:
        try:
            cognito.forgot_password(ClientId=CLIENT_ID, Username=user_id)
        except (cognito.exceptions.LimitExceededException, cognito.exceptions.TooManyRequestsException):
            return helpers._response(429, TOO_MANY)
        except (cognito.exceptions.UserNotFoundException, cognito.exceptions.InvalidParameterException,
                cognito.exceptions.NotAuthorizedException):
            pass  # e.g. no verified email on the account: same answer, nothing sent
    return helpers._response(200, CODE_SENT)


def reset_password(event, body):
    email = _email(body.get("email"))
    code = str(body.get("code") or "").strip()
    password = body.get("password")
    if not email or not code or len(code) > MAX_CODE or not isinstance(password, str) or not password \
            or len(password) > MAX_PASSWORD:
        return helpers._response(400, {"msg": "Enter your email, the code and a new password."})
    if _limited(event, email):
        return helpers._response(429, TOO_MANY)
    user_id = user_for_email(email)
    if not user_id:
        return helpers._response(400, BAD_CODE)
    try:
        cognito.confirm_forgot_password(ClientId=CLIENT_ID, Username=user_id, ConfirmationCode=code,
                                        Password=password)
    except cognito.exceptions.InvalidPasswordException as e:
        # Cognito's password rules ("must have uppercase characters", ...): safe to show.
        msg = str(e.response.get("Error", {}).get("Message") or "That password isn't allowed.")
        return helpers._response(400, {"msg": msg.split(": ", 1)[-1], "code": "INVALID_PASSWORD"})
    except (cognito.exceptions.CodeMismatchException, cognito.exceptions.ExpiredCodeException,
            cognito.exceptions.UserNotFoundException, cognito.exceptions.NotAuthorizedException):
        return helpers._response(400, BAD_CODE)
    except (cognito.exceptions.LimitExceededException, cognito.exceptions.TooManyRequestsException):
        return helpers._response(429, TOO_MANY)
    return helpers._response(200, {"msg": "Password updated. You can now sign in.", "userId": user_id})


ROUTES = {"/guest/sign-in-name": sign_in_name, "/guest/forgot-password": forgot_password,
          "/guest/reset-password": reset_password}


@helpers.cors
def lambda_handler(event, context):
    route = ROUTES.get(event.get("resource") or "")
    if (event.get("httpMethod") or "").upper() != "POST" or not route:
        return helpers._response(404, {"msg": "Not found"})
    try:
        body = helpers._parse_body(event)
    except ValueError:
        return helpers._response(400, {"msg": "Invalid request"})
    if not isinstance(body, dict):
        return helpers._response(400, {"msg": "Invalid request"})
    try:
        return route(event, body)
    except (BotoCoreError, ClientError):
        logger.exception("Email sign-in step failed")
        return helpers._response(500, {"msg": "Something went wrong. Please try again."})
