#!/usr/bin/env python3
# brain-find.py — resolve a free-text reference to a brain page slug.
#
# Why this exists: the post-#82 Telethon probe showed that when the
# user asked "what are Nishank's top 3 takeaways", the agent fell back
# to the `last` pointer (per AGENTS.md) which had drifted to a
# qoves.com glow-up page. The agent then quoted from the wrong brain
# page with full confidence. brain-find replaces `last`-as-default
# with an actual lookup: take the user's words, find the closest
# matching brain page, return its slug.
#
# Resolution order:
#   1. Lexical match across lists/to-read.md rows — handle, posted_by,
#      title, URL host. Fast, deterministic, no LLM.
#   2. Fallback: `gbrain search "<query>"` for a semantic match. Slower,
#      requires the gbrain index to be fresh.
#
# Usage:
#   brain-find.py "Nishank's article"
#   brain-find.py "the Tobi tweet"
#   brain-find.py "QOVES glow-up"
#
# Prints a single-line JSON envelope on stdout:
#   {"ok": true,
#    "best": "media/social/x-nishank-20260512",
#    "confident": true,                       # true when top score >> 2nd
#    "candidates": [{"slug": ..., "score": 0.95, "title": ...,
#                    "posted_by": ..., "url": ..., "reason": "..."}, ...],
#    "user_message": "(diagnostic — agent usually doesn't relay this)"}
#
# best == "" + ok=false reason=no_match  when nothing scores above the floor.

import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

BRAIN_DIR = Path.home() / ".brain"
LIST_PATH = BRAIN_DIR / "lists" / "to-read.md"
GBRAIN = "${STATE_HOME}/.bun/bin/gbrain"

os.environ["PATH"] = "${STATE_HOME}/.bun/bin:" + os.environ.get("PATH", "")


def log(msg):
    print(f"[brain-find] {msg}", file=sys.stderr)


V2_QUEUE_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*$"
)
V2_READ_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*$"
)

# Stop words and decorations the user always says but that shouldn't
# match anything in the row data.
STOP = {
    "the", "a", "an", "what", "does", "do", "did", "say", "says", "is", "are",
    "was", "were", "about", "from", "by", "of", "in", "on", "for", "with",
    "to", "at", "and", "or", "but", "this", "that", "those", "these", "his",
    "her", "their", "my", "our", "your", "article", "post", "tweet", "thread",
    "page", "piece", "essay", "video", "blog", "summary", "summarize",
    "explain", "quote", "quotes", "takeaway", "takeaways", "top", "main",
    "key", "core", "points", "directly", "please", "please?", "show", "tell",
    "give", "me", "us", "him", "her", "them", "it", "what's", "whats",
    # Numbers as words (user says "top 3")
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "first", "second", "third",
}


def _strip(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _tokenize_query(q):
    """Return the meaningful tokens from the user's free-text. We use
    these as match candidates: an author/title/handle that overlaps a
    token gets scored. Numbers, stop words, and punctuation are removed."""
    # Pull @handles whole so we keep "Nishank" tokenizable but
    # @nishanksingla stays as "nishanksingla"
    handles = [m.group(1).lower() for m in re.finditer(r"@(\w+)", q or "")]
    cleaned = re.sub(r"[^\w\s@]", " ", (q or "").lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    tokens = []
    for raw in cleaned.split():
        if raw.startswith("@"):
            tokens.append(raw[1:])
        elif raw not in STOP and len(raw) >= 2:
            tokens.append(raw)
    return tokens + handles


def _normalize_url(url):
    p = urllib.parse.urlsplit((url or "").strip())
    drop = {"s", "t", "si", "utm_source", "utm_medium", "utm_campaign",
            "utm_term", "utm_content", "ref", "ref_src", "feature",
            "fbclid", "gclid"}
    qs = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if k.lower() not in drop]
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(),
                                     p.path.rstrip("/") or p.path,
                                     urllib.parse.urlencode(qs), ""))


def _load_rows():
    """Iterate every queue + read row from lists/to-read.md as
    dicts with the v2 fields we care about for matching."""
    if not LIST_PATH.exists():
        return []
    rows = []
    section = None
    for line in LIST_PATH.read_text().splitlines():
        ls = line.strip().lower()
        if ls.startswith("## queue") or ls == "## to read":
            section = "queue"
            continue
        if ls.startswith("## recently read") or ls == "## read":
            section = "read"
            continue
        if ls.startswith("## "):
            section = None
            continue
        if section not in ("queue", "read"):
            continue
        if "| title " in line.lower() or line.strip().startswith("|---"):
            continue
        if section == "read":
            m = V2_READ_RE.match(line)
            if m:
                rows.append({
                    "title": m.group(1).strip(), "posted_by": m.group(2).strip(),
                    "one_liner": m.group(3).strip(), "kind": m.group(4).strip(),
                    "url": m.group(5).strip(), "added": m.group(6).strip(),
                    "read": m.group(7).strip(),
                    "brain_page": m.group(8).strip().strip("[]").strip(),
                    "bucket": "read",
                })
                continue
        m = V2_QUEUE_RE.match(line)
        if m:
            rows.append({
                "title": m.group(1).strip(), "posted_by": m.group(2).strip(),
                "one_liner": m.group(3).strip(), "kind": m.group(4).strip(),
                "url": m.group(5).strip(), "added": m.group(6).strip(),
                "brain_page": m.group(7).strip().strip("[]").strip(),
                "bucket": section,
            })
    return rows


