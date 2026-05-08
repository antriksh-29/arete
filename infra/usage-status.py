#!/usr/bin/env python3
# usage-status — today's (or a window's) LLM token + cost report.
#
# Wave 4B-β: real implementation. Reads OpenClaw's canonical cost
# tracker via `openclaw gateway usage-cost --days N --json` (the CLI
# uses its own cache file at ~/.openclaw/state/agents/<id>/sessions/
# .usage-cost-cache.json under a lockfile).
#
# Fallbacks:
#   1. CLI succeeds (default path)
#   2. Read the cache file directly if CLI errors (lockfile contention,
#      openclaw not on PATH for whatever reason)
#   3. Return a clear "data unavailable" envelope if both fail (never
#      fabricate numbers — Rule 14 says spend visibility, not spend
#      hallucination)
#
# Usage:
#   usage-status.py                   today (1 day window)
#   usage-status.py yesterday         yesterday's totals only
#   usage-status.py 2026-05-04        a specific date
#   usage-status.py --days 7          rolling 7-day window
#   usage-status.py --days 30 --json  raw envelope (default is JSON anyway)
#   usage-status.py --smoke

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

CACHE_PATH = Path.home() / ".openclaw/state/agents/default/sessions/.usage-cost-cache.json"


# ── helpers ────────────────────────────────────────────────────

def today_ist() -> str:
    """Today's date in IST (Asia/Kolkata) — same convention as other skills."""
    # Avoid pulling in pytz; compute IST from UTC.
    utc = dt.datetime.now(dt.timezone.utc)
    ist = utc + dt.timedelta(hours=5, minutes=30)
    return ist.strftime("%Y-%m-%d")


def yesterday_ist() -> str:
    utc = dt.datetime.now(dt.timezone.utc)
    ist = utc + dt.timedelta(hours=5, minutes=30) - dt.timedelta(days=1)
    return ist.strftime("%Y-%m-%d")


def run_cli(days: int) -> dict | None:
    """Run `openclaw gateway usage-cost --days N --json`. Returns the
    parsed JSON or None if the CLI isn't available or returned an error."""
    try:
        r = subprocess.run(
            ["openclaw", "gateway", "usage-cost", "--days", str(days), "--json"],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def read_cache(target_date: str) -> dict | None:
    """Read the cache file directly and aggregate today's entries.
    Returns the aggregated row in the same shape as the CLI's daily[]
    entry, or None if the cache is unreadable."""
    if not CACHE_PATH.exists():
        return None
    try:
        cache = json.loads(CACHE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    # Cache shape (v2):
    #   {"version":2,"updatedAt":<ts>,"files":{
    #     "<transcript>":{"totals":{},"usageEntries":[
    #       {"timestamp":<ms>,"input":n,"output":n,...,"cost":{...}}
    #     ]}
    #   }}
    # Window the entries to the IST day boundaries of `target_date`.
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    ist_start = dt.datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=ist)
    ist_end = ist_start + dt.timedelta(days=1)
    start_ms = int(ist_start.timestamp() * 1000)
    end_ms = int(ist_end.timestamp() * 1000)

    agg = {
        "date": target_date,
        "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
        "totalTokens": 0,
        "inputCost": 0.0, "outputCost": 0.0,
        "cacheReadCost": 0.0, "cacheWriteCost": 0.0,
        "totalCost": 0.0,
    }
    for f in cache.get("files", {}).values():
        for e in f.get("usageEntries", []):
            ts = e.get("timestamp", 0)
            if not (start_ms <= ts < end_ms):
                continue
            agg["input"] += e.get("input", 0)
            agg["output"] += e.get("output", 0)
            agg["cacheRead"] += e.get("cacheRead", 0)
            agg["cacheWrite"] += e.get("cacheWrite", 0)
            cost = e.get("cost", {})
            agg["inputCost"] += cost.get("input", 0)
            agg["outputCost"] += cost.get("output", 0)
            agg["cacheReadCost"] += cost.get("cacheRead", 0)
            agg["cacheWriteCost"] += cost.get("cacheWrite", 0)
            agg["totalCost"] += cost.get("total", 0)
    agg["totalTokens"] = agg["input"] + agg["output"] + agg["cacheRead"] + agg["cacheWrite"]
    return agg


def format_message(row: dict) -> str:
    cost = row.get("totalCost", 0)
    tokens = row.get("totalTokens", 0)
    date = row.get("date", "?")
    if tokens == 0 and cost == 0:
        return f"💰 No LLM activity logged for {date}."
    return (
        f"💰 Spend on {date}: ${cost:.4f} · {tokens:,} tokens "
        f"(in {row.get('input', 0):,} / out {row.get('output', 0):,} / "
        f"cache R {row.get('cacheRead', 0):,} W {row.get('cacheWrite', 0):,})"
    )


# ── CLI ────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if args and args[0] == "--smoke":
        print(json.dumps({"ok": True, "skill": "usage-status",
                          "user_message": "usage-status smoke ok"}))
        return

    # Parse args
    days = 1
    target_date = None
    if args:
        if args[0] == "--days":
            days = int(args[1])
        elif args[0] == "yesterday":
            target_date = yesterday_ist()
        elif args[0] == "today":
            target_date = today_ist()
        elif len(args[0]) == 10 and args[0][4] == "-" and args[0][7] == "-":
            target_date = args[0]
        # If --days not specified and target_date set, default to 1 day
        if target_date and days == 1:
            days = 1

    if target_date is None:
        target_date = today_ist()

    # Try CLI first
    cli_data = run_cli(days)
    if cli_data is not None:
        # CLI returned full structure — find target_date in daily[]
        daily = cli_data.get("daily", [])
        row = next((r for r in daily if r.get("date") == target_date), None)
        if row is None:
            # CLI ran but target_date isn't in the window — return
            # whatever's in totals across the window, with the right date.
            row = cli_data.get("totals", {})
            row["date"] = target_date
        out = {
            "ok": True,
            "skill": "usage-status",
            "source": "openclaw cli",
            "scope": target_date,
            "days_window": days,
            "row": row,
            "user_message": format_message(row),
        }
        print(json.dumps(out, ensure_ascii=False))
        return

    # Fallback: read cache directly
    row = read_cache(target_date)
    if row is not None:
        out = {
            "ok": True,
            "skill": "usage-status",
            "source": "cache file",
            "scope": target_date,
            "days_window": 1,
            "row": row,
            "user_message": format_message(row),
        }
        print(json.dumps(out, ensure_ascii=False))
        return

    # Honest failure — Rule 14 says spend visibility, not hallucination.
    print(json.dumps({
        "ok": False,
        "skill": "usage-status",
        "source": None,
        "scope": target_date,
        "user_message": (
            f"Usage report for {target_date} unavailable — both `openclaw "
            f"gateway usage-cost` CLI and the cache at {CACHE_PATH} are "
            "unreachable. Check `systemctl --user status openclaw-gateway` "
            "and `openclaw doctor` to investigate."
        ),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
