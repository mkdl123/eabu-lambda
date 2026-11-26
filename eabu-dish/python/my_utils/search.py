"""Search on Home ("Italian food near me", "yoga", "dog parks"): matches what eabu already has.

No web search and no Claude call: a search looks through this week's Discovered cards for the
area (GET /discovered?q=) and the current eabu posts within 50 miles (the nearby and guest feeds
with ?q=). When nothing matches, the app offers Apple/Google Maps and "Post a request".

parse_query turns the typed text into terms from a fixed vocabulary (TERMS) plus any other words,
which are matched literally:
  - "broad" terms (food, restaurant, eat...) mean a whole category, and are dropped when anything
    more specific is asked for ("italian food" = italian, not every restaurant);
  - "topic" terms (fitness, yoga, beauty, pets, dog parks, events) mean a whole Discovered topic;
  - "specific" terms (a cuisine, barber, nails, vet...) match the card's text or stock photo within
    their topic, and the post's title/description within their categories.
A card or post matches when it meets at least one term; more terms met rank it higher.

Searches are counted per area and week by vocabulary key only (never the typed text, never who
searched): rows `searches#<area>#<Monday>` in eabu-rate-limit with one `t_<key>` number per key,
kept SEARCH_KEEP_DAYS. The Monday eabu-discover run reads the last few weeks (popular_searches) and
asks Claude to include the most searched kinds of place within its usual counts, so the feed
drifts toward what people look for at no extra cost.
"""
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from my_utils import content_filter

logger = logging.getLogger(__name__)

MAX_QUERY = 60
MAX_WORDS = 5           # literal words used from one query
SEARCH_KEEP_DAYS = 35
POPULAR_WEEKS = 4
POPULAR_MIN = 2         # a key needs this many searches to steer the Monday run
POPULAR_MAX = 5

BROAD, TOPIC, SPECIFIC = "broad", "topic", "specific"
LIFESTYLE = ("lifestyle",)

