#!/usr/bin/env python3
# Build the X (Twitter) corpus for the tweet-drafting engine.
#
# - @antriksssh  → X API user-timeline, last 6 months, exclude replies/RTs
# - admired     → OpenAI web-search tool, top ~20 tweets per handle
#
# Writes one brain page per handle: corpus/x-<handle>.md.
# All output is markdown that will get embedded by gbrain on `put`.

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["PATH"] = "/home/botuser/.bun/bin:" + os.environ.get("PATH", "")

BRAIN_DIR = Path.home() / ".brain"
CORPUS_DIR = BRAIN_DIR / "corpus"
GBRAIN = "/home/botuser/.bun/bin/gbrain"
TODAY = datetime.now(timezone.utc).date().isoformat()

OWN_HANDLE = "antriksssh"
ADMIRED = ["paulg", "naval", "sama", "garrytan", "vkhosla"]
SIX_MONTHS_DAYS = 183
MAX_OWN_TWEETS = 1500


def log(msg):
    print(f"[x-corpus] {msg}", file=sys.stderr)


def vault_get(key):
    return subprocess.run(["bot-cred", "get", key], capture_output=True, text=True, check=True).stdout.strip()


# ---- own tweets via X API ----

def x_get(path, params, token):
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"https://api.x.com/2{path}?{qs}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_own_tweets(handle, token):
    user = x_get(f"/users/by/username/{handle}", {"user.fields": "name,description,public_metrics"}, token)
    if "data" not in user:
        sys.exit(f"ABORT: couldn't resolve @{handle}: {json.dumps(user)[:200]}")
    user_id = user["data"]["id"]
    user_name = user["data"]["name"]
    log(f"@{handle} resolved to user_id={user_id} ({user_name})")

    cutoff = datetime.now(timezone.utc) - timedelta(days=SIX_MONTHS_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    log(f"pulling tweets newer than {cutoff_str}")

    all_tweets = []
    pagination_token = None
    page = 0
    while page < 30 and len(all_tweets) < MAX_OWN_TWEETS:
        params = {
            "max_results": "100",
            "exclude": "replies,retweets",
            "start_time": cutoff_str,
            "tweet.fields": "created_at,public_metrics,entities,referenced_tweets",
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        resp = x_get(f"/users/{user_id}/tweets", params, token)
        tweets = resp.get("data", []) or []
        all_tweets.extend(tweets)
        page += 1
        log(f"  page {page}: +{len(tweets)} (total {len(all_tweets)})")
        pagination_token = (resp.get("meta") or {}).get("next_token")
        if not pagination_token:
            break

    all_tweets.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    return user_name, all_tweets


def render_own_corpus(handle, name, tweets):
    lines = [
        "---",
        "type: x_corpus",
        f"handle: {handle}",
        f"name: {name}",
        f"url: https://x.com/{handle}",
        "sourced: x_api",
        f"window: last_{SIX_MONTHS_DAYS}d",
        f"created: {TODAY}",
        f"updated: {TODAY}",
        f"tweet_count: {len(tweets)}",
        "---",
        "",
        f"# @{handle} corpus ({name})",
        "",
        f"Last {SIX_MONTHS_DAYS} days, main tweets only (replies + RTs excluded). Source: X API.",
        "Used by `/x-draft` for own-voice signal in the blend.",
        "",
        "## Approved drafts",
        "",
        "_Approved drafts from `/x-draft approve` will be appended here over time._",
        "",
        "## Tweets",
        "",
    ]
    for t in tweets:
        d = (t.get("created_at") or "")[:10]
        m = t.get("public_metrics") or {}
        likes = m.get("like_count", 0)
        rts = m.get("retweet_count", 0)
        text = (t.get("text") or "").strip()
        lines.append(f"### {d} — {likes} likes, {rts} RTs")
        for ln in text.split("\n"):
            lines.append(f"> {ln}")
        lines.append("")
    return "\n".join(lines) + "\n"


# ---- admired corpora via OpenAI web search ----

WEB_SYSTEM_PROMPT = """You are gathering data for a tweet-style training corpus. The user wants the ~20 most representative or most-cited tweets by a given X (Twitter) account — the ones that capture the account's voice, recurring themes, and rhetorical style.

Use web search to find these. Sources: tweet anthologies, listicles, news articles quoting the account, screenshots, the account's own pinned/popular tweets.

Output STRICT JSON only, no preamble, no markdown fences. Schema:

{
  "voice_notes": "2-4 sentences describing this account's voice: typical sentence length, recurring rhetorical moves, vocabulary signatures, opening patterns, what they avoid.",
  "tweets": [
    {
      "text": "the tweet text, as accurately as possible (paraphrasing OK if the source is a screenshot or article quote)",
      "approx_date": "YYYY or YYYY-MM if known, else null",
      "context": "one short line on why this tweet is notable or representative",
      "source_url": "URL of the page where you found this"
    }
  ]
}

Rules:
- Aim for 15-20 tweets per account.
- Prefer tweets that are widely cited / iconic / often-quoted.
- Each tweet text MUST be the tweet itself, not commentary about the tweet.
- Skip tweets that are just images / videos with no text.
- Skip very recent tweets unless they're widely shared — recent != iconic.
- Do NOT invent tweets. If you can't verify the text, skip it.
- If you can only find 8-10 verified tweets, return those rather than padding with low-quality ones."""


def fetch_admired(handle, openai_key):
    user_msg = (
        f"Account: @{handle}\n"
        f"X URL: https://x.com/{handle}\n\n"
        f"Find 15-20 of their most representative, most-cited, or iconic tweets. "
        f"Return the JSON object specified in the system prompt."
    )
    payload = {
        "model": "gpt-5.4-mini",
        "input": user_msg,
        "instructions": WEB_SYSTEM_PROMPT,
        "tools": [{"type": "web_search_preview", "search_context_size": "high"}],
        "max_output_tokens": 8000,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        resp = json.loads(r.read())
    text = ""
    for item in resp.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    text = c["text"]
                    break
    if not text:
        log(f"@{handle} no output_text in response: {json.dumps(resp)[:400]}")
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        log(f"@{handle} JSON parse error: {e}; first 400 of text: {text[:400]}")
        return None


def render_admired_corpus(handle, data):
    lines = [
        "---",
        "type: x_corpus",
        f"handle: {handle}",
        f"url: https://x.com/{handle}",
        "sourced: web",
        f"created: {TODAY}",
        f"updated: {TODAY}",
        f"tweet_count: {len(data.get('tweets', []))}",
        "---",
        "",
        f"# @{handle} corpus (top tweets, web-sourced)",
        "",
        "Curated set of high-signal tweets. Sourced via OpenAI web search + LLM extraction. ",
        "Tweet text may be paraphrased ~1-3% from original (screenshots/articles get retyped). ",
        "Used by `/x-draft` for admired-voice signal in the blend.",
        "",
        "## Voice notes",
        "",
        data.get("voice_notes", "_(none extracted)_"),
        "",
        "## Tweets",
        "",
    ]
    for i, t in enumerate(data.get("tweets", []), 1):
        date_str = t.get("approx_date") or "date unknown"
        lines.append(f"### {i}. {date_str}")
        for ln in (t.get("text") or "").split("\n"):
            lines.append(f"> {ln}")
        if t.get("context"):
            lines.append(f"- _Context:_ {t['context']}")
        if t.get("source_url"):
            lines.append(f"- _Source:_ {t['source_url']}")
        lines.append("")
    return "\n".join(lines) + "\n"


# ---- write + commit ----

def save_page(slug, body):
    path = BRAIN_DIR / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    subprocess.run([GBRAIN, "put", slug], input=body, text=True, capture_output=True, check=False)
    log(f"wrote + put {slug}")


def git_commit():
    cwd = str(BRAIN_DIR)
    if not (BRAIN_DIR / ".git").exists():
        return
    subprocess.run(["git", "add", "corpus/"], cwd=cwd, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=alfred@bot", "-c", "user.name=Alfred",
         "commit", "-m", f"x-corpus collected {TODAY}"],
        cwd=cwd, capture_output=True
    )


def main():
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    only = sys.argv[1] if len(sys.argv) > 1 else "all"

    summary = {"own": None, "admired": {}}

    if only in ("all", "own"):
        log(f"=== own: @{OWN_HANDLE} ===")
        token = vault_get("X_API_BEARER_TOKEN")
        name, tweets = fetch_own_tweets(OWN_HANDLE, token)
        body = render_own_corpus(OWN_HANDLE, name, tweets)
        save_page(f"corpus/x-{OWN_HANDLE}", body)
        summary["own"] = {"handle": OWN_HANDLE, "count": len(tweets)}

    if only in ("all", "admired"):
        openai_key = vault_get("OPENAI_API_KEY")
        for handle in ADMIRED:
            log(f"=== admired: @{handle} ===")
            data = fetch_admired(handle, openai_key)
            if not data:
                log(f"@{handle} fetch returned None; skipping")
                summary["admired"][handle] = {"error": "no_data"}
                continue
            body = render_admired_corpus(handle, data)
            save_page(f"corpus/x-{handle}", body)
            summary["admired"][handle] = {"count": len(data.get("tweets", []))}

    git_commit()
    subprocess.run([GBRAIN, "sync"], capture_output=True, check=False)
    print(json.dumps({"ok": True, "summary": summary}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {e}")
        print(json.dumps({"ok": False, "reason": "error", "error": str(e)}))
        sys.exit(1)
