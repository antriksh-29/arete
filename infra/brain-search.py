#!/usr/bin/env python3
# brain-search.py — discovery primitive for Layer 3.
#
# "what's in my brain about ambition?" / "find me everything on
# founder-mode" / "what was I reading about persistence last week"
# — these are pure retrieval, no LLM call needed. gbrain already has
# search; this wrapper presents results in the shape Telegram replies
# want: ranked list with title, posted_by, one_liner, slug.
#
# Two retrieval modes:
#   1. semantic — `gbrain search "<query>"` against the embedding index.
#      Best for conceptual / topic queries.
#   2. lexical — substring scan of lists/to-read + on-disk brain pages.
#      Useful when gbrain index is cold / new pages haven't been
#      indexed yet.
#
# Usage:
#   brain-search.py "ambition"
#   brain-search.py "founder mode" --limit 5
#   brain-search.py "persistence" --kind note   (filter by type)
#   brain-search.py "persistence" --since 2026-05-01
#
# Prints a single-line JSON envelope on stdout:
#   {"ok": true, "query": "ambition",
#    "results": [
#       {"slug": "media/social/x-nishank-20260512",
#        "title": "ambition, survivorship bias, persistence",
#        "posted_by": "Nishank Singla (@nishanksingla)",
#        "one_liner": "Founder-mode self-portrait",
#        "kind": "x_thread", "added": "2026-05-12", "score": 1.0,
#        "matched_via": "title"},
#       ...
#    ],
#    "user_message": "🔎 ..."}

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import date
from pathlib import Path

BRAIN_DIR = Path.home() / ".brain"
LIST_PATH = BRAIN_DIR / "lists" / "to-read.md"
NOTES_DIR = BRAIN_DIR / "notes"
GBRAIN = "${STATE_HOME}/.bun/bin/gbrain"
TODAY = date.today().isoformat()

os.environ["PATH"] = "${STATE_HOME}/.bun/bin:" + os.environ.get("PATH", "")


def log(msg):
    print(f"[brain-search] {msg}", file=sys.stderr)


V2_QUEUE_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*$"
)
V2_READ_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*$"
)
STOP = {"the", "a", "an", "what", "do", "is", "are", "about", "from",
        "of", "in", "on", "for", "with", "to", "and", "or", "but",
        "find", "show", "me", "us", "everything", "anything", "things",
        "stuff", "all", "any"}


def _tokenize(q):
    cleaned = re.sub(r"[^\w\s@]", " ", (q or "").lower())
    return [w for w in cleaned.split() if w and w not in STOP and len(w) >= 2]


def _load_bookmark_rows():
    if not LIST_PATH.exists():
        return []
    rows = []
    section = None
    for line in LIST_PATH.read_text().splitlines():
        ls = line.strip().lower()
        if ls.startswith("## queue") or ls == "## to read":
            section = "queue"; continue
        if ls.startswith("## recently read") or ls == "## read":
            section = "read"; continue
        if ls.startswith("## "):
            section = None; continue
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
                    "slug": m.group(8).strip().strip("[]").strip(),
                    "bucket": "read",
                })
                continue
        m = V2_QUEUE_RE.match(line)
        if m:
            rows.append({
                "title": m.group(1).strip(), "posted_by": m.group(2).strip(),
                "one_liner": m.group(3).strip(), "kind": m.group(4).strip(),
                "url": m.group(5).strip(), "added": m.group(6).strip(),
                "slug": m.group(7).strip().strip("[]").strip(),
                "bucket": section,
            })
    return rows


_NOTE_TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_NOTE_CREATED_RE = re.compile(r"^created:\s*(\d{4}-\d{2}-\d{2})", re.MULTILINE)


def _load_note_rows():
    """Notes don't live in lists/to-read.md — they're standalone brain
    pages under notes/. Scan the directory directly so brain-search
    finds the user's own thoughts the same way it finds bookmarks."""
    out = []
    if not NOTES_DIR.is_dir():
        return out
    for path in sorted(NOTES_DIR.glob("*.md")):
        body = path.read_text()
        title_m = _NOTE_TITLE_RE.search(body)
        created_m = _NOTE_CREATED_RE.search(body)
        out.append({
            "title": title_m.group(1).strip() if title_m else path.stem,
            "posted_by": " (note)",
            "one_liner": "",
            "kind": "note",
            "url": "",
            "added": created_m.group(1) if created_m else "",
            "slug": f"notes/{path.stem}",
            "bucket": "note",
            "_body": body,
        })
    return out