# (key, level, Discovered topics, post categories, label for the Monday prompt, aliases).
# Aliases are matched as whole words (a trailing s/es is allowed) in the query, the card's text and
# the post's text. Keys that are stock photo keys also match a card with that photo. Keep the
# cuisine keys in step with PHOTO_KEYS["eat"] in eabu-discover.
TERMS = [
    ("food", BROAD, ("eat",), ("eat",), "food",
     ["food", "restaurant", "eat", "eats", "eating", "dinner", "lunch", "dining", "takeout", "take out",
      "cuisine", "meal", "hungry", "place to eat", "food truck", "food delivery", "meal delivery"]),
    ("homefood", SPECIFIC, ("eat",), ("eat",), "home-cooked food",
     ["tiffin", "home cooked", "homemade", "home made", "home food", "meal prep", "catering", "caterer"]),
    ("italian", SPECIFIC, ("eat",), ("eat",), "Italian", ["italian", "pasta", "trattoria", "osteria", "lasagna"]),
    ("pizza", SPECIFIC, ("eat",), ("eat",), "pizza", ["pizza", "pizzeria", "pizzas"]),
    ("indian", SPECIFIC, ("eat",), ("eat",), "Indian",
     ["indian", "curry", "biryani", "dosa", "tandoori", "punjabi", "gujarati", "bengali", "chaat", "thali",
      "masala", "nepali", "pakistani", "south indian", "north indian", "desi"]),
    ("chinese", SPECIFIC, ("eat",), ("eat",), "Chinese",
     ["chinese", "dim sum", "dumpling", "szechuan", "sichuan", "cantonese", "noodle", "hakka", "momo"]),
    ("mexican", SPECIFIC, ("eat",), ("eat",), "Mexican", ["mexican", "taco", "tacos", "taqueria", "burrito", "tamale"]),
    ("japanese", SPECIFIC, ("eat",), ("eat",), "Japanese (sushi, ramen)", ["japanese", "sushi", "ramen", "izakaya", "hibachi"]),
    ("thai", SPECIFIC, ("eat",), ("eat",), "Thai", ["thai"]),
    ("vietnamese", SPECIFIC, ("eat",), ("eat",), "Vietnamese", ["vietnamese", "pho", "banh mi"]),
    ("korean", SPECIFIC, ("eat",), ("eat",), "Korean", ["korean", "kbbq", "bibimbap"]),
    ("mediterranean", SPECIFIC, ("eat",), ("eat",), "Mediterranean or Greek", ["mediterranean", "greek", "turkish", "gyro"]),
    ("middle_eastern", SPECIFIC, ("eat",), ("eat",), "Middle Eastern",
     ["middle eastern", "lebanese", "falafel", "persian", "halal", "afghan", "shawarma", "kebab"]),
    ("american", SPECIFIC, ("eat",), ("eat",), "American (burgers, diners)", ["american", "burger", "diner", "wings"]),
    ("bbq", SPECIFIC, ("eat",), ("eat",), "BBQ or steak", ["bbq", "barbecue", "steak", "steakhouse", "smokehouse"]),
    ("seafood", SPECIFIC, ("eat",), ("eat",), "seafood", ["seafood", "fish", "oyster", "crab", "lobster"]),
    ("french", SPECIFIC, ("eat",), ("eat",), "French", ["french", "bistro", "brasserie"]),
    ("caribbean", SPECIFIC, ("eat",), ("eat",), "Caribbean or Latin",
     ["caribbean", "jamaican", "cuban", "puerto rican", "latin", "peruvian", "colombian", "dominican"]),
    ("southern", SPECIFIC, ("eat",), ("eat",), "Southern or soul food",
     ["southern", "soul food", "cajun", "creole", "fried chicken"]),
    ("cafe", SPECIFIC, ("eat",), ("eat",), "cafes and coffee", ["cafe", "café", "coffee", "espresso", "tea", "boba"]),
    ("bakery", SPECIFIC, ("eat",), ("eat",), "bakeries", ["bakery", "bread", "pastry", "donut", "doughnut", "croissant", "cake"]),
    ("dessert", SPECIFIC, ("eat",), ("eat",), "desserts and ice cream",
     ["dessert", "ice cream", "gelato", "frozen yogurt", "sweets", "mithai"]),
    ("breakfast", SPECIFIC, ("eat",), ("eat",), "breakfast and brunch", ["breakfast", "brunch", "pancake"]),
    ("deli", SPECIFIC, ("eat",), ("eat",), "delis and sandwiches", ["deli", "sandwich", "bagel", "hoagie", "sub"]),
    ("vegan", SPECIFIC, ("eat",), ("eat",), "vegan or vegetarian",
     ["vegan", "vegetarian", "veg", "plant based", "salad", "juice"]),

    ("fitness", TOPIC, ("fitness",), LIFESTYLE, "gyms",
     ["fitness", "gym", "workout", "work out", "exercise", "crossfit", "personal trainer", "strength training"]),
    ("boxing", SPECIFIC, ("fitness",), LIFESTYLE, "boxing and martial arts",
     ["boxing", "kickboxing", "martial art", "karate", "jiu jitsu", "bjj", "mma", "taekwondo", "self defense"]),
    ("cycling", SPECIFIC, ("fitness",), LIFESTYLE, "cycling and spin studios", ["cycling", "spin", "spinning"]),
    ("swim", SPECIFIC, ("fitness",), LIFESTYLE, "swimming pools", ["swim", "swimming", "pool", "aquatic"]),
    ("dance", SPECIFIC, ("fitness",), LIFESTYLE, "dance studios", ["dance", "dancing", "zumba", "salsa", "bollywood"]),
    ("yoga", TOPIC, ("yoga",), LIFESTYLE, "yoga", ["yoga", "meditation"]),
    ("pilates", SPECIFIC, ("yoga", "fitness"), LIFESTYLE, "pilates and barre", ["pilates", "barre", "reformer"]),
    ("beauty", TOPIC, ("beauty",), LIFESTYLE, "beauty", ["beauty", "beauty parlour", "beauty parlor", "parlour", "parlor"]),
    ("salon", SPECIFIC, ("beauty",), LIFESTYLE, "hair salons",
     ["salon", "hair", "haircut", "hair cut", "blowout", "stylist", "braids", "hair color"]),
    ("barber", SPECIFIC, ("beauty",), LIFESTYLE, "barbers", ["barber", "barbershop", "fade", "beard"]),
    ("nails", SPECIFIC, ("beauty",), LIFESTYLE, "nail salons", ["nail", "manicure", "pedicure", "mani pedi"]),
    ("spa", SPECIFIC, ("beauty",), LIFESTYLE, "day spas and massage", ["spa", "massage", "facial", "day spa"]),
    ("brows", SPECIFIC, ("beauty",), LIFESTYLE, "brows, lashes, threading and makeup",
     ["brow", "eyebrow", "threading", "waxing", "lash", "eyelash", "makeup", "mehndi", "henna"]),
    ("pets", TOPIC, ("pets",), LIFESTYLE + ("live",), "pet care",
     ["pet", "pet care", "animal", "dog", "puppy", "cat", "kitten"]),
    ("vet", SPECIFIC, ("pets",), LIFESTYLE + ("live",), "veterinarians",
     ["vet", "veterinarian", "veterinary", "animal hospital", "animal clinic"]),
    ("groomer", SPECIFIC, ("pets",), LIFESTYLE + ("live",), "pet groomers", ["groomer", "grooming", "dog grooming"]),
    ("petcare", SPECIFIC, ("pets",), LIFESTYLE + ("live",), "dog daycare, boarding and walkers",
     ["dog daycare", "daycare", "boarding", "kennel", "pet sitting", "pet sitter", "dog walker", "dog walking"]),
    ("dogparks", TOPIC, ("dogparks",), LIFESTYLE + ("live",), "dog parks",
     ["dog park", "dog run", "off leash", "off-leash", "bark park", "dog beach"]),
    # Household help and transport: eabu posts only (Live / Move categories); Discovered has no such
    # topics, so no cards (adding them would mean more Monday searches, i.e. more cost).
    ("cleaning", SPECIFIC, (), ("live",), "cleaning services",
     ["cleaning", "cleaner", "house cleaning", "home cleaning", "cleaning service", "deep clean", "deep cleaning",
      "maid", "housekeeping", "housekeeper", "janitorial", "carpet cleaning", "move out cleaning",
      "kaamwali", "kaam wali"]),
    ("plumber", SPECIFIC, (), ("live",), "plumbers",
     ["plumber", "plumer", "plumbing", "leak", "leaking", "drain", "clogged", "pipe", "water heater", "toilet repair"]),
    ("electrician", SPECIFIC, (), ("live",), "electricians",
     ["electrician", "electrical", "wiring", "electric repair", "light fixture"]),
    ("handyman", SPECIFIC, (), ("live",), "handymen and repairs",
     ["handyman", "handy man", "repair", "repairs", "fix", "fixing", "assembly", "furniture assembly",
      "mounting", "tv mounting", "carpenter", "carpentry", "appliance repair", "ac repair"]),
    ("painting", SPECIFIC, (), ("live",), "painters", ["painter", "painting", "house painting", "wall painting"]),
    ("yardwork", SPECIFIC, (), ("live",), "lawn care and gardening",
     ["lawn", "lawn care", "mowing", "gardener", "gardening", "landscaping", "yard work", "snow removal",
      "leaf removal", "mali"]),
    ("pestcontrol", SPECIFIC, (), ("live",), "pest control", ["pest control", "exterminator", "termite", "bed bug"]),
    ("laundry", SPECIFIC, (), ("live",), "laundry and ironing",
     ["laundry", "ironing", "dry cleaning", "wash and fold", "dhobi", "press wala"]),
    ("childcare", SPECIFIC, (), ("live",), "babysitters and nannies",
     ["babysitter", "babysitting", "baby sitter", "nanny", "childcare", "child care", "au pair"]),
    ("eldercare", SPECIFIC, (), ("live",), "elder care",
     ["elder care", "senior care", "caregiver", "home care", "companion care", "home nurse"]),
    ("tutoring", SPECIFIC, (), ("live", "lifestyle"), "tutors and lessons",
     ["tutor", "tutoring", "tuition", "homework help", "lesson", "music lesson", "piano", "guitar", "coaching",
      "math", "sat prep", "language class"]),
    ("cook", SPECIFIC, (), ("live", "eat"), "personal cooks", ["cook", "personal chef", "private chef", "chef"]),
    ("movers", SPECIFIC, (), ("move",), "movers",
     ["mover", "moving", "packers", "packers and movers", "moving help", "relocation", "haul", "hauling",
      "junk removal", "truck"]),
    ("rides", SPECIFIC, (), ("move",), "rides and drivers",
     ["ride", "rides", "driver", "taxi", "cab", "airport", "airport ride", "airport pickup", "chauffeur",
      "carpool", "car pool", "drop off", "pickup", "pick up", "auto rickshaw", "rickshaw"]),
    ("delivery", SPECIFIC, (), ("move",), "delivery and errands",
     ["delivery", "deliver", "courier", "errand", "errands", "grocery delivery"]),
    ("events", TOPIC, ("events",), (), "events",
     ["event", "concert", "live music", "music", "theatre", "theater", "comedy", "things to do",
      "something to do", "gig"]),
]

