"""App status for the maintenance message (kill switch).

GET /guest/status   (no sign-in)
    {"paused": false} normally. While scripts/kill-switch.sh has paused eabu:
    {"paused": true, "message": "..."} and the app shows the message instead of its screens.

This is the one function the kill switch leaves running, and /guest/status is the one route
it leaves open (with its own small throttle), so the app can still explain why nothing works.
The state lives in the SSM parameter /eabu/app-status (JSON, written only by the kill switch).
A missing parameter or any error answers "not paused": this route must never take the app down.
"""
import json
import logging
import os
import time

import boto3

from my_utils import helpers

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REGION = os.environ.get("REGION_NAME", "us-east-1")
STATUS_PARAM = os.environ.get("APP_STATUS_PARAM", "/eabu/app-status")
CACHE_SECONDS = 30
DEFAULT_MESSAGE = "eabu is paused for maintenance. Please check back soon."
MESSAGE_MAX = 500

ssm = boto3.client("ssm", region_name=REGION)
_cache = {"at": 0.0, "body": None}


def _read_status():
    try:
        raw = ssm.get_parameter(Name=STATUS_PARAM)["Parameter"]["Value"]
        data = json.loads(raw)
    except ssm.exceptions.ParameterNotFound:
        return {"paused": False}
    except Exception:  # never fail the app because of the status check
        logger.warning("app status unreadable, answering not paused", exc_info=True)
        return {"paused": False}
    if not isinstance(data, dict) or data.get("paused") is not True:
        return {"paused": False}
    message = str(data.get("message") or DEFAULT_MESSAGE).strip()[:MESSAGE_MAX] or DEFAULT_MESSAGE
    return {"paused": True, "message": message}


def lambda_handler(event, context):
    now = time.time()
    if _cache["body"] is None or now - _cache["at"] > CACHE_SECONDS:
        _cache["body"], _cache["at"] = _read_status(), now
    res = helpers._response(200, _cache["body"])
    res["headers"]["Cache-Control"] = "no-store"
    return res
