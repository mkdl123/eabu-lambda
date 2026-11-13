#!/usr/bin/env python3
"""Send Cognito's emails (sign-up codes, password resets) through SES as "eabu <no-reply@eabu.net>".

Cognito's built-in email always comes from no-reply@verificationemail.com. With SES it comes from
our verified eabu.net domain. Needs SES production access first: in the SES sandbox only
verified addresses receive mail, so new users would stop getting their codes.

    python3 scripts/use-ses-for-cognito-email.py            # report only
    python3 scripts/use-ses-for-cognito-email.py --apply    # switch to SES
    python3 scripts/use-ses-for-cognito-email.py --revert --apply   # back to Cognito's built-in email

Like attach-pre-signup-trigger.py, it copies every other pool setting unchanged (UpdateUserPool
resets whatever isn't passed) and checks afterwards that nothing else changed.
"""
import argparse
import json

import boto3

REGION = "us-east-1"
POOL_ID = "us-east-1_G0VobzUMd"
DOMAIN = "eabu.net"
FROM = "eabu <no-reply@eabu.net>"
REPLY_TO = "mdas@eabu.net"
SUBJECT = "Your eabu verification code"
DROP = {("AdminCreateUserConfig", "UnusedAccountValidityDays")}


def comparable(pool, ignore):
    pool = {k: v for k, v in pool.items() if k not in ignore | {"LastModifiedDate", "EstimatedNumberOfUsers"}}
    return json.loads(json.dumps(pool, sort_keys=True, default=str))


def allow_cognito_to_send(ses, account):
    """SES identity policy letting Cognito (for this account's pools only) send as eabu.net."""
    arn = f"arn:aws:ses:{REGION}:{account}:identity/{DOMAIN}"
    policy = {"Version": "2012-10-17", "Statement": [{
        "Sid": "AllowCognitoToSend", "Effect": "Allow",
        "Principal": {"Service": "cognito-idp.amazonaws.com"},
        "Action": ["ses:SendEmail", "ses:SendRawEmail"], "Resource": arn,
        "Condition": {"StringEquals": {"aws:SourceAccount": account},
                      "ArnLike": {"aws:SourceArn": f"arn:aws:cognito-idp:{REGION}:{account}:userpool/*"}},
    }]}
    ses.create_email_identity_policy(EmailIdentity=DOMAIN, PolicyName="cognito-send", Policy=json.dumps(policy)) \
        if "cognito-send" not in ses.get_email_identity_policies(EmailIdentity=DOMAIN).get("Policies", {}) else None
    return arn


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--revert", action="store_true")
    args = p.parse_args()

    account = boto3.client("sts").get_caller_identity()["Account"]
    ses = boto3.client("sesv2", region_name=REGION)
    cognito = boto3.client("cognito-idp", region_name=REGION)
    before = cognito.describe_user_pool(UserPoolId=POOL_ID)["UserPool"]
    print("email now:", before.get("EmailConfiguration"))

    production = ses.get_account().get("ProductionAccessEnabled")
    if not args.revert and not production:
        print("SES is still in the sandbox: switching now would stop sign-up codes reaching new users.")
        return
    if not args.apply:
        print("report only; add --apply to switch" + (" back" if args.revert else f" to SES as {FROM}"))
        return

    if args.revert:
        email = {"EmailSendingAccount": "COGNITO_DEFAULT"}
    else:
        email = {"EmailSendingAccount": "DEVELOPER", "SourceArn": allow_cognito_to_send(ses, account),
                 "From": FROM, "ReplyToEmailAddress": REPLY_TO}
    allowed = cognito.meta.service_model.operation_model("UpdateUserPool").input_shape.members
    params = {k: v for k, v in before.items() if k in allowed}
    for outer, inner in DROP:
        if isinstance(params.get(outer), dict):
            params[outer] = {k: v for k, v in params[outer].items() if k != inner}
    params.update(UserPoolId=POOL_ID, EmailConfiguration=email)
    if not args.revert:
        template = dict(params.get("VerificationMessageTemplate") or {})
        template["EmailSubject"] = SUBJECT
        params["VerificationMessageTemplate"] = template
        params["EmailVerificationSubject"] = SUBJECT
    cognito.update_user_pool(**params)

    after = cognito.describe_user_pool(UserPoolId=POOL_ID)["UserPool"]
    print("email after:", after.get("EmailConfiguration"))
    ignore = {"EmailConfiguration", "VerificationMessageTemplate", "EmailVerificationSubject"}
    b, a = comparable(before, ignore), comparable(after, ignore)
    changed = [k for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
    print("all other pool settings unchanged" if not changed else f"WARNING: also changed: {changed}")


if __name__ == "__main__":
    main()
