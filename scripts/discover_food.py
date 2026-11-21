#!/usr/bin/env python3
"""Test run for "Discovered near you": ask Claude (with web search) for new food
finds near a ZIP this week, print them, and print what the run cost.

Not part of the deployed app. Nothing is saved to AWS.

    pip install --user anthropic
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 scripts/discover_food.py 19103
    python3 scripts/discover_food.py 19103 --city Philadelphia --state PA --max 8
    python3 scripts/discover_food.py 19103 --json finds.json   # also save the raw finds
"""
import argparse
import datetime
import json
import sys

import anthropic

# Approximate list prices (USD per million tokens: input, output, cache read) for the cost line.
# Check the pricing page before relying on them.
MODELS = {
    "opus": ("claude-opus-5-5", 4.00, 20.00, 0.20),
    "sonnet": ("claude-sonnet-5-5", 2.00, 10.00, 0.10),
    "haiku": ("claude-haiku-5-5", 0.10, 0.50, 0.01),
}
# Haiku 5.5 charges more for a request whose prompt is over 100K tokens.
HAIKU_LONG = (0.50, 2.50, 0.05)
PRICE_PER_SEARCH = 0.01

SAVE_TOOL = {
    "name": "save_finds",
    "description": "Save the final list of food finds. Call this exactly once, after searching.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "finds": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "headline": {"type": "string", "description": "Short, fun one-liner, e.g. 'Sweet potato fries for breakfast'"},
                        "place": {"type": "string", "description": "Restaurant, cafe, truck or market name"},
                        "address_or_area": {"type": "string", "description": "Street address if known, else neighbourhood/town"},
                        "what": {"type": "string", "description": "One or two sentences in your own words, no copied text"},
                        "when": {"type": "string", "description": "Dates/days/hours it applies, as stated by the source"},
                        "why_new": {"type": "string", "description": "Why it counts as new this week (new menu item, opening, limited special, event)"},
                        "source_url": {"type": "string"},
                        "source_date": {"type": "string", "description": "Publish/post date of the source, YYYY-MM-DD, or 'unknown'"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": ["headline", "place", "address_or_area", "what", "when",
                                 "why_new", "source_url", "source_date", "confidence"],
                    "additionalProperties": False,
                },
            },
            "notes": {"type": "string", "description": "Anything worth knowing about this run (thin results, gaps)"},
        },
        "required": ["finds", "notes"],
        "additionalProperties": False,
    },
}

SYSTEM = """You find what is new in local food for an app's "Discovered near you" feed.

Search the public web for food items or food happenings near the given US ZIP code that are new or
limited for this week: new menu items, daily/weekly specials, seasonal items, new openings, pop-ups,
food trucks at a new spot, farmers market or food festival happenings.

Rules:
- Only include things whose source is dated within the last 14 days, or that state dates covering this week.
  Skip anything you can't date.
- Within about 25 miles of the ZIP. Prefer independent local places over national chain promotions.
- Each find needs a real source URL you actually saw. It must be the specific article, post or menu page,
  never a site's homepage or a section index. If you only saw a headline, open the page with web_fetch to
  confirm the details first; if you can't confirm them, leave the find out.
- Spread your searches across the whole area (nearby towns, farmers markets, food trucks), not one source.
  Use at most 2 finds from any one article, and at most 2 holiday-menu finds in total.
- Save fetches for the finds you'll actually include; one search result page or article per find is enough.
- Write descriptions in your own words; don't copy text from the source.
- No alcohol-only events, nothing sexual, no coupons that require sign-up.
- Fewer good finds beat more weak ones. If you find nothing solid, return an empty list and say why in notes.

When done searching, call save_finds once with your picks, best first."""


