"""Keep sexual content out of the app: a word filter for text and Rekognition for pictures.

Text: blocked_text_field(payload) returns the first field whose text contains a blocked word
or phrase. Matching is on whole words after lowercasing, removing accents and undoing common
look-alike spellings (s3x, p0rn, $ex), so "Sussex" or "analysis" are fine. A few long,
unambiguous terms are also caught inside other text or with separators (p.o.r.n.h.u.b).

Pictures: picture_block_reason(image_bytes) asks Amazon Rekognition DetectModerationLabels
and returns the label that blocks the picture, or None. Explicit nudity, sexual activity and
partial nudity are blocked; swimwear, bare backs and kissing are allowed (fitness, beauty and
swimming-lesson listings). If Rekognition can't be reached it raises ModerationUnavailable and
the caller refuses the upload: an unchecked picture is never stored.

Edit the lists below to tune it; tests/unit/test_content_filter.py covers both.
"""
import logging
import os
import re
import unicodedata

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

# Client-supplied text that other users can see.
TEXT_FIELDS = ("title", "subtitle", "description", "offerDescription", "message", "unit",
               "serviceType", "name", "address", "url")

# Whole words (a trailing "s"/"es" plural also matches).
BLOCKED_WORDS = {
    "sex", "sexy", "sexual", "sexting", "porn", "porno", "pornography", "pornographic",
    "pornstar", "xxx", "nsfw", "nudity", "topless", "erotic", "erotica", "horny", "slut",
    "whore", "hooker", "prostitute", "prostitution", "escort", "stripper", "striptease",
    "fetish", "bdsm", "orgasm", "orgy", "masturbate", "masturbation", "blowjob", "handjob",
    "rimjob", "cumshot", "creampie", "gangbang", "threesome", "milf", "dildo", "buttplug",
    "penis", "vagina", "boobs", "boobies", "tits", "titties", "cock", "pussy", "fuck",
    "fucking", "hentai", "camgirl", "camboy", "onlyfans", "fansly", "pornhub", "xvideos",
    "xhamster", "chaturbate", "hookup", "gfe", "nuru",
}
# Not blocked on purpose, because real listings use them: nude (lipstick, nails), naked (cakes,
# burritos), kinky (twist braids), nipple (baby bottles), anal (pet gland care), cum (laude),
# vibrator (concrete, massage tools), dick (a name).

# Phrases, matched on whole words.
BLOCKED_PHRASES = (
    "happy ending", "body rub", "body to body", "sensual massage", "erotic massage",
    "adult massage", "nude massage", "tantric massage", "strip club", "sugar daddy",
    "sugar baby", "sugar mommy", "friends with benefits", "no strings attached", "one night stand",
    "adult entertainment", "adult services", "adult content", "sex work", "sex worker",
    "only fans", "send nudes", "for adults only",
)

# Long, unambiguous terms that are also caught inside other words or spelled with
# separators ("xxpornhubxx", "o n l y f a n s"). Keep these 6+ letters, never short words.
BLOCKED_ANYWHERE = ("pornhub", "onlyfans", "blowjob", "handjob", "gangbang", "cumshot",
                    "xvideos", "xhamster", "chaturbate", "pornstar", "pornograph")

# Caught inside a single word ("pornking"), but never across words: "pickup or nearby"
# squashed together contains "porn".
BLOCKED_IN_WORD = ("porn",)

# Look-alike characters people use to dodge filters.
_LOOKALIKES = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t",
                             "@": "a", "$": "s", "!": "i", "|": "i"})
_NON_WORD = re.compile(r"[^a-z]+")
_PHRASE_SET = {tuple(p.split()) for p in BLOCKED_PHRASES}
_MAX_PHRASE = max(len(p) for p in _PHRASE_SET)


