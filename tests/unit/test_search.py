"""Search on Home (my_utils/search.py): understanding the query, matching Discovered cards and eabu
posts, per-area search counts, and the Monday hint. No web search or Claude call is made."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from my_utils import search
from test_discover import make_discovered
from test_favourites import as_user, listing, setup
from test_guest import guest
from test_lifestyle import ZIP
from test_rate_limit import make_rate_table
from test_security import load

NOW = datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)
FUTURE = 4102444800  # 2100


@pytest.fixture(autouse=True)
def enforce(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "enforce")
    monkeypatch.setenv("AUTH_USER_ID_CLAIM", "sub")


@pytest.mark.parametrize("q, keys, words", [
    ("Italian/Indian  Food near me", ["italian", "indian"], []),     # "food" dropped: something specific asked
    ("Fitness/Yoga near me?", ["fitness", "yoga"], []),
    ("what are the Dog Parks near me?", ["dogparks"], []),            # not "pets": the phrase wins
    ("food", ["food"], []),
    ("best sushi spots", ["japanese"], []),
    ("vegan tamales", ["vegan", "mexican"], []),
    ("pupusas", [], ["pupusas"]),                                     # unknown word: matched literally
    ("near me", [], []),
    ("", [], []),
    ("biryani ke paas", ["indian"], []),
    ("porn", [], []),                                                 # blocked text matches nothing
    ("Chienese Food", ["chinese"], []),                               # misspellings are corrected
    ("itallian", ["italian"], []),
    ("indain food", ["indian"], []),
    ("thia food", ["thai"], []),                                      # a swap in a short word
    ("dog parcs", ["dogparks"], []),
    ("bryani", ["indian"], []),
    ("wine bars", [], ["wine", "bars"]),                              # real words aren't "corrected"
    ("plumber", ["plumber"], []),
    ("house cleaner near me", ["cleaning"], []),                      # filler words like "house" ignored
    ("cleening service", ["cleaning"], []),
    ("kaamwali bai", ["cleaning"], []),
    ("movers near me", ["movers"], []),                               # household and transport: posts only
    ("airport ride", ["rides"], []),
    ("food truck", ["food"], []),
    ("babysiter", ["childcare"], []),
    ("piano lessons", ["tutoring"], []),
    ("chair", [], ["chair"]),                                         # a real word is never "corrected" to hair
    ("train", [], ["train"]),
    ("bench", [], ["bench"]),
])
def test_parse_query(q, keys, words):
    parsed = search.parse_query(q)
    assert parsed["keys"] == keys and parsed["words"] == words


def card(name, topic, place_type="", what="", photo="", kind="place"):
    return {"place": name, "topic": topic, "category": "eat" if topic == "eat" else "lifestyle",
            "placeType": place_type, "what": what, "photo": photo, "kind": kind}


CARDS = [
    card("Trattoria Rosa", "eat", "Italian restaurant", photo="italian"),
    card("Saffron", "eat", "Indian restaurant", "Known for its biryani.", photo="indian"),
    card("Blue Fin", "eat", "Sushi bar", photo="japanese"),
    card("Iron House", "fitness", "Gym", photo="gym"),
    card("Lotus Flow", "yoga", "Yoga studio", photo="yoga"),
    card("Thai Touch", "beauty", "Day spa", "Thai massage and facials.", photo="spa"),
    card("Bark Meadow", "dogparks", "Dog park", "Fenced, with separate small-dog area.", photo="pet"),
    card("Jazz Night", "events", kind="event"),
]


def names(q, items=CARDS):
    return [c["place"] for c in search.rank(items, search.parse_query(q), search.card_score)]


def test_cards_match_by_topic_type_and_photo():
    assert names("Italian/Indian food near me") == ["Trattoria Rosa", "Saffron"]
    assert names("food") == ["Trattoria Rosa", "Saffron", "Blue Fin"]
    assert names("sushi") == ["Blue Fin"]
    assert names("Fitness/Yoga") == ["Iron House", "Lotus Flow"]
    assert names("dog parks near me") == ["Bark Meadow"]
    assert names("thai food") == []                     # the Thai massage spa isn't food
    assert names("thai massage") == ["Thai Touch"]
    assert names("concerts this weekend") == ["Jazz Night"]
    assert names("biryani") == ["Saffron"]
    assert names("near me") == []


def test_posts_match_text_within_categories_best_first():
    posts = [
        {"title": "Home-cooked Indian tiffin", "description": "Fresh dal and roti daily", "category": "eat"},
        {"title": "Mehndi artist", "description": "Indian bridal henna", "category": "lifestyle"},
        {"title": "Dog walking", "description": "Morning walks, 30 min", "category": "live"},
        {"title": "Sparkle Maids", "description": "Weekly house cleaning, deep clean on request", "category": "live"},
        {"title": "Pasta night", "description": "Italian dinners for 4", "category": "eat"},
    ]
    rank = lambda q: [p["title"] for p in search.rank(posts, search.parse_query(q), search.post_score)]
    assert rank("indian food") == ["Home-cooked Indian tiffin"]
    assert rank("tiffin") == ["Home-cooked Indian tiffin"]
    assert rank("food") == ["Home-cooked Indian tiffin", "Pasta night"]
    assert rank("dog walker") == ["Dog walking"]
    assert rank("henna") == ["Mehndi artist"]
    assert rank("italian dinners") == ["Pasta night"]
    assert rank("indian tiffin") == ["Home-cooked Indian tiffin"]
    assert rank("cleaning service near me") == ["Sparkle Maids"]
    assert rank("maid") == ["Sparkle Maids"]
    assert names("house cleaner") == []                  # no Discovered cards for cleaning (posts only)


@mock_aws
def test_get_discovered_with_q_filters_and_counts_keys_only():
    make_rate_table()
    table = make_discovered()
    table.put_item(Item={"areaZip": "19060", "ID": 0, "zips": ["19060", "19033"]})
    for n, c in enumerate(CARDS, 1):
        table.put_item(Item={"areaZip": "19060", "ID": 2026112300 + n, "expiresAt": FUTURE, "weekOf": "2026-11-23",
                             **c, "eventDate": "2099-01-01"})
    fn = load("eabu-discovered")
    fn._cache = None
    fn.lambda_client = SimpleNamespace(invoke=lambda **kw: None)

    def get(q, user="viewer"):
        event = {"httpMethod": "GET", "queryStringParameters": {"zip": "19033", "v": "2", "q": q}}
        if user:
            event["requestContext"] = {"authorizer": {"claims": {"sub": user}}}
        res = fn.lambda_handler(event, None)
        return json.loads(res["body"])

    body = get("Italian/Indian food near me")
    assert [i["place"] for i in body["Items"]] == ["Trattoria Rosa", "Saffron"]
    assert body["query"]["keys"] == ["italian", "indian"] and body["areaZip"] == "19060"
    assert get("dog parks", user=None).get("Items") is None                  # guests need the guest route
    guest_body = json.loads(fn.lambda_handler(guest("/guest/discovered", {"zip": "19033", "v": "2", "q": "dog parks"}), None)["body"])
    assert [i["place"] for i in guest_body["Items"]] == ["Bark Meadow"]
    get("sushi"); get("pupusas")
    rows = boto3.resource("dynamodb", region_name="us-east-1").Table("eabu-rate-limit").scan()["Items"]
    counts = [r for r in rows if r["key"].startswith("searches#19060#")]
    assert len(counts) == 1
    row = counts[0]
    assert (row["t_italian"], row["t_indian"], row["t_dogparks"], row["t_japanese"]) == (1, 1, 1, 1)
    assert not any("pupusa" in k for k in row)                                # typed text is never stored
    # Without q nothing is filtered or counted.
    assert len(json.loads(fn.lambda_handler({"httpMethod": "GET", "queryStringParameters": {"zip": "19033", "v": "2"},
                                             "requestContext": {"authorizer": {"claims": {"sub": "v"}}}}, None)["body"])["Items"]) == 8


@mock_aws
def test_popular_searches_steer_the_monday_prompt():
    make_rate_table()
    rate = boto3.resource("dynamodb", region_name="us-east-1").Table("eabu-rate-limit")
    for _ in range(3):
        search.count_search(rate, "19060", ["japanese", "dogparks"], now=NOW)
    search.count_search(rate, "19060", ["barber"], now=NOW)                       # once: not enough
    search.count_search(rate, "19060", ["thai"], now=datetime(2026, 9, 1, tzinfo=timezone.utc))  # too old
    search.count_search(rate, "19060", ["thai"], now=datetime(2026, 11, 17, tzinfo=timezone.utc))
    search.count_search(rate, "19060", ["thai"], now=datetime(2026, 11, 18, tzinfo=timezone.utc))
    popular = search.popular_searches(rate, "19060", now=NOW)
    assert popular[:3] == [("dogparks", 3), ("japanese", 3), ("thai", 2)]
    fn = load("eabu-discover")
    ask = fn.build_ask("19060", fn.topics_for_week(NOW.date()), [], NOW.date(), popular)
    assert "searching for: Japanese (sushi, ramen) (eat); Thai (eat)." in ask   # dog parks are a topic already
    assert "barbers (beauty)" not in ask
    assert "searching for" not in fn.build_ask("19060", fn.topics_for_week(NOW.date()), [], NOW.date())
    assert search.popular_searches(SimpleNamespace(get_item=lambda **k: 1 / 0), "19060") == []   # never fails the run


@mock_aws
def test_feeds_search_posts_and_dont_count_an_open():
    make_rate_table()
    _, fn = setup()
    listing("eat", "provider", "cook", title="Indian tiffin", description="Dal, rice and roti daily")
    listing("eat", "provider", "baker", title="Sourdough", description="Text 610-555-1234 to order")
    listing("lifestyle", "provider", "walker", title="Yoga in the park", description="Saturday mornings")
    me = {"httpMethod": "GET", "resource": "/favourites-feed/{userId}/{kind}",
          "pathParameters": {"userId": "ann", "kind": "seller"}}

    def nearby(q, **extra):
        res = fn.lambda_handler(as_user({**me, "queryStringParameters": {"zip": ZIP, "nearby": "1", "q": q, **extra}}, "ann"), None)
        assert res["statusCode"] == 200
        return json.loads(res["body"])

    assert [p["title"] for p in nearby("indian food near me")["Items"]] == ["Indian tiffin"]
    assert [p["title"] for p in nearby("yoga", limit="10")["Items"]] == ["Yoga in the park"]
    assert nearby("food", limit="1")["nextCursor"] is None and len(nearby("food", limit="1")["Items"]) == 1
    assert nearby("555")["Items"] == []
    rows = boto3.resource("dynamodb", region_name="us-east-1").Table("eabu-rate-limit").scan()["Items"]
    assert not any(r["key"].startswith("opens#") for r in rows)                  # a search isn't an open

    def guest_search(q):
        return json.loads(fn.lambda_handler(guest("/guest/feed", {"zip": ZIP, "limit": "10", "q": q}), None)["body"])["Items"]
    assert [p["title"] for p in guest_search("tiffin")] == ["Indian tiffin"]
    assert guest_search("1234") == [] and guest_search("610-555-1234") == []    # hidden numbers can't be probed
    assert "phone" not in guest_search("sourdough")[0]


def test_misspellings_are_reported_and_found():
    parsed = search.parse_query("Chienese Food near me")
    assert parsed["fixed"] == {"chienese": "chinese"}
    chinese = card("Golden Dragon", "eat", "Chinese restaurant", photo="chinese")
    assert names("Chienese Food", CARDS + [chinese]) == ["Golden Dragon"]
    assert search.parse_query("chinese")["fixed"] == {}


def test_real_words_are_never_corrected():
    assert search.REAL_WORDS and "chair" in search.REAL_WORDS
    for w in ("chair", "train", "plate", "bench", "mail", "horse", "glass", "clear", "paste", "drive"):
        assert search.fix_word(w) is None, w
    assert search.fix_word("chienese") == "chinese"


def test_household_posts_match_their_category():
    posts = [{"title": "Two movers with a truck", "description": "Local moves", "category": "move"},
             {"title": "Handyman", "description": "Repairs, TV mounting", "category": "live"},
             {"title": "Airport rides", "description": "PHL and EWR", "category": "move"}]
    rank = lambda q: [p["title"] for p in search.rank(posts, search.parse_query(q), search.post_score)]
    assert rank("movers") == ["Two movers with a truck"]
    assert rank("tv mounting") == ["Handyman"]
    assert rank("ride to the airport") == ["Airport rides"]
    assert names("movers") == []                         # no Discovered cards for these
