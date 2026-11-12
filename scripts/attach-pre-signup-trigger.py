#!/usr/bin/env python3
"""Attach (or check) the eabu-pre-signup Lambda as the user pool's pre sign-up trigger.

The user pool lives outside the SAM stack. Cognito's UpdateUserPool resets every setting
that isn't passed, so this copies the pool's current settings, changes only
LambdaConfig.PreSignUp, and afterwards checks nothing else changed.

    python3 scripts/attach-pre-signup-trigger.py            # report only
    python3 scripts/attach-pre-signup-trigger.py --apply    # attach it
    python3 scripts/attach-pre-signup-trigger.py --detach --apply   # remove it again

Run after `sam deploy` (which creates the function and Cognito's permission to call it).
"""
import argparse
import json

import boto3

REGION = "us-east-1"
POOL_ID = "us-east-1_G0VobzUMd"
STACK = "eabulambda"
LOGICAL_ID = "EabuPreSignupFunction"
# Settings DescribeUserPool returns that UpdateUserPool rejects or ignores.
DROP = {("AdminCreateUserConfig", "UnusedAccountValidityDays")}


def comparable(pool):
    pool = dict(pool)
    for k in ("LastModifiedDate", "LambdaConfig", "EstimatedNumberOfUsers"):
        pool.pop(k, None)
    return json.dumps(pool, sort_keys=True, default=str)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--detach", action="store_true")
    args = p.parse_args()

    cognito = boto3.client("cognito-idp", region_name=REGION)
    arn = boto3.client("lambda", region_name=REGION).get_function(
        FunctionName=boto3.client("cloudformation", region_name=REGION).describe_stack_resource(
            StackName=STACK, LogicalResourceId=LOGICAL_ID)["StackResourceDetail"]["PhysicalResourceId"]
    )["Configuration"]["FunctionArn"]

    before = cognito.describe_user_pool(UserPoolId=POOL_ID)["UserPool"]
    lambda_config = dict(before.get("LambdaConfig") or {})
    current = lambda_config.get("PreSignUp")
    print(f"PreSignUp now: {current or 'none'}")
    wanted = None if args.detach else arn
    if current == wanted:
        print("nothing to change")
        return
    print(f"PreSignUp wanted: {wanted or 'none'}")
    if not args.apply:
        print("report only; add --apply to change it")
        return

    if wanted:
        lambda_config["PreSignUp"] = wanted
    else:
        lambda_config.pop("PreSignUp", None)
    allowed = cognito.meta.service_model.operation_model("UpdateUserPool").input_shape.members
    params = {k: v for k, v in before.items() if k in allowed}
    for outer, inner in DROP:
        if isinstance(params.get(outer), dict):
            params[outer] = {k: v for k, v in params[outer].items() if k != inner}
    params.update(UserPoolId=POOL_ID, LambdaConfig=lambda_config)
    cognito.update_user_pool(**params)

    after = cognito.describe_user_pool(UserPoolId=POOL_ID)["UserPool"]
    print(f"PreSignUp after: {(after.get('LambdaConfig') or {}).get('PreSignUp') or 'none'}")
    if comparable(after) != comparable(before):
        print("WARNING: other pool settings changed:")
        b, a = json.loads(comparable(before)), json.loads(comparable(after))
        for k in sorted(set(a) | set(b)):
            if a.get(k) != b.get(k):
                print(f"  {k}: {b.get(k)!r} -> {a.get(k)!r}")
    else:
        print("all other pool settings unchanged")


if __name__ == "__main__":
    main()