def _score_row(tokens, row, body_text=""):
    """Lexical match scoring. Higher = better."""
    if not tokens:
        return 0.0, ""
    score = 0.0
    reasons = []
    title = (row.get("title") or "").lower()
    posted_by = (row.get("posted_by") or "").lower()
    one_liner = (row.get("one_liner") or "").lower()
    body = (body_text or "").lower()
    slug = (row.get("slug") or "").lower()
    for tok in tokens:
        if tok in title:
            score += 1.0; reasons.append(f"title~{tok}"); continue
        if tok in posted_by:
            score += 0.7; reasons.append(f"posted_by~{tok}"); continue
        if tok in one_liner:
            score += 0.6; reasons.append(f"one_liner~{tok}"); continue
        if tok in slug:
            score += 0.5; reasons.append(f"slug~{tok}"); continue
        if body and tok in body:
            score += 0.4; reasons.append(f"body~{tok}"); continue
    return score, ",".join(reasons)


def _try_gbrain_search(query, limit):
    """gbrain search returns a ranked list. The CLI format is
    line-per-result with the slug visible; semantic score isn't
    exposed reliably so we normalize to 0.5 + position bonus."""
    try:
        r = subprocess.run(
            [GBRAIN, "search", query, "--limit", str(limit)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    slugs = []
    for line in r.stdout.splitlines():
        m = re.match(r"^\s*([a-z][\w.-]*\/[\w./-]+)\s*", line)
        if m:
            slugs.append(m.group(1).rstrip("."))
        if len(slugs) >= limit:
            break
    return slugs


def search(query, limit=5, kind_filter=None, since=None):
    tokens = _tokenize(query)
    bookmarks = _load_bookmark_rows()
    notes = _load_note_rows()
    all_rows = bookmarks + notes

    if kind_filter:
        all_rows = [r for r in all_rows if r.get("kind") == kind_filter]
    if since:
        all_rows = [r for r in all_rows if r.get("added", "") >= since]

    scored = []
    for row in all_rows:
        body_for_note = row.pop("_body", "") if row.get("kind") == "note" else ""
        s, reason = _score_row(tokens, row, body_text=body_for_note)
        if s >= 0.4:
            scored.append({**row, "score": round(s, 3), "matched_via": reason, "tier": "lexical"})
    scored.sort(key=lambda r: r["score"], reverse=True)

    if not scored:
        # Semantic fallback — top N slugs from gbrain search, looked
        # back up in the row data for full metadata.
        log(f"no lexical match; trying gbrain search for {query!r}")
        slug_set = _try_gbrain_search(query, limit)
        for i, slug in enumerate(slug_set):
            row = next((r for r in all_rows if r.get("slug") == slug), None)
            if row:
                scored.append({**row, "score": round(0.5 - i * 0.05, 3),
                               "matched_via": "gbrain_search", "tier": "semantic"})
            else:
                scored.append({"slug": slug, "title": "", "posted_by": "",
                               "one_liner": "", "kind": "", "url": "",
                               "added": "", "score": round(0.5 - i * 0.05, 3),
                               "matched_via": "gbrain_search", "tier": "semantic"})

    results = scored[:limit]

    if not results:
        return {
            "ok": False, "reason": "no_match", "query": query, "results": [],
            "user_message": (
                f"🔎 Nothing in your brain matches '{query}'. "
                f"Try `show bookmarks` for a full list, or add something with "
                f"`save this <url>` / `take a note: <text>`."
            ),
        }

    # Telegram-friendly user_message — numbered list with title +
    # posted_by + slug. Caller can deep-dive any of them with brain-ask.
    lines = [f"🔎 Found {len(results)} match{'es' if len(results) != 1 else ''} for '{query}':", ""]
    for i, r in enumerate(results, 1):
        title = r.get("title") or r.get("slug")
        lines.append(f"{i}. {title}")
        meta = []
        if r.get("posted_by"):
            meta.append(f"Posted by: {r['posted_by']}")
        if r.get("one_liner"):
            meta.append(r["one_liner"][:120])
        for m in meta:
            lines.append(f"   {m}")
        lines.append(f"   📎 {r['slug']}")
        lines.append("")
    lines.append("Ask 'quote from <#N>' / 'what does <#N> say about X' to dive in.")
    return {
        "ok": True, "query": query, "results": results,
        "user_message": "\n".join(lines),
    }


def main():
    ap = argparse.ArgumentParser(description="Search the brain by topic / keyword.")
    ap.add_argument("query", nargs="?", help="Free-text search query.")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--kind", help="Filter by row kind (x_thread, html, note, ...).")
    ap.add_argument("--since", help="Filter by added date >= YYYY-MM-DD.")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()

    if args.smoke:
        print(json.dumps({"ok": True, "skill": "brain-search",
                          "user_message": "brain-search smoke ok"}))
        return
    if not args.query:
        sys.exit('Usage: brain-search.py "<query>" [--limit N] [--kind K] [--since YYYY-MM-DD]')
    print(json.dumps(search(args.query, limit=args.limit,
                             kind_filter=args.kind, since=args.since)))


if __name__ == "__main__":
    main()
