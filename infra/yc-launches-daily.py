#!/usr/bin/env python3
"""yc-launches-daily.py — deterministic dispatcher for the
yc-launches-daily-briefings skill (formerly named
help-me-get-a-daily-report-on-all-the-to, renamed 2026-05-24).

REPLACES the agentTurn cron path. That path was failing with
"cron: isolated agent setup timed out before runner start" every
~24h because OpenClaw's isolated-agent cold start (~60-85s) kept
racing the cron setup timeout (May 22 18:30 errored at 74s,
May 23 18:30 errored at 85s). Even successful runs landed at 61-78s,
right at the edge.

There's no LLM judgment in this skill — fetch YC launches, filter
to last 24h + latest batch, format as bullets, send to Telegram.
A direct Python script removes the cold-start race entirely.

Source: https://www.ycombinator.com/launches returns clean JSON in
the response body (each entry has title, tagline, slug, created_at,
company.batch, search_path).

Usage:
  yc-launches-daily.py            # send today's launches
  yc-launches-daily.py --dry-run  # print formatted output, don't send
"""

from __future__ import annotations
import argparse
import datetime as dt
import html
import json
import subprocess
import sys
import urllib.request
from pathlib import Path


YC_LAUNCHES_URL = "https://www.ycombinator.com/launches"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) yc-launches-daily/alfred"
SEND_TG = Path.home() / "bin" / "send-telegram.sh"
LOOKBACK_HOURS = 24
MAX_BULLETS = 15  # safety: don't spam if YC drops 30 launches in a day


def fetch_launches() -> list[dict]:
    # YC's /launches endpoint returns HTML to default urllib requests
    # (no Accept header → server picks text/html) and JSON to clients
    # that explicitly accept JSON. Pin the header so we always get the
    # API shape regardless of urllib's defaults across Python versions.
    req = urllib.request.Request(
        YC_LAUNCHES_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"YC launches did not return JSON (got {body[:120]!r}…). "
            f"Endpoint shape may have changed. {e}"
        )
    if "hits" not in data:
        raise SystemExit("YC launches response missing 'hits' key — schema changed")
    return data["hits"]


def latest_batch(hits: list[dict]) -> str | None:
    # Pick the batch label most common among recent launches. The skill
    # spec says "new companies coming out of the latest batch", so we
    # need to figure out which batch is current rather than hardcoding it.
    counts: dict[str, int] = {}
    for h in hits[:30]:
        batch = (h.get("company") or {}).get("batch")
        if batch:
            counts[batch] = counts.get(batch, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def filter_recent(hits: list[dict], hours: int, batch: str | None) -> list[dict]:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    kept = []
    for h in hits:
        try:
            ts = dt.datetime.fromisoformat(h["created_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        if ts < cutoff:
            continue
        if batch and (h.get("company") or {}).get("batch") != batch:
            continue
        kept.append(h)
    return kept


def format_bullet(hit: dict) -> str:
    # The skill spec wants: bullet + one-line description + YC post link.
    # The hit "title" is usually "CompanyName - tagline" or just the
    # company name; the tagline field is the one-line description.
    raw_title = (hit.get("title") or "").strip()
    tagline = (hit.get("tagline") or "").strip()
    url = hit.get("search_path") or ""
    company = ((hit.get("company") or {}).get("name") or "").strip()

    # Prefer a concise "Company — tagline" shape. If raw_title already
    # contains a dash, the tagline portion is in the title; use it.
    name = company or raw_title.split(" - ", 1)[0]
    desc = tagline or (raw_title.split(" - ", 1)[1] if " - " in raw_title else raw_title)

    # Telegram HTML — escape user-supplied text, bold the company name.
    name_esc = html.escape(name)
    desc_esc = html.escape(desc)
    if url:
        return f'• <b>{name_esc}</b> — {desc_esc}\n  <a href="{html.escape(url)}">{html.escape(url)}</a>'
    return f"• <b>{name_esc}</b> — {desc_esc}"


def format_message(hits: list[dict], batch: str | None) -> str:
    today = dt.datetime.now().strftime("%b %-d, %Y")
    if not hits:
        return (
            f"<b>YC Daily Launches — {html.escape(today)}</b>\n\n"
            f"No new launches in the last {LOOKBACK_HOURS} hours"
            + (f" from batch {html.escape(batch)}." if batch else ".")
        )
    header = f"<b>YC Daily Launches — {html.escape(today)}</b>"
    if batch:
        header += f"\n<i>Latest batch: {html.escape(batch)}</i>"
    bullets = "\n\n".join(format_bullet(h) for h in hits[:MAX_BULLETS])
    return f"{header}\n\n{bullets}"


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="format + print, don't send")
    ap.add_argument("--hours", type=int, default=LOOKBACK_HOURS,
                    help=f"lookback window in hours (default {LOOKBACK_HOURS})")
    ap.add_argument("--batch", default=None,
                    help="override batch filter (default: auto-detect latest)")
    ap.add_argument("--no-batch-filter", action="store_true",
                    help="include launches from all batches")
    args = ap.parse_args()

    hits = fetch_launches()
    batch = None if args.no_batch_filter else (args.batch or latest_batch(hits))
    recent = filter_recent(hits, args.hours, batch)
    msg = format_message(recent, batch)

    if args.dry_run:
        print("--- would send ---")
        print(msg)
        print(f"\n--- {len(recent)} launches in last {args.hours}h"
              + (f", batch={batch}" if batch else "") + " ---")
        return 0

    return send(msg)


if __name__ == "__main__":
    sys.exit(main())
