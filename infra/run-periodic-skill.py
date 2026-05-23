#!/usr/bin/env python3
"""run-periodic-skill.py — deterministic dispatcher for periodic user-skills.

Reads `~/.openclaw/workspace/skills/<slug>/SKILL.md` (or the repo path
as fallback), finds the numbered content pool ("## Insight pool",
"## Fact pool", etc.), picks one item at random (avoiding the most
recent N to reduce repeats), formats it nicely, and posts to Telegram
via `~/bin/send-telegram.sh --html`.

This is the deterministic delivery path. Replaces the OpenClaw cron +
agentTurn approach which kept misrouting periodic fires into the
skill-author elicit pipeline (because the agentTurn message contained
the skill slug, Kimi interpreted it as a refine intent and asked
"Keep the existing trigger phrases, or want to add new ones?" instead
of producing content).

Pattern matches `heartbeat-morning-briefing.sh` — the user already
moved away from agent-based delivery to Python-composed + direct
Telegram send for exactly this kind of reliability concern.

Usage:
  run-periodic-skill.py <slug>           # send one item
  run-periodic-skill.py <slug> --dry-run # parse + format + print, don't send

The skill's SKILL.md must contain a section like:

  ## <Something> pool (<N>)

  1. <body text> [optional " — Author Name, "<Source>" (<year>)"]
  2. <body text>
  ...

The dispatcher picks one item, formats:
  - If a " — Author, "Title" (year)" suffix exists: emit as
    `<b>Title</b>\\n\\n<body>\\n\\n<i>— Author, Title</i>`
  - Otherwise: emit the body as-is

State file at `~/.brain-bootstrap-raw/state/<slug>.json` tracks the
last 5 emitted indices. The dispatcher avoids those when possible so
the user doesn't see the same insight twice in a week.
"""

from __future__ import annotations
import html
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path


WORKSPACE_SKILL_DIR = Path.home() / ".openclaw" / "workspace" / "skills"
REPO_USER_SKILL_DIR = Path.home() / "personal-assistant" / "user-skills"
STATE_DIR = Path.home() / ".brain-bootstrap-raw" / "state"
SEND_TG = Path.home() / "bin" / "send-telegram.sh"
RECENT_WINDOW = 5  # avoid the last N indices when possible


def find_skill_md(slug: str) -> Path | None:
    for base in (WORKSPACE_SKILL_DIR, REPO_USER_SKILL_DIR):
        p = base / slug / "SKILL.md"
        if p.is_file():
            return p
    return None


def parse_pool(text: str) -> list[str]:
    # Find the first "## <words> pool" section heading (case-insensitive).
    m = re.search(r"^##\s+[A-Za-z][\w\s]*\bpool\b[^\n]*$", text, re.M | re.I)
    if not m:
        return []
    start = m.end()
    # The section ends at the next "## " heading or end of file.
    next_h = re.search(r"^##\s+", text[start:], re.M)
    end = start + next_h.start() if next_h else len(text)
    pool_text = text[start:end]
    # Parse numbered list lines: "1. body..." (multiline content not supported
    # — each item must be one logical line).
    items = re.findall(r"^\s*\d+\.\s+(.+?)\s*$", pool_text, re.M)
    return items


def split_attribution(item: str) -> tuple[str, str | None, str | None]:
    """Return (body, attribution_full, title). attribution_full is the
    "Author, \"Title\" (year)" portion if present; title is just the
    quoted source title (or None).

    Some items have an em-dash WITHIN the body (e.g. for parenthetical
    clauses), so we must split on the LAST occurrence of " — Author"
    where Author starts with a capital letter — that's the attribution
    boundary. Falling back to the LAST em-dash also handles edge cases
    like "— Author Name, Source" (no quotes / year).
    """
    # Find all em-dash (or "--") separator positions
    seps = [m.start() for m in re.finditer(r"\s+(?:—|--)\s+", item)]
    if not seps:
        return item, None, None
    # Prefer the LAST em-dash split — that's the attribution boundary
    last = seps[-1]
    sep_match = re.match(r"\s+(?:—|--)\s+", item[last:])
    sep_len = sep_match.end() if sep_match else 3
    body = item[:last].strip()
    attribution = item[last + sep_len:].strip()
    # Extract title from attribution if it's "Author, "Title" (year)" shape
    tm = re.search(r'"([^"]+)"', attribution)
    title = tm.group(1) if tm else None
    return body, attribution, title


