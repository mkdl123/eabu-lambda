#!/usr/bin/env python3
"""Set how long the app stays signed in (the Cognito app client's refresh token lifetime).

People stay signed in this many days after they last signed in; the app refreshes its
short-lived tokens in the background meanwhile. Existing sessions keep their old expiry until
their next sign-in.

    python3 scripts/set-sign-in-days.py            # report only
    python3 scripts/set-sign-in-days.py 365 --apply

UpdateUserPoolClient resets whatever isn't passed, so this copies every current client setting,
changes only RefreshTokenValidity, and checks afterwards that nothing else changed.
"""
import argparse
import json

import boto3

REGION = "us-east-1"
POOL_ID = "us-east-1_G0VobzUMd"
# The iPhone app's client (auth/amplifyConfig.js). The pool's other client (..._clientWeb)
# belonged to the retired web app.
APP_CLIENT_NAME = "eabuda06a1b3_app_client"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("days", nargs="?", type=int)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    cognito = boto3.client("cognito-idp", region_name=REGION)
    clients = cognito.list_user_pool_clients(UserPoolId=POOL_ID, MaxResults=60)["UserPoolClients"]
    matches = [c for c in clients if c["ClientName"] == APP_CLIENT_NAME]
    if len(matches) != 1:
        raise SystemExit(f"app client {APP_CLIENT_NAME!r} not found")
    client_id = matches[0]["ClientId"]
    before = cognito.describe_user_pool_client(UserPoolId=POOL_ID, ClientId=client_id)["UserPoolClient"]
    units = before.get("TokenValidityUnits") or {}
    print(f"signed in for: {before.get('RefreshTokenValidity')} {units.get('RefreshToken', 'days')}")
    if not args.days:
        return
    if not 1 <= args.days <= 3650:
        raise SystemExit("days must be 1-3650 (Cognito's limit is 10 years)")
    if not args.apply:
        print(f"report only; add --apply to set {args.days} days")
        return

    allowed = cognito.meta.service_model.operation_model("UpdateUserPoolClient").input_shape.members
    params = {k: v for k, v in before.items() if k in allowed}
    params["RefreshTokenValidity"] = args.days
    params["TokenValidityUnits"] = {**units, "RefreshToken": "days"}
    cognito.update_user_pool_client(**params)

    after = cognito.describe_user_pool_client(UserPoolId=POOL_ID, ClientId=client_id)["UserPoolClient"]
    print(f"signed in for now: {after.get('RefreshTokenValidity')} {after['TokenValidityUnits']['RefreshToken']}")
    skip = {"RefreshTokenValidity", "LastModifiedDate", "TokenValidityUnits"}
    norm = lambda c: json.loads(json.dumps({k: v for k, v in c.items() if k not in skip}, sort_keys=True, default=str))
    b, a = norm(before), norm(after)
    changed = [k for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
    other_units = {k: v for k, v in (after.get("TokenValidityUnits") or {}).items() if k != "RefreshToken"}
    if other_units != {k: v for k, v in units.items() if k != "RefreshToken"}:
        changed.append("TokenValidityUnits (access/id)")
    print("all other client settings unchanged" if not changed else f"WARNING: also changed: {changed}")


if __name__ == "__main__":
    main()