STOPWORDS = {
    "near", "nearby", "me", "around", "close", "closest", "best", "top", "good", "great", "nice", "cheap",
    "local", "the", "a", "an", "in", "at", "on", "my", "what", "whats", "where", "wheres", "which", "are", "is",
    "any", "some", "place", "places", "spot", "spots", "find", "show", "open", "now", "today", "tonight",
    "area", "for", "to", "of", "and", "or", "with", "i", "want", "need", "looking", "look", "there", "here",
    "rated", "highly", "recommend", "recommended", "options", "option", "get", "go", "can", "who", "how",
    "this", "week", "weekend", "nearest", "you", "your", "us", "please", "from", "by", "it", "do", "does",
    "house", "home", "service", "services", "help", "someone", "person", "company", "bai",
    "ke", "paas", "mein", "kaha", "kahan", "acha", "accha", "wala", "wali",
}

_TERMS = {t[0]: {"key": t[0], "level": t[1], "topics": t[2], "categories": t[3], "label": t[4]} for t in TERMS}


def _alias_pattern(alias):
    words = [re.escape(w) for w in re.split(r"[\s-]+", alias.lower()) if w]
    return r"[\s-]+".join(words) + r"(?:s|es)?"


# One pattern per term over all its aliases; longest aliases first so "dog park" wins over "dog".
for _key, *_rest, _aliases in TERMS:
    _TERMS[_key]["aliases"] = _aliases
    _TERMS[_key]["re"] = re.compile(r"(?<![\w])(?:" + "|".join(
        _alias_pattern(a) for a in sorted(_aliases, key=len, reverse=True)) + r")(?![\w])", re.I)
