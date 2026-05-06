#!/usr/bin/env python3
# Tweet drafting engine. Reads identity/X_VOICE.md + relevant corpus
# excerpts, produces a draft via GPT-5.4-mini, enforces 280-char limit,
# and persists pending-draft state for the approve/edit/reject flow.
#
# Usage:
#   x-draft.py new "<raw thought>"            [--style <handle>]
#   x-draft.py reply <tweet-url> "<raw>"      [--style <handle>]
#   x-draft.py quote <tweet-url> "<raw>"      [--style <handle>]
#
# Prints a single-line JSON envelope. Pending-draft state stored at
# ~/.brain-bootstrap-raw/state/x-draft-pending.json.

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

os.environ["PATH"] = "/home/botuser/.bun/bin:" + os.environ.get("PATH", "")

BRAIN_DIR = Path.home() / ".brain"
WORKSPACE = Path.home() / ".openclaw" / "workspace"
STATE_DIR = Path.home() / ".brain-bootstrap-raw" / "state"
STATE_FILE = STATE_DIR / "x-draft-pending.json"
VOICE_PATH = WORKSPACE / "identity" / "X_VOICE.md"
GBRAIN = "/home/botuser/.bun/bin/gbrain"

OWN_HANDLE = "antriksssh"
ADMIRED = ["paulg", "naval", "sama", "garrytan", "vkhosla"]
MAX_CHARS = 280


def log(msg):
    print(f"[x-draft] {msg}", file=sys.stderr)


def vault_get(key):
    return subprocess.run(["bot-cred", "get", key], capture_output=True, text=True, check=True).stdout.strip()


def load_voice_profile():
    if VOICE_PATH.exists():
        return VOICE_PATH.read_text()
    return "_(X_VOICE.md not yet built — run x-style-profile.py first)_"


def load_corpus_excerpt(handle, max_chars=8000):
    p = BRAIN_DIR / "corpus" / f"x-{handle}.md"
    if not p.exists():
        return ""
    text = p.read_text()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n_(truncated — see full page in brain)_"


