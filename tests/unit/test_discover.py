"""Weekly "Discovered near you": areas, checks on Claude's finds, saving, and GET /discovered."""
import json
from datetime import datetime, timezone
from decimal import Decimal
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


def place(**over):
    p = {"topic": "eat", "name": "Taco Heart", "place_type": "Taqueria", "address_or_area": "12 Lancaster Ave, Ardmore, PA",
         "what": "Austin-style breakfast tacos people drive across town for.", "rating": 4.6, "rating_count": 812,
         "rating_site": "Yelp", "source_url": "https://www.yelp.com/biz/taco-heart-ardmore", "confidence": "high"}
    p.update(over)
    return p


class FakeClaude:
    """messages.create returns a save_places call (optionally after a paused turn)."""

    def __init__(self, places, pause_first=False):
        self.places, self.pause_first, self.calls = places, pause_first, []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(input_tokens=1000, output_tokens=100,
                                server_tool_use=SimpleNamespace(web_search_requests=3))
        if self.pause_first and len(self.calls) == 1:
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="...")], stop_reason="pause_turn", usage=usage)
        block = SimpleNamespace(type="tool_use", name="save_places", input={"places": self.places, "notes": ""})
        return SimpleNamespace(content=[block], stop_reason="tool_use", usage=usage)


def tm_event(name="Jazz Night", venue="The Keswick Theatre", day="2026-11-27", time="20:00", **over):
    e = {"name": name, "url": f"https://www.ticketmaster.com/{name.lower().replace(' ', '-')}/event/1",
         "dates": {"start": {"localDate": day, "localTime": time}, "status": {"code": "onsale"}},
         "classifications": [{"segment": {"name": "Music"}, "genre": {"name": "Jazz"}}],
         "_embedded": {"venues": [{"name": venue, "city": {"name": "Glenside"}, "state": {"stateCode": "PA"},
                                   "address": {"line1": "291 N Keswick Ave"}}]}}
    e.update(over)
    return e


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


def test_checks_keep_only_well_rated_new_places_within_each_topics_cap():
    fn = load("eabu-discover")
    topics = fn.topics_for_week(TODAY)
    assert [t for t, _ in topics] in (["fitness", "yoga"], ["beauty", "pets"])
    other = "pets" if topics[0][0] == "fitness" else "yoga"
    t1 = topics[0][0]
    kept = fn.check_places([
        place(),
        place(name="Low Stars", rating=3.9),
        place(name="Few Reviews", rating_count=12),                            # eat needs 50
        place(name="Home Page", source_url="https://www.yelp.com/"),
        place(name="Plain Http", source_url="http://example.com/biz/a"),
        place(name="Unsure", confidence="low"),
        place(name="Shown Lately"),
        place(name="Taco  Heart!"),                                            # same place twice
        place(name="Rude", what="p0rn night"),                                 # content filter
        place(name="Off Topic", topic=other),                                  # not this week's topic
        place(name="Small Studio", topic=t1, rating=4.8, rating_count=20,
              source_url="https://www.yelp.com/biz/small-studio"),            # lifestyle needs 15
        place(name="Second", topic=t1, source_url="https://www.yelp.com/biz/second"),
        place(name="Third", topic=t1, source_url="https://www.yelp.com/biz/third"),   # topic cap is 2
        place(name="Cafe B", source_url="https://www.yelp.com/biz/b"),
        place(name="Cafe C", source_url="https://www.yelp.com/biz/c"),
        place(name="Cafe D", source_url="https://www.yelp.com/biz/d"),
        place(name="Cafe E", source_url="https://www.yelp.com/biz/e"),          # eat cap is 4
    ], topics, recent={"shown lately"})
    assert [k["name"] for k in kept] == ["Taco Heart", "Cafe B", "Cafe C", "Cafe D", "Small Studio", "Second"]
    assert kept[0]["category"] == "eat" and kept[4]["category"] == "lifestyle"
    assert kept[0]["sourceSite"] == "yelp.com" and kept[0]["rating"] == 4.6