_ALIASES = sorted(((a, k) for k, *_r, aliases in TERMS for a in aliases), key=lambda x: -len(x[0]))
_ALIAS_RES = [(re.compile(r"(?<![\w])" + _alias_pattern(a) + r"(?![\w])", re.I), k) for a, k in _ALIASES]
_WORD = re.compile(r"[a-z0-9À-ɏऀ-ॿ]+")

# Spelling: a word the vocabulary doesn't know is corrected to the closest vocabulary word
# ("chienese" -> "chinese", "indain" -> "indian", "itallian" -> "italian") when it is 1 edit away
# (2 for words of 8+ letters). 4-letter words only when two neighbouring letters are swapped ("thia"),
# shorter ones never: too many real words are one letter from a vocabulary word.
FIX_MIN_LEN = 5
FIX_TWO_EDITS_LEN = 8
NEVER_FIX = {"wines", "beers", "hotel", "hotels", "motel", "books", "movie", "movies", "dates", "doctor",
             "dentist", "school"}


def _alias_words():
    for *_r, aliases in TERMS:
        for a in aliases:
            for w in re.split(r"[\s-]+", a.lower()):
                if w:
                    yield w
                    if len(w) > 3:
                        yield w + "s"


_VOCAB = sorted(set(_alias_words()))
_VOCAB_SET = set(_VOCAB)