def _score(tokens, row):
    """Cheap deterministic match scoring. Higher = better. Returns
    (score, reason_string). Lots of small signals add up:

      * Exact match on a token equal to the row's handle → +1.0
      * Token substring inside posted_by (case-insensitive) → +0.7
      * Token substring inside title → +0.5
      * Token substring inside URL host → +0.4
      * Token substring inside slug → +0.4

    A row scoring < 0.4 isn't returned.

    We're explicit about the reason in the envelope so the agent can
    surface "I picked X because the handle matched 'nishanksingla'"
    if the user pushes back."""
    if not tokens:
        return 0.0, ""
    score = 0.0
    reasons = []
    posted_by = _strip(row.get("posted_by", ""))
    title = _strip(row.get("title", ""))
    url = (row.get("url") or "").lower()
    slug = (row.get("brain_page") or "").lower()
    host = ""
    try:
        host = urllib.parse.urlsplit(url).netloc.replace("www.", "")
    except Exception:
        pass
    # Extract @handle from posted_by if present — "Tobi Lutke (@tobi)"
    handle_m = re.search(r"@(\w+)", row.get("posted_by", ""))
    handle = handle_m.group(1).lower() if handle_m else ""

    for tok in tokens:
        if handle and tok == handle:
            score += 1.0
            reasons.append(f"handle=={tok}")
            continue
        if handle and tok in handle:
            score += 0.85
            reasons.append(f"handle~{tok}")
            continue
        if posted_by and tok in posted_by:
            score += 0.7
            reasons.append(f"posted_by~{tok}")
            continue
        if title and tok in title:
            score += 0.5
            reasons.append(f"title~{tok}")
            continue
        if host and tok in host:
            score += 0.4
            reasons.append(f"host~{tok}")
            continue
        if slug and tok in slug:
            score += 0.4
            reasons.append(f"slug~{tok}")
            continue
    return score, ",".join(reasons)


def _try_gbrain_search(query):
    """Fallback semantic search via gbrain. Returns a list of slug
    strings — best-effort, empty on failure (no panic)."""
    try:
        r = subprocess.run(
            [GBRAIN, "search", query, "--limit", "5"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    slugs = []
    for line in r.stdout.splitlines():
        # gbrain search prints slugs on their own line; pick anything
        # that looks like a slug (has a '/')
        m = re.match(r"^\s*([a-z][\w./-]*\/[\w./-]+)\s*", line)
        if m:
            slugs.append(m.group(1).rstrip("."))
    return slugs


def find(query):
    """Public entry point. Returns the dict the CLI prints."""
    query_norm = _strip(query)
    tokens = _tokenize_query(query)
    rows = _load_rows()

    # ---- Tier 1: lexical match against lists/to-read ----
    scored = []
    for row in rows:
        if not row.get("brain_page"):
            continue
        s, reason = _score(tokens, row)
        if s >= 0.4:
            scored.append({
                "slug": row["brain_page"],
                "score": round(s, 3),
                "title": row.get("title", ""),
                "posted_by": row.get("posted_by", ""),
                "url": row.get("url", ""),
                "reason": reason,
                "tier": "lexical",
            })
    scored.sort(key=lambda c: c["score"], reverse=True)

    # ---- Tier 2: semantic fallback if lexical was empty ----
    if not scored and tokens:
        log(f"no lexical match; trying gbrain search for {tokens!r}")
        for slug in _try_gbrain_search(query):
            # Best-effort enrich with the row data if we have it
            row = next((r for r in rows if r.get("brain_page") == slug), {})
            scored.append({
                "slug": slug,
                "score": 0.5,
                "title": row.get("title", ""),
                "posted_by": row.get("posted_by", ""),
                "url": row.get("url", ""),
                "reason": "gbrain_search",
                "tier": "semantic",
            })

    if not scored:
        return {
            "ok": False, "reason": "no_match",
            "query": query, "candidates": [],
            "user_message": (
                f"🧠 I don't see a brain page that matches '{query}'. "
                f"Try `show bookmarks` to see what's saved, or paste the URL."
            ),
        }

    best = scored[0]
    # Confidence: top score is at least 1.5x second-place, OR no second place.
    confident = (len(scored) == 1) or (best["score"] >= 1.5 * scored[1]["score"])
    return {
        "ok": True,
        "best": best["slug"],
        "confident": confident,
        "candidates": scored[:3],
        "query": query,
        "user_message": (
            f"📌 Top match: {best['posted_by']} — {best['title'][:80]}\n"
            f"   {best['slug']}"
        ),
    }


def main():
    if len(sys.argv) < 2:
        sys.exit('Usage: brain-find.py "<free-text query>"')
    q = sys.argv[1]
    if q == "--smoke":
        print(json.dumps({"ok": True, "skill": "brain-find", "user_message": "brain-find smoke ok"}))
        return
    print(json.dumps(find(q)))


if __name__ == "__main__":
    main()