def _forms(text: str):
    """The lowercase, accent-free text, plus its look-alike-decoded version if different."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    decoded = plain.translate(_LOOKALIKES)
    return (plain,) if decoded == plain else (plain, decoded)


def _word_blocked(word: str) -> bool:
    if word in BLOCKED_WORDS:
        return True
    for suffix in ("es", "s"):
        if word.endswith(suffix) and word[: -len(suffix)] in BLOCKED_WORDS:
            return True
    return False


def text_is_blocked(text) -> bool:
    if not isinstance(text, str) or not text:
        return False
    for form in _forms(text):
        words = _NON_WORD.sub(" ", form).split()
        if any(_word_blocked(w) or any(t in w for t in BLOCKED_IN_WORD) for w in words):
            return True
        for size in range(2, _MAX_PHRASE + 1):
            if any(tuple(words[i:i + size]) in _PHRASE_SET for i in range(len(words) - size + 1)):
                return True
        squashed = "".join(words)
        if any(term in squashed for term in BLOCKED_ANYWHERE):
            return True
    return False


def blocked_text_field(payload: dict, fields=TEXT_FIELDS):
    """Return the name of the first field with blocked text, or None."""
    for field in fields:
        if text_is_blocked(payload.get(field)):
            return field
    return None


def blocked_text_message(field: str) -> str:
    label = {"offerDescription": "description", "serviceType": "service type"}.get(field, field)
    return (f"Your {label} contains words that aren't allowed. "
            f"Sexual or adult content can't be posted on Eabu.")


# ---------------------------------------------------------------------------------------
# Pictures

# Rekognition labels that block a picture. Names from both the current (v7) and the older
# (v6) moderation taxonomies, since Rekognition returns whichever model the account gets.
# Deliberately absent: Swimwear or Underwear, Bare Back, Exposed Male Nipple, Kissing on the
# Lips (and their parents, which Rekognition also returns alongside them).
BLOCKED_PICTURE_LABELS = {
    # v7
    "Explicit", "Explicit Nudity", "Explicit Sexual Activity", "Exposed Female Genitalia",
    "Exposed Male Genitalia", "Exposed Buttocks or Anus", "Exposed Female Nipple",
    "Sex Toys", "Obstructed Intimate Parts", "Obstructed Female Nipple",
    "Obstructed Male Genitalia", "Partially Exposed Buttocks", "Partially Exposed Female Breast",
    "Implied Nudity",
    # v6
    "Nudity", "Graphic Male Nudity", "Graphic Female Nudity", "Sexual Activity",
    "Illustrated Explicit Nudity", "Adult Toys", "Partial Nudity", "Sexual Situations",
}
MIN_CONFIDENCE = float(os.environ.get("PICTURE_MODERATION_MIN_CONFIDENCE", "70"))

_rekognition = None


class ModerationUnavailable(Exception):
    """Rekognition couldn't check the picture; the upload must be refused."""


def _client():
    global _rekognition
    if _rekognition is None:
        _rekognition = boto3.client("rekognition", region_name=os.environ.get("REGION_NAME", "us-east-1"))
    return _rekognition


def picture_block_reason(image_bytes: bytes):
    """Return the Rekognition label that blocks this picture, or None if it's allowed."""
    try:
        labels = _client().detect_moderation_labels(
            Image={"Bytes": image_bytes}, MinConfidence=MIN_CONFIDENCE).get("ModerationLabels", [])
    except (BotoCoreError, ClientError) as e:
        logger.error("Picture moderation failed: %s", type(e).__name__)
        raise ModerationUnavailable() from e
    for label in labels:
        for name in (label.get("Name"), label.get("ParentName")):
            if name in BLOCKED_PICTURE_LABELS:
                return name
    return None


PICTURE_REJECTED_MESSAGE = ("This picture isn't allowed. Pictures with nudity or sexual content "
                            "can't be posted on Eabu.")
PICTURE_CHECK_FAILED_MESSAGE = "We couldn't check this picture. Please try again."