def _edits(a, b, cap):
    """Edit distance (insert, delete, change, or swap two neighbours); cap + 1 once it exceeds cap."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[-1]


def _load_real_words():
    """Real English words near the vocabulary, which are never "corrected" (chair is not hair).
    Built by scripts/build_search_words.py from the macOS word list; rebuild after changing TERMS."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "search_real_words.txt")
    try:
        with open(path, encoding="utf-8") as f:
            return frozenset(w.strip() for w in f if w.strip() and not w.startswith("#"))
    except OSError:
        logger.warning("search_real_words.txt missing; spelling correction is off")
        return None


REAL_WORDS = _load_real_words()


def fix_word(word):
    """The vocabulary word a misspelling most likely meant, or None. A real word is never changed."""
    if REAL_WORDS is None or word in REAL_WORDS:
        return None
    if not word.isalpha() or word in _VOCAB_SET or word in STOPWORDS or word in NEVER_FIX:
        return None
    if len(word) == FIX_MIN_LEN - 1:  # a short word only when two neighbouring letters are swapped ("thia")
        swaps = {word[:i] + word[i + 1] + word[i] + word[i + 2:] for i in range(len(word) - 1)}
        return next((v for v in _VOCAB if v in swaps), None)
    if len(word) < FIX_MIN_LEN:
        return None
    cap = 2 if len(word) >= FIX_TWO_EDITS_LEN else 1
    best = None
    for v in _VOCAB:
        if len(v) < FIX_MIN_LEN - 1 or abs(len(v) - len(word)) > cap:
            continue
        d = _edits(word, v, cap)
        if d <= cap and (best is None or (d, v[0] != word[0], v) < best):
            best = (d, v[0] != word[0], v)
    return best[2] if best else None


def parse_query(q):
    """{"terms": [term dicts], "words": [other words], "keys": [vocabulary keys, for counting],
    "fixed": {misspelt word: what it was read as}}.
    Empty terms and words when the text is blank, all filler words, or blocked."""
    text = re.sub(r"\s+", " ", str(q or "")).strip().lower()[:MAX_QUERY]
    empty = {"terms": [], "words": [], "keys": [], "fixed": {}}
    if not text or content_filter.text_is_blocked(text):
        return empty
    text = text.replace("/", " ").replace("&", " ").replace(",", " ")
    fixed = {}
    for w in dict.fromkeys(_WORD.findall(text)):
        right = fix_word(w)
        if right:
            fixed[w] = right
    if fixed:
        text = _WORD.sub(lambda m: fixed.get(m.group(0), m.group(0)), text)
    found = {}  # key -> where it first appears, so keys keep the query's order
    for rx, key in _ALIAS_RES:   # longest phrases first, each matched span blanked out
        m = rx.search(text)
        if m:
            found[key] = min(found.get(key, m.start()), m.start())
            text = rx.sub(lambda x: " " * len(x.group(0)), text)
    keys = sorted(found, key=found.get)
    words = []
    for w in _WORD.findall(text):
        if w in STOPWORDS or len(w) < 3 or w.isdigit() or w in words:
            continue
        words.append(w)
    terms = [_TERMS[k] for k in keys]
    if any(t["level"] != BROAD for t in terms) or words:
        terms = [t for t in terms if t["level"] != BROAD]   # "italian food" = italian
    return {"terms": terms, "words": words[:MAX_WORDS], "keys": [t["key"] for t in terms], "fixed": fixed}


def is_empty(parsed):
    return not (parsed["terms"] or parsed["words"])


def _word_re(word):
    return re.compile(r"(?<![\w])" + re.escape(word), re.I)   # prefix: "tamale" finds "tamales"


def _card_text(card):
    return " ".join(str(card.get(k) or "") for k in ("place", "placeType", "what", "headline"))