def fetch_target_tweet(url, token):
    m = re.search(r"/status/(\d+)", url)
    if not m:
        return None
    tid = m.group(1)
    qs = urllib.parse.urlencode({
        "expansions": "author_id",
        "tweet.fields": "created_at,public_metrics,entities",
        "user.fields": "username,name",
    })
    req = urllib.request.Request(
        f"https://api.x.com/2/tweets/{tid}?{qs}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
    except Exception as e:
        log(f"target tweet fetch failed: {e}")
        return None
    if "data" not in d:
        return None
    users = {u["id"]: u for u in d.get("includes", {}).get("users", [])}
    author = users.get(d["data"].get("author_id"), {})
    return {
        "id": tid,
        "text": d["data"].get("text", ""),
        "author_handle": author.get("username", ""),
        "author_name": author.get("name", ""),
        "url": url,
    }


def build_blend_directive(style_override):
    if not style_override:
        return (
            f"Default blend: 20% @{OWN_HANDLE} (own voice), 80% averaged across "
            f"@{', @'.join(ADMIRED)}. Anchor in admired-voice texture; layer Antriksh's "
            f"openers, vocabulary, and closers on top."
        )
    sh = style_override.lower().lstrip("@")
    if sh in ("antriksh", "antriksssh", "self", "me"):
        return f"Override: pure @{OWN_HANDLE} voice. 100% own voice — no admired blend."
    if sh in ADMIRED:
        others = [h for h in ADMIRED if h != sh]
        return (
            f"Override: lean hard into @{sh}. Blend: 60% @{sh}, 20% @{OWN_HANDLE}, "
            f"20% averaged across @{', @'.join(others)}."
        )
    return (
        f"Override requested for @{sh} but it's not in the admired set. "
        f"Falling back to default blend (20/80)."
    )


def mode_directive(mode, target):
    if mode == "new":
        return "Mode: NEW TWEET. Stand-alone post from a raw thought. Carry the full voice — opener, body, closer."
    if mode == "reply":
        return (
            f"Mode: REPLY to @{target['author_handle']}'s tweet:\n"
            f"> {target['text']}\n\n"
            f"Replies are conversational and direct. Acknowledge the parent tweet implicitly. "
            f"Often shorter than a stand-alone tweet. No need to restate context already in the parent."
        )
    if mode == "quote":
        return (
            f"Mode: QUOTE RETWEET of @{target['author_handle']}'s tweet:\n"
            f"> {target['text']}\n\n"
            f"Quote tweets add YOUR take ON the quoted content. Must be about the quoted tweet, "
            f"not generic commentary. The reader sees both your text AND the quoted tweet, so don't "
            f"restate what they can already see."
        )
    return "Mode: UNKNOWN."


SYSTEM_PROMPT_BASE = """You are drafting an X (Twitter) post in Antriksh's voice, blended with the voices of accounts he admires.

The voice profile below is the master spec. Follow the blend recipe in it. Look at the corpus excerpts to ground each pattern in actual examples — don't drift into generic LLM-tweet style (no "🚀", no "Let's go!", no "Here's a thread", no hashtags unless the corpus shows them).

Hard rules:
1. Output MUST be ≤ 280 characters. Count carefully. Spaces, punctuation, and emoji all count.
2. Output ONLY the tweet text. No preamble, no explanation, no quotes around it, no markdown.
3. Do NOT use em dashes (—). Use hyphens, periods, or rewrite the sentence.
4. Do NOT invent facts, statistics, @-handles, or URLs not present in the input.
5. Match the tone: Antriksh + admired-voice patterns, NOT corporate-LLM polish.
6. If the raw thought has profanity or strong opinions, KEEP THEM — don't sanitize."""


def generate(prompt_user_msg):
    sys.path.insert(0, "/home/botuser/bin")
    import llm  # type: ignore
    text, provider = llm.call(
        system_prompt=SYSTEM_PROMPT_BASE,
        user_msg=prompt_user_msg,
        tier="mini",
        max_tokens=800,
    )
    log(f"generate provider={provider}")
    return text


def enforce_length(draft, prompt_user_msg):
    """If over 280 chars, retry once with explicit instruction."""
    if len(draft) <= MAX_CHARS:
        return draft, True
    log(f"draft is {len(draft)} chars > {MAX_CHARS}; retrying with explicit cap")
    retry_msg = (
        prompt_user_msg
        + f"\n\nYour previous draft was {len(draft)} characters, which exceeds the 280-char limit. "
        + f"Previous draft:\n\n{draft}\n\nTighten it to ≤ 280 characters. Output only the new tweet text."
    )
    new_draft = generate(retry_msg)
    return new_draft, len(new_draft) <= MAX_CHARS


def build_prompt(mode, raw, target, style_override):
    voice = load_voice_profile()
    own_corpus = load_corpus_excerpt(OWN_HANDLE, max_chars=4000)
    admired_excerpts = "\n\n".join(
        f"=== @{h} corpus excerpt ===\n{load_corpus_excerpt(h, max_chars=2500)}"
        for h in ADMIRED
    )
    blend = build_blend_directive(style_override)
    mode_dir = mode_directive(mode, target)

    parts = [
        "## VOICE PROFILE (master spec)",
        voice,
        "",
        "## BLEND DIRECTIVE",
        blend,
        "",
        "## MODE",
        mode_dir,
        "",
        "## OWN CORPUS EXCERPT (@antriksssh)",
        own_corpus,
        "",
        "## ADMIRED CORPUS EXCERPTS",
        admired_excerpts,
        "",
        "## RAW INPUT",
        raw,
        "",
        "Generate the tweet now. Output ONLY the tweet text, ≤ 280 characters, no preamble.",
    ]
    return "\n\n".join(parts)


def main():
    if len(sys.argv) < 3:
        sys.exit("Usage: x-draft.py {new <raw>|reply <url> <raw>|quote <url> <raw>} [--style <handle>]")

    mode = sys.argv[1]
    style_override = None
    args = sys.argv[2:]
    if "--style" in args:
        i = args.index("--style")
        style_override = args[i + 1]
        args = args[:i] + args[i + 2:]

    target = None
    if mode == "new":
        raw = args[0]
    elif mode in ("reply", "quote"):
        if len(args) < 2:
            sys.exit(f"Usage: x-draft.py {mode} <url> <raw>")
        url = args[0]
        raw = args[1]
        token = vault_get("X_API_BEARER_TOKEN")
        target = fetch_target_tweet(url, token)
        if not target:
            print(json.dumps({"ok": False, "reason": "target_tweet_unavailable", "user_message": "🔒 Couldn't fetch the target tweet (deleted, private, or API issue). Try again or paste the text directly."}))
            return
    else:
        sys.exit(f"Unknown mode: {mode}")

    prompt = build_prompt(mode, raw, target, style_override)
    log(f"mode={mode} style={style_override or 'default'} prompt_chars={len(prompt)}")

    draft = generate(prompt)
    if not draft:
        print(json.dumps({"ok": False, "reason": "empty_response", "user_message": "⚠️ Draft generator returned empty. Try again."}))
        return

    draft, fits = enforce_length(draft, prompt)

    pending = {
        "draft": draft,
        "mode": mode,
        "raw": raw,
        "style_override": style_override,
        "target": target,
        "char_count": len(draft),
        "char_limit_ok": fits,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(pending, indent=2))

    user_msg = f"📝 Draft ({mode}, {len(draft)} chars):\n\n{draft}\n\n"
    if not fits:
        user_msg += "⚠️ Over 280 chars — tighten manually if you post.\n\n"
    user_msg += "Reply: `approve` · `edit: <new text>` · `reject` · `try again`"

    print(json.dumps({
        "ok": True,
        "draft": draft,
        "char_count": len(draft),
        "char_limit_ok": fits,
        "mode": mode,
        "style_override": style_override,
        "user_message": user_msg,
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {e}")
        print(json.dumps({"ok": False, "reason": "error", "error": str(e), "user_message": "⚠️ Drafting failed. See logs."}))
        sys.exit(1)
