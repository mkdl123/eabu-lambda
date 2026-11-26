#!/usr/bin/env python3
"""Update prompt: tell the app (2.3+) which version is current, through GET /guest/status.

    python3 scripts/app_version.py status
    python3 scripts/app_version.py latest 2.3      # after Apple RELEASES 2.3: older apps get a banner
    python3 scripts/app_version.py min 2.1         # apps older than 2.1 must update (full screen)
    python3 scripts/app_version.py min off         # stop forcing
    python3 scripts/app_version.py latest off      # stop offering

Writes SSM /eabu/app-version = {"latest": "...", "min": "..."}. GET /guest/status caches for 30 s.
Set latest only once the version is live on the App Store, or the Update button finds nothing new.
Use min only when the backend can no longer serve older apps: it blocks them until they update.
"""
import json
import re
import sys

import boto3

PARAM = "/eabu/app-version"
VERSION_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){0,2}$")
ssm = boto3.client("ssm", region_name="us-east-1")


def read():
    try:
        return json.loads(ssm.get_parameter(Name=PARAM)["Parameter"]["Value"])
    except ssm.exceptions.ParameterNotFound:
        return {}


def key(v):
    return tuple(int(x) for x in v.split("."))


def main(argv):
    if len(argv) == 1 and argv[0] == "status":
        data = read()
        print(f"latest: {data.get('latest') or '(not set)'}   min: {data.get('min') or '(not set)'}")
        return 0
    if len(argv) != 2 or argv[0] not in ("latest", "min"):
        print(__doc__)
        return 2
    which, value = argv
    data = read()
    if value == "off":
        data.pop(which, None)
    elif not VERSION_RE.match(value):
        print(f"not a version: {value}")
        return 2
    else:
        data[which] = value
    lo, hi = data.get("min"), data.get("latest")
    if lo and hi and key(lo) > key(hi):
        print(f"min {lo} is newer than latest {hi}; set latest first")
        return 2
    if which == "min" and value != "off":
        ans = input(f"Apps older than {value} will be BLOCKED until they update. Type yes: ")
        if ans.strip() != "yes":
            print("Cancelled.")
            return 1
    ssm.put_parameter(Name=PARAM, Value=json.dumps(data), Type="String", Overwrite=True)
    print(f"saved {PARAM} = {json.dumps(data)} (live within 30 s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