def test_lifestyle_topics_alternate_weekly():
    fn = load("eabu-discover")
    from datetime import timedelta
    a, b = fn.topics_for_week(TODAY), fn.topics_for_week(TODAY + timedelta(days=7))
    assert {t for t, _ in a} | {t for t, _ in b} == {"fitness", "yoga", "beauty", "pets"}
    assert fn.topics_for_week(TODAY + timedelta(days=14)) == a


def test_place_card_keeps_the_fields_older_builds_show():
    fn = load("eabu-discover")
    [p] = fn.check_places([place()], fn.topics_for_week(TODAY))
    row = fn.place_row(p, TODAY)
    assert row["headline"] == "⭐ 4.6 · Taqueria" and row["place"] == "Taco Heart"
    assert row["whyNew"] == "Rated 4.6 on Yelp (812 reviews)" and row["sourceDate"] == "2026-11-23"
    assert row["mapsUrl"] == ("https://www.google.com/maps/search/?api=1&query="
                              "Taco%20Heart%2C%2012%20Lancaster%20Ave%2C%20Ardmore%2C%20PA")
    assert row["kind"] == "place" and row["category"] == "eat" and str(row["rating"]) == "4.6"


def test_events_one_per_show_and_venue_soonest_first():
    fn = load("eabu-discover")
    cards = fn.pick_events([
        tm_event(),
        tm_event(day="2026-11-28"),                                            # same show again
        tm_event(name="Other Act"),                                            # same venue
        tm_event(name="Cancelled Show", venue="Hall A", dates={"start": {"localDate": "2026-11-27"},
                                                               "status": {"code": "cancelled"}}),
        tm_event(name="No Link", venue="Hall B", url="http://x"),
        tm_event(name="Nutcracker", venue="Academy of Music", time="14:00",
                 classifications=[{"segment": {"name": "Arts & Theatre"}, "genre": {"name": "Ballet"}}]),
    ], today=TODAY)
    assert [c["headline"] for c in cards] == ["Jazz Night", "Nutcracker"]
    assert cards[0]["when"] == "Fri, Nov 27 · 8:00 PM" and cards[0]["what"] == "Music · Jazz"
    assert cards[0]["eventDate"] == "2026-11-27" and cards[0]["ticketUrl"].startswith("https://www.ticketmaster.com/")
    assert cards[0]["addressOrArea"] == "291 N Keswick Ave, Glenside, PA" and cards[1]["when"].endswith("2:00 PM")
    assert all(c["kind"] == "event" and c["category"] == "lifestyle" for c in cards)


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
def test_area_run_saves_places_and_events_and_rotates_places(monkeypatch):
    table = make_discovered()
    fn = load("eabu-discover")
    topic = fn.topics_for_week(TODAY)[0][0]
    fake = FakeClaude([place(), place(name="Iron Barn", topic=topic, place_type="Gym",
                                      source_url="https://www.yelp.com/biz/iron-barn")], pause_first=True)
    fn._client = fake
    monkeypatch.setattr(fn, "fetch_events", lambda z, now: [tm_event()])
    assert fn.run_area({"areaZip": "19060", "zips": ["19060", "19033"]}, now=NOW) == {"saved": 3}

    rows = table.scan()["Items"]
    area = next(r for r in rows if r["ID"] == 0)
    cards = sorted((r for r in rows if r["ID"] != 0), key=lambda r: r["ID"])
    assert [int(r["ID"]) for r in cards] == [2026112301, 2026112302, 2026112303]
    assert [r["place"] for r in cards] == ["Taco Heart", "Iron Barn", "The Keswick Theatre"]
    assert [r["category"] for r in cards] == ["eat", "lifestyle", "lifestyle"] and cards[2]["kind"] == "event"
    assert int(cards[0]["expiresAt"]) == int(NOW.timestamp()) + 8 * 86400 and cards[0]["weekOf"] == "2026-11-23"
    assert area["zips"] == ["19060", "19033"] and set(area["shownPlaces"]) == {"taco heart", "iron barn"}
    ask = fake.calls[0]["messages"][0]["content"]
    assert "ZIP 19060" in ask and "- eat: up to 4" in ask and f"- {topic}: up to 2" in ask and len(fake.calls) == 2
    assert fake.calls[0]["model"] == "claude-haiku-5-5"

    # A re-run the same week replaces the week's cards (and may show this week's places again).
    fn._client = FakeClaude([place()])
    monkeypatch.setattr(fn, "fetch_events", lambda z, now: [])
    fn.run_area({"areaZip": "19060"}, now=NOW)
    assert [r["place"] for r in table.scan()["Items"] if r["ID"] != 0] == ["Taco Heart"]

    # Next week Claude is told what was shown, and a repeat is dropped anyway.
    from datetime import timedelta
    fake3 = FakeClaude([place(), place(name="New Diner", source_url="https://www.yelp.com/biz/new-diner")])
    fn._client = fake3
    fn.run_area({"areaZip": "19060"}, now=NOW + timedelta(days=7))
    assert "taco heart" in fake3.calls[0]["messages"][0]["content"]
    week2 = [r["place"] for r in table.scan()["Items"] if int(r["ID"]) // 100 == 20261130]
    assert week2 == ["New Diner"]

    # Five weeks later Taco Heart may come back.
    fn._client = FakeClaude([place()])
    fn.run_area({"areaZip": "19060"}, now=NOW + timedelta(days=35))
    assert [r["place"] for r in table.scan()["Items"] if int(r["ID"]) // 100 == 20261228] == ["Taco Heart"]


def test_ticketmaster_is_skipped_without_a_key(monkeypatch):
    fn = load("eabu-discover")
    monkeypatch.setattr(fn, "_tm_key", "")
    assert fn.fetch_events("19060", NOW) == []


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
    table.put_item(Item={"areaZip": "19060", "ID": 2026112304, "headline": "Gym", "expiresAt": future, "weekOf": "2026-11-23",
                         "category": "lifestyle", "kind": "place", "rating": Decimal("4.7"), "ratingCount": 120})
    table.put_item(Item={"areaZip": "19060", "ID": 2026112305, "headline": "Concert", "expiresAt": future, "weekOf": "2026-11-23",
                         "category": "lifestyle", "kind": "event", "eventDate": "2099-01-01"})
    table.put_item(Item={"areaZip": "19060", "ID": 2026112306, "headline": "Past show", "expiresAt": future, "weekOf": "2026-11-23",
                         "category": "lifestyle", "kind": "event", "eventDate": "2020-01-01"})
    table.put_item(Item={"areaZip": "19060", "ID": 2026112307, "headline": "Diner", "expiresAt": future, "weekOf": "2026-11-23",
                         "category": "eat", "kind": "place"})
    fn = load("eabu-discovered")
    fn._cache = None
    started = []
    fn.lambda_client = SimpleNamespace(invoke=lambda **kw: started.append(json.loads(kw["Payload"])))

    def get(zip_code, user="viewer", v=None):
        event = {"httpMethod": "GET", "queryStringParameters": {"zip": zip_code, **({"v": v} if v else {})}}
        if user:
            event["requestContext"] = {"authorizer": {"claims": {"sub": user}}}
        res = fn.lambda_handler(event, None)
        return res["statusCode"], json.loads(res["body"])

    status, body = get("19033")           # a member ZIP; app 1.7 gets food only
    assert status == 200 and [i["headline"] for i in body["Items"]] == ["First", "Second", "Diner"]
    v2 = get("19033", v="2")[1]["Items"]   # 1.8: places + upcoming events, never a past one
    assert [i["headline"] for i in v2] == ["First", "Second", "Gym", "Concert", "Diner"]
    assert v2[2]["rating"] == "4.7" and v2[2]["ratingCount"] == "120" and v2[0]["category"] == ""
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