def _card_topic(card):
    if card.get("topic"):
        return str(card["topic"])
    return "events" if card.get("kind") == "event" else str(card.get("category") or "eat")


def card_score(card, parsed):
    """How many of the query's terms and words this Discovered card meets (0 = no match)."""
    text, topic, photo = _card_text(card), _card_topic(card), str(card.get("photo") or "")
    score = 0
    for t in parsed["terms"]:
        if t["level"] in (BROAD, TOPIC):
            hit = topic in t["topics"] or (t["level"] == TOPIC and bool(t["re"].search(text)))
        else:
            hit = topic in t["topics"] and (photo == t["key"] or bool(t["re"].search(text)))
        score += hit
    score += sum(1 for w in parsed["words"] if _word_re(w).search(text))
    return score


def post_score(post, parsed):
    """How many of the query's terms and words this eabu post meets (0 = no match)."""
    text = f"{post.get('title') or ''} {post.get('description') or ''}"
    category = str(post.get("category") or "")
    score = 0
    for t in parsed["terms"]:
        if t["level"] == BROAD:
            score += category in t["categories"]
        else:
            score += category in t["categories"] and bool(t["re"].search(text))
    score += sum(1 for w in parsed["words"] if _word_re(w).search(text))
    return score


def rank(items, parsed, score):
    """The items that match, best first; ties keep their order (e.g. newest first)."""
    scored = [(score(i, parsed), n, i) for n, i in enumerate(items)]
    return [i for s, n, i in sorted((x for x in scored if x[0] > 0), key=lambda x: (-x[0], x[1]))]


# ---------------------------------------------------------------------------
# Counting (eabu-rate-limit) and the Monday hint
# ---------------------------------------------------------------------------
def _week(now):
    d = now.date()
    return (d - timedelta(days=d.weekday())).isoformat()


def count_search(table, area_zip, keys, now=None):
    """Add one search to each vocabulary key for the area this week. Never raises."""
    keys = [k for k in keys if k in _TERMS][:6]
    if not area_zip or not keys:
        return
    now = now or datetime.now(timezone.utc)
    try:
        names = {f"#k{n}": f"t_{k}" for n, k in enumerate(keys)}
        table.update_item(
            Key={"key": f"searches#{area_zip}#{_week(now)}"},
            UpdateExpression="ADD " + ", ".join(f"{n} :one" for n in names) + " SET expiresAt = if_not_exists(expiresAt, :exp)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={":one": 1, ":exp": int(now.timestamp()) + SEARCH_KEEP_DAYS * 86400})
    except Exception:  # noqa: BLE001 - a counter must never break the search
        logger.warning("Search count failed")


def popular_searches(table, area_zip, now=None, weeks=POPULAR_WEEKS):
    """[(key, searches)] for the area over the last `weeks` weeks, most searched first. [] on error."""
    now = now or datetime.now(timezone.utc)
    totals = {}
    try:
        for w in range(weeks):
            row = table.get_item(Key={"key": f"searches#{area_zip}#{_week(now - timedelta(weeks=w))}"}).get("Item") or {}
            for name, value in row.items():
                if name.startswith("t_") and name[2:] in _TERMS:
                    totals[name[2:]] = totals.get(name[2:], 0) + int(value)
    except Exception:  # noqa: BLE001 - the Monday run goes ahead without the hint
        logger.warning("Could not read searches for %s", area_zip)
        return []
    return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))


def monday_hint(popular, topics):
    """The line asking Claude to include the most searched kinds of place (specific terms within
    this run's topics, searched at least POPULAR_MIN times), or "" when there are none."""
    picks = []
    for key, n in popular:
        t = _TERMS.get(key)
        if t and t["level"] == SPECIFIC and n >= POPULAR_MIN and any(tp in topics for tp in t["topics"]):
            picks.append(f"{t['label']} ({t['topics'][0]})")
        if len(picks) == POPULAR_MAX:
            break
    if not picks:
        return ""
    return ("- People nearby have been searching for: " + "; ".join(picks) + ". Within the counts above, include "
            "at least one qualifying place for each of these where you can find one.")
