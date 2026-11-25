"""Kill switch: GET /guest/status tells the app whether eabu is paused (SSM /eabu/app-status)."""
import json

import boto3
import pytest
from moto import mock_aws

from test_security import load


def call(fn):
    fn._cache.update(at=0.0, body=None)
    res = fn.lambda_handler({"httpMethod": "GET", "resource": "/guest/status"}, None)
    assert res["statusCode"] == 200 and res["headers"]["Access-Control-Allow-Origin"] == "*"
    return json.loads(res["body"])


def put(value):
    boto3.client("ssm", region_name="us-east-1").put_parameter(
        Name="/eabu/app-status", Value=value, Type="String", Overwrite=True)


@mock_aws
def test_not_paused_when_parameter_missing():
    assert call(load("eabu-app-status")) == {"paused": False}


@mock_aws
def test_paused_with_message():
    fn = load("eabu-app-status")
    put(json.dumps({"paused": True, "message": "Back on Monday."}))
    assert call(fn) == {"paused": True, "message": "Back on Monday."}


@mock_aws
def test_paused_without_message_uses_default_and_long_message_is_cut():
    fn = load("eabu-app-status")
    put(json.dumps({"paused": True}))
    assert call(fn) == {"paused": True, "message": fn.DEFAULT_MESSAGE}
    put(json.dumps({"paused": True, "message": "x" * 2000}))
    assert len(call(fn)["message"]) == fn.MESSAGE_MAX


@pytest.mark.parametrize("value", ['{"paused": false, "message": "hi"}', "not json", '["paused"]', '{"paused": "yes"}'])
@mock_aws
def test_anything_else_is_not_paused(value):
    fn = load("eabu-app-status")
    put(value)
    assert call(fn) == {"paused": False}


@mock_aws
def test_answer_is_cached_for_a_short_while():
    fn = load("eabu-app-status")
    assert call(fn) == {"paused": False}
    put(json.dumps({"paused": True}))
    res = fn.lambda_handler({"httpMethod": "GET"}, None)  # within CACHE_SECONDS: old answer
    assert json.loads(res["body"]) == {"paused": False}
