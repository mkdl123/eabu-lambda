#!/usr/bin/env python3
"""Test run for "Discovered near you": ask Claude (with web search) for this week's top-rated
places near a ZIP (food plus the week's two Lifestyle topics), print them with whether the
Lambda's checks would keep each one, and print what the run cost. Ticketmaster events aren't
part of this test.

Not part of the deployed app. Nothing is saved to AWS.

    pip install --user anthropic
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 scripts/discover_food.py 19103
    python3 scripts/discover_food.py 19103 --city Philadelphia --state PA
    python3 scripts/discover_food.py 19103 --json places.json   # also save the raw places
"""
import argparse
import datetime
import importlib.util
import json
import os
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

# The prompt, tool and checks come from the Lambda itself, so the two can't drift apart.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "eabu-dish", "python"))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
_spec = importlib.util.spec_from_file_location("discover_app", os.path.join(_HERE, "..", "eabu-dish", "eabu-discover", "app.py"))
discover = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(discover)
SAVE_TOOL, SYSTEM = discover.SAVE_TOOL, discover.SYSTEM


def run(model_key, zip_code, city, state, max_searches, max_fetches):
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

    topics = discover.topics_for_week(discover.week_start(today))
    messages = [{"role": "user", "content": discover.build_ask(zip_code, topics, [], today)}]

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

        saved = next((b for b in response.content if b.type == "tool_use" and b.name == "save_places"), None)
        if saved:
            return {**saved.input, "topics": topics}, totals
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        if response.stop_reason == "refusal":
            sys.exit("Claude declined this request.")
        # Finished without calling the tool: show its text so we can see why.
        text = "".join(b.text for b in response.content if b.type == "text")
        return {"places": [], "topics": topics, "notes": f"(no save_places call, stop_reason={response.stop_reason}) {text[:800]}"}, totals
    return {"places": [], "topics": topics, "notes": "Stopped after too many paused turns."}, totals


def cost(model_key, t):
    return t["token_cost"] + t["searches"] * PRICE_PER_SEARCH


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("zip")
    p.add_argument("--city")
    p.add_argument("--state", help="two-letter state, e.g. PA")
    p.add_argument("--searches", type=int, default=15, help="max web searches (default 15)")
    p.add_argument("--fetches", type=int, default=8, help="max pages opened to confirm details (default 6)")
    p.add_argument("--model", choices=sorted(MODELS), default="haiku", help="haiku (default, what the Lambda uses), sonnet or opus")
    p.add_argument("--json", help="also write the places to this file")
    a = p.parse_args()

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("Set your key first: export ANTHROPIC_API_KEY=sk-ant-...")

    try:
        result, totals = run(a.model, a.zip, a.city, a.state, a.searches, a.fetches)
    except anthropic.AuthenticationError:
        sys.exit("API key missing or invalid: export ANTHROPIC_API_KEY=sk-ant-...")
    except anthropic.RateLimitError:
        sys.exit("Rate limited; try again in a minute.")
    except anthropic.APIStatusError as e:
        sys.exit(f"API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        sys.exit("Network error reaching the API.")

    places = result.get("places", [])
    kept = {k["key"] for k in discover.check_places(places, result["topics"])}
    print(f"\nTop-rated near {a.zip}: {len(places)} place(s), {len(kept)} kept by the Lambda's checks\n" + "=" * 60)
    for i, f in enumerate(places, 1):
        mark = "KEPT" if discover.place_key(f["name"]) in kept else "dropped"
        print(f"\n{i}. [{f['topic']}] {f['name']} ({f['place_type']})  [{f['confidence']}, {mark}]")
        print(f"   ⭐ {f['rating']} on {f['rating_site']} ({f['rating_count']} reviews) · {f['address_or_area']}")
        print(f"   {f['what']}")
        print(f"   Source: {f['source_url']}")
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