def load_state(slug: str) -> dict:
    p = STATE_DIR / f"{slug}.json"
    if not p.is_file():
        return {"recent_indices": [], "last_sent_at": 0}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"recent_indices": [], "last_sent_at": 0}


def save_state(slug: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{slug}.json").write_text(json.dumps(state, indent=2))


def pick_index(items: list[str], recent: list[int]) -> int:
    if not items:
        raise SystemExit("pool is empty")
    avoid = set(recent[-RECENT_WINDOW:])
    fresh = [i for i in range(len(items)) if i not in avoid]
    if not fresh:
        fresh = list(range(len(items)))
    return random.choice(fresh)


def reply_footer(slug: str) -> str:
    # Visible (small italic) footer appended to every periodic-skill
    # Telegram message. Two jobs:
    #   1. Tell the user this came from a specific skill — and that
    #      replying continues the conversation with it.
    #   2. Stamp the message body with a machine-readable slug so that
    #      when Telegram quotes it in the user's reply, the agent can
    #      see which skill the reply belongs to.
    # The "/<slug>" form mirrors how the user invokes skills manually.
    return f'\n\n<i>↩ reply to keep talking to /{html.escape(slug)}</i>'


def write_last_periodic_state(slug: str, msg: str) -> None:
    # Deterministic backup for intent-router.sh. If the LLM misses the
    # tag (e.g. OpenClaw doesn't render reply-quoted bodies into the
    # turn), the router can still see "the last cron fire was N seconds
    # ago for skill X" and route the user's next non-classified message
    # to that skill. One-shot: router deletes this file after consuming.
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path = STATE_DIR / "last-periodic-skill.json"
    state_path.write_text(json.dumps({
        "slug": slug,
        "sent_at": int(time.time()),
        "message_preview": msg[:240],
    }, indent=2))


def format_message(item: str) -> str:
    """Produce Telegram-HTML formatted output. Caller passes --html to
    send-telegram.sh.

    With attribution + title:
      <b>Title</b>

      Body text

      <i>— Author, "Title" (year)</i>

    Without attribution: just the body, escaped.
    """
    body, attribution, title = split_attribution(item)
    body_esc = html.escape(body)
    if title and attribution:
        title_esc = html.escape(title)
        attr_esc = html.escape(attribution)
        return f"<b>{title_esc}</b>\n\n{body_esc}\n\n<i>— {attr_esc}</i>"
    if attribution:
        attr_esc = html.escape(attribution)
        return f"{body_esc}\n\n<i>— {attr_esc}</i>"
    return body_esc


def send(msg: str) -> int:
    if not SEND_TG.is_file():
        print(f"send-telegram.sh missing at {SEND_TG}", file=sys.stderr)
        return 1
    try:
        subprocess.run([str(SEND_TG), "--html", msg], check=True, timeout=20)
        return 0
    except subprocess.CalledProcessError as e:
        print(f"send-telegram failed: rc={e.returncode}", file=sys.stderr)
        return e.returncode or 1
    except subprocess.TimeoutExpired:
        print("send-telegram timed out", file=sys.stderr)
        return 124


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: run-periodic-skill.py <slug> [--dry-run]", file=sys.stderr)
        return 2
    slug = argv[1]
    dry = "--dry-run" in argv[2:]

    skill_md = find_skill_md(slug)
    if skill_md is None:
        print(f"SKILL.md not found for slug={slug}", file=sys.stderr)
        return 1
    text = skill_md.read_text()
    items = parse_pool(text)
    if not items:
        print(f"no '## ... pool' section found in {skill_md}", file=sys.stderr)
        return 1

    state = load_state(slug)
    idx = pick_index(items, state.get("recent_indices", []))
    msg = format_message(items[idx]) + reply_footer(slug)

    if dry:
        print(f"--- would send (item {idx + 1}/{len(items)}) ---")
        print(msg)
        return 0

    rc = send(msg)
    if rc == 0:
        state.setdefault("recent_indices", []).append(idx)
        state["recent_indices"] = state["recent_indices"][-10:]
        state["last_sent_at"] = int(time.time())
        state["last_index"] = idx
        save_state(slug, state)
        write_last_periodic_state(slug, msg)
        print(f"sent {slug} item {idx + 1}/{len(items)}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