def run(model_key, zip_code, city, state, max_finds, max_searches, max_fetches):
    model = MODELS[model_key][0]
    client = anthropic.Anthropic()
    today = datetime.date.today()

    web_search = {"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}
    web_fetch = {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": max_fetches,
                 "max_content_tokens": 8000}
    if city or state:
        web_search["user_location"] = {
            "type": "approximate", "country": "US",
            **({"city": city} if city else {}), **({"region": state} if state else {}),
        }

    where = f"ZIP {zip_code}" + (f" ({city}, {state})" if city and state else "")
    messages = [{
        "role": "user",
        "content": f"Today is {today:%A, %B %d, %Y}. Find up to {max_finds} new or limited food finds "
                   f"this week near {where}.",
    }]

    totals = {"input": 0, "output": 0, "cache_read": 0, "searches": 0, "fetches": 0, "token_cost": 0.0}
    for _ in range(8):  # a few pause_turn resumes at most
        # Server-side refusal fallback exists for Opus/Sonnet, not Haiku.
        fallback = {} if model_key == "haiku" else {
            "betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            **fallback,
            output_config={"effort": "medium"},
            system=SYSTEM,
            tools=[web_search, web_fetch, SAVE_TOOL],
            messages=messages,
        )
        u = response.usage
        totals["input"] += u.input_tokens or 0
        totals["output"] += u.output_tokens or 0
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        totals["cache_read"] += cache_read
        _, p_in, p_out, p_cache = MODELS[model_key]
        if model_key == "haiku" and (u.input_tokens or 0) + cache_read > 100_000:
            p_in, p_out, p_cache = HAIKU_LONG
        totals["token_cost"] += ((u.input_tokens or 0) * p_in + (u.output_tokens or 0) * p_out
                                 + cache_read * p_cache) / 1_000_000
        stu = getattr(u, "server_tool_use", None)
        totals["searches"] += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        totals["fetches"] += (getattr(stu, "web_fetch_requests", 0) or 0) if stu else 0

        saved = next((b for b in response.content if b.type == "tool_use" and b.name == "save_finds"), None)
        if saved:
            return saved.input, totals
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        if response.stop_reason == "refusal":
            sys.exit("Claude declined this request.")
        # Finished without calling the tool: show its text so we can see why.
        text = "".join(b.text for b in response.content if b.type == "text")
        return {"finds": [], "notes": f"(no save_finds call, stop_reason={response.stop_reason}) {text[:800]}"}, totals
    return {"finds": [], "notes": "Stopped after too many paused turns."}, totals


def cost(model_key, t):
    return t["token_cost"] + t["searches"] * PRICE_PER_SEARCH


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("zip")
    p.add_argument("--city")
    p.add_argument("--state", help="two-letter state, e.g. PA")
    p.add_argument("--max", type=int, default=8, help="max finds (default 8)")
    p.add_argument("--searches", type=int, default=15, help="max web searches (default 15)")
    p.add_argument("--fetches", type=int, default=6, help="max pages opened to confirm details (default 6)")
    p.add_argument("--model", choices=sorted(MODELS), default="opus", help="opus (default), sonnet or haiku")
    p.add_argument("--json", help="also write the finds to this file")
    a = p.parse_args()

    import os
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("Set your key first: export ANTHROPIC_API_KEY=sk-ant-...")

    try:
        result, totals = run(a.model, a.zip, a.city, a.state, a.max, a.searches, a.fetches)
    except anthropic.AuthenticationError:
        sys.exit("API key missing or invalid: export ANTHROPIC_API_KEY=sk-ant-...")
    except anthropic.RateLimitError:
        sys.exit("Rate limited; try again in a minute.")
    except anthropic.APIStatusError as e:
        sys.exit(f"API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        sys.exit("Network error reaching the API.")

    finds = result.get("finds", [])
    print(f"\nDiscovered near {a.zip}: {len(finds)} find(s)\n" + "=" * 60)
    for i, f in enumerate(finds, 1):
        print(f"\n{i}. {f['headline']}  [{f['confidence']}]")
        print(f"   {f['place']} · {f['address_or_area']}")
        print(f"   {f['what']}")
        print(f"   When: {f['when']}   Why new: {f['why_new']}")
        print(f"   Source ({f['source_date']}): {f['source_url']}")
    if result.get("notes"):
        print(f"\nNotes: {result['notes']}")
    print(f"\nCost: ~${cost(a.model, totals):.3f} with {MODELS[a.model][0]}  ({totals['searches']} searches, "
          f"{totals['fetches']} pages opened, {totals['input']:,} in / {totals['output']:,} out tokens)")

    if a.json:
        with open(a.json, "w") as fh:
            json.dump({"zip": a.zip, "date": str(datetime.date.today()), **result, "model": MODELS[a.model][0], "usage": totals}, fh, indent=2)
        print(f"Saved to {a.json}")


if __name__ == "__main__":
    main()
