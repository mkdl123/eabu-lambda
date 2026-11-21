"""Weekly "Discovered near you": areas, checks on Claude's finds, saving, and GET /discovered."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from test_lifestyle import make_tables
from test_security import load

REGION = "us-east-1"
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)  # a Monday
TODAY = NOW.date()


@pytest.fixture(autouse=True)
def enforce(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "enforce")
    monkeypatch.setenv("AUTH_USER_ID_CLAIM", "sub")


def find(**over):
    f = {"headline": "Breakfast tacos land in Ardmore", "place": "Taco Heart", "address_or_area": "Ardmore, PA",
         "what": "Austin-style breakfast tacos.", "when": "Mon-Sat 8-6", "why_new": "Soft opening",
         "source_url": "https://delco.today/2026/11/new-restaurants/", "source_date": "2026-11-20",
         "confidence": "high"}
    f.update(over)
    return f


class FakeClaude:
    """messages.create returns a save_finds call (optionally after a paused turn)."""

    def __init__(self, finds, pause_first=False):
        self.finds, self.pause_first, self.calls = finds, pause_first, []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(input_tokens=1000, output_tokens=100,
                                server_tool_use=SimpleNamespace(web_search_requests=3))
        if self.pause_first and len(self.calls) == 1:
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="...")], stop_reason="pause_turn", usage=usage)
        block = SimpleNamespace(type="tool_use", name="save_finds", input={"finds": self.finds, "notes": ""})
        return SimpleNamespace(content=[block], stop_reason="tool_use", usage=usage)


def make_discovered():
    boto3.resource("dynamodb", region_name=REGION).create_table(
        TableName="eabu-discovered", BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "areaZip", "KeyType": "HASH"}, {"AttributeName": "ID", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "areaZip", "AttributeType": "S"},
                              {"AttributeName": "ID", "AttributeType": "N"}])
    return boto3.resource("dynamodb", region_name=REGION).Table("eabu-discovered")


def test_areas_group_nearby_zips_busiest_first():
    fn = load("eabu-discover")
    # Ridley Park, Folsom and Media are a few miles apart; 10003 (NYC) is ~90 miles away.
    areas = fn.group_areas({"19060": 5, "19033": 2, "19063": 1, "10003": 3})
    assert [a[0] for a in areas] == ["19060", "10003"]
    assert set(areas[0][1]) == {"19060", "19033", "19063"} and areas[0][2] == 8
    assert fn.group_areas({"19060": 5, "10003": 3}, max_areas=1) == [("19060", ["19060"], 5)]


def test_checks_drop_weak_old_homepage_and_repeated_finds():
    fn = load("eabu-discover")
    kept = fn.check_finds([
        find(),
        find(place="Old Spot", source_date="2026-10-01"),                     # too old
        find(place="No Date", source_date="unknown"),
        find(place="Home Page", source_url="https://delco.today/"),           # homepage
        find(place="Plain Http", source_url="http://example.com/a"),
        find(place="Unsure", confidence="low"),
        find(place="Shown Before", headline="Same headline"),
        find(place="Second From Article", headline="Two"),
        find(place="Third From Article", headline="Three"),                   # 3rd from one URL
        find(place="Rude", what="p0rn night"),                                # content filter
    ], TODAY, avoid_keys={"shown before|same headline"})
    assert [k["place"] for k in kept] == ["Taco Heart", "Second From Article"]
    assert kept[0]["sourceSite"] == "delco.today" and kept[0]["sourceDate"] == "2026-11-20"


@mock_aws
def test_dispatch_starts_one_run_per_area(monkeypatch):
    tables = make_tables()
    make_discovered()
    for z, created in (("19060", "2026-11-20"), ("19033", "2026-11-19"), ("10003", "2026-11-18"), ("59801", "2026-08-01")):
        tables["eat-provider"].put_item(Item={"zipcode": z, "ID": 1, "createdAt": created})
    tables["eat-consumer"].put_item(Item={"zipcode": "19060", "ID": 2, "createdAt": "2026-11-21"})  # busiest
    tables["eat-provider"].put_item(Item={"zipcode": "11111", "ID": 100, "counter": 5})  # counter row
    fn = load("eabu-discover")
    started = []
    monkeypatch.setattr(fn.lambda_client, "invoke", lambda **kw: started.append(json.loads(kw["Payload"])))
    fn.dispatch(SimpleNamespace(invoked_function_arn="arn:fn"), now=NOW)
    assert sorted(p["areaZip"] for p in started) == ["10003", "19060"]   # 59801's post is too old


@mock_aws
def test_area_run_saves_this_weeks_finds_and_remembers_them():
    table = make_discovered()
    fn = load("eabu-discover")
    fake = FakeClaude([find(), find(place="Capozzoli's", headline="New family kitchen",
                                    source_url="https://cfood.org/818500/capozzoli/")], pause_first=True)
    fn._client = fake
    assert fn.run_area({"areaZip": "19060", "zips": ["19060", "19033"]}, now=NOW) == {"saved": 2}

    rows = table.scan()["Items"]
    area = next(r for r in rows if r["ID"] == 0)
    finds = sorted((r for r in rows if r["ID"] != 0), key=lambda r: r["ID"])
    assert [int(r["ID"]) for r in finds] == [2026112301, 2026112302]
    assert finds[0]["place"] == "Taco Heart" and finds[0]["weekOf"] == "2026-11-23"
    assert int(finds[0]["expiresAt"]) == int(NOW.timestamp()) + 8 * 86400
    assert area["zips"] == ["19060", "19033"] and len(area["shownKeys"]) == 2
    assert "ZIP 19060" in fake.calls[0]["messages"][0]["content"] and len(fake.calls) == 2
    assert fake.calls[0]["model"] == "claude-haiku-5-5"

    # A re-run the same week replaces the week's finds and tells Claude what was shown.
    fake2 = FakeClaude([find(place="New Place", headline="Fresh")])
    fn._client = fake2
    fn.run_area({"areaZip": "19060"}, now=NOW)
    left = [r for r in table.scan()["Items"] if r["ID"] != 0]
    assert [r["place"] for r in left] == ["New Place"]
    assert "taco heart: breakfast tacos land in ardmore" in fake2.calls[0]["messages"][0]["content"]


@mock_aws
def test_get_discovered_returns_the_callers_area():
    table = make_discovered()
    table.put_item(Item={"areaZip": "19060", "ID": 0, "zips": ["19060", "19033"]})
    future = int(NOW.timestamp()) + 10**8
    table.put_item(Item={"areaZip": "19060", "ID": 2026111601, "headline": "Last week", "expiresAt": future, "weekOf": "2026-11-16"})
    table.put_item(Item={"areaZip": "19060", "ID": 2026112302, "headline": "Second", "expiresAt": future, "weekOf": "2026-11-23"})
    table.put_item(Item={"areaZip": "19060", "ID": 2026112301, "headline": "First", "expiresAt": future, "weekOf": "2026-11-23",
                         "sourceUrl": "https://a.com/x", "place": "P"})
    table.put_item(Item={"areaZip": "19060", "ID": 2026112303, "headline": "Expired", "expiresAt": 5, "weekOf": "2026-11-23"})
    fn = load("eabu-discovered")
    fn._cache = None
    started = []
    fn.lambda_client = SimpleNamespace(invoke=lambda **kw: started.append(json.loads(kw["Payload"])))

    def get(zip_code, user="viewer"):
        event = {"httpMethod": "GET", "queryStringParameters": {"zip": zip_code}}
        if user:
            event["requestContext"] = {"authorizer": {"claims": {"sub": user}}}
        res = fn.lambda_handler(event, None)
        return res["statusCode"], json.loads(res["body"])

    status, body = get("19033")           # a member ZIP
    assert status == 200 and [i["headline"] for i in body["Items"]] == ["First", "Second"]
    assert body["areaZip"] == "19060" and body["weekOf"] == "2026-11-23" and body["Items"][0]["ID"] == "2026112301"
    assert get("19063")[1]["areaZip"] == "19060"     # Media: not a member, but within 25 miles
    assert body["pending"] is False
    assert get("abc")[0] == 400 and get("00000")[1]["areaZip"] is None   # not a real ZIP: no search
    assert get("19060", user=None)[0] == 401
    assert started == []
    viewed = table.get_item(Key={"areaZip": "19060", "ID": 0})["Item"]
    assert int(viewed["lastViewedAt"]) > 0                                  # keeps it in the Monday run

    # Missoula has no area yet: one is created and its first search starts straight away.
    status, body = get("59801")
    assert status == 200 and body == {"areaZip": "59801", "weekOf": None, "pending": True, "Items": [], "Count": 0}
    assert started == [{"areaZip": "59801", "zips": ["59801"]}]
    get("59802")                                                            # next door: same area, no 2nd search
    assert len(started) == 1 and table.get_item(Key={"areaZip": "59801", "ID": 0})["Item"]["pending"] is True


@mock_aws
def test_new_area_searches_are_capped_per_day(monkeypatch):
    table = make_discovered()
    fn = load("eabu-discovered")
    fn._cache = None
    monkeypatch.setattr(fn, "NEW_AREAS_PER_DAY", 2)
    started = []
    fn.lambda_client = SimpleNamespace(invoke=lambda **kw: started.append(json.loads(kw["Payload"])["areaZip"]))
    for z in ("59801", "10003", "94103"):                                   # three far-apart cities
        res = fn.lambda_handler({"httpMethod": "GET", "queryStringParameters": {"zip": z},
                                 "requestContext": {"authorizer": {"claims": {"sub": "u"}}}}, None)
        assert res["statusCode"] == 200
    assert started == ["59801", "10003"]
    third = table.get_item(Key={"areaZip": "94103", "ID": 0})["Item"]
    assert "pending" not in third and int(third["lastViewedAt"]) > 0       # waits for Monday
    assert json.loads(res["body"])["pending"] is False


@mock_aws
def test_monday_run_includes_viewed_areas_without_posts(monkeypatch):
    make_tables()
    table = make_discovered()
    table.put_item(Item={"areaZip": "59801", "ID": 0, "lastViewedAt": int(NOW.timestamp()) - 86400})
    table.put_item(Item={"areaZip": "94103", "ID": 0, "lastViewedAt": int(NOW.timestamp()) - 40 * 86400})  # stale
    table.put_item(Item={"areaZip": "daily#2026-11-22", "ID": 0, "n": 3})
    fn = load("eabu-discover")
    started = []
    monkeypatch.setattr(fn.lambda_client, "invoke", lambda **kw: started.append(json.loads(kw["Payload"])["areaZip"]))
    fn.dispatch(SimpleNamespace(invoked_function_arn="arn:fn"), now=NOW)
    assert started == ["59801"]


def test_area_with_current_finds_wins_over_an_empty_one():
    fn = load("eabu-discovered")
    areas = {"19060": {"zips": ["19060"]}, "19033": {"zips": ["19033", "19060"]}}
    finds = {"19033": [{"ID": 1, "expiresAt": 10**10}]}
    assert fn.area_for("19060", areas, finds, now=1) == "19033"
    assert fn.area_for("19060", areas, {}, now=1) == "19060"


def test_report_accepts_discovered_finds():
    fn = load("eabu-report")
    assert "discovered" in fn.CONTENT_TYPES


@mock_aws
def test_a_stale_cache_doesnt_hide_or_repeat_a_new_area():
    table = make_discovered()
    fn = load("eabu-discovered")
    started = []
    fn.lambda_client = SimpleNamespace(invoke=lambda **kw: started.append(json.loads(kw["Payload"])["areaZip"]))

    def get(z):
        res = fn.lambda_handler({"httpMethod": "GET", "queryStringParameters": {"zip": z},
                                 "requestContext": {"authorizer": {"claims": {"sub": "u"}}}}, None)
        return json.loads(res["body"])

    fn._load()                                   # this container cached the table while it was empty
    # Meanwhile another container created the Missoula area, and its search saved a find.
    table.put_item(Item={"areaZip": "59801", "ID": 0, "zips": ["59801"], "lastViewedAt": 1})
    table.put_item(Item={"areaZip": "59801", "ID": 2026112301, "headline": "Huckleberry pie",
                         "expiresAt": 10**10, "weekOf": "2026-11-23"})
    for z in ("59801", "59802"):                 # same ZIP, and a neighbour
        body = get(z)
        assert body["areaZip"] == "59801" and [i["headline"] for i in body["Items"]] == ["Huckleberry pie"]
    assert started == []                          # no second search
