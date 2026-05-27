#!/usr/bin/env python3
# Fitness snapshot from habits/activity-tracker brain page.
#
# Reads:
#   - Total sessions YTD
#   - Target (defaults to 200 if not parseable)
#   - Category breakdown (Tennis / Gym / Running / Others)
#
# Computes:
#   - % completed
#   - Pace status: ahead/on/behind target pace, in sessions
#   - This week sessions (from activities.tsv if available)
#
# Output: bullet-formatted markdown for the morning briefing.

import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ["PATH"] = "${STATE_HOME}/.bun/bin:" + os.environ.get("PATH", "")

GBRAIN = "${STATE_HOME}/.bun/bin/gbrain"
ACTIVITIES_TSV = Path.home() / ".brain-bootstrap-raw" / "state" / "activity-tracker" / "activities.tsv"

DEFAULT_TARGET = 200
TODAY = date.today()
START_OF_YEAR = date(TODAY.year, 1, 1)
DAYS_IN_YEAR = 366 if TODAY.year % 4 == 0 and (TODAY.year % 100 != 0 or TODAY.year % 400 == 0) else 365


def log(msg):
    print(f"[fitness] {msg}", file=sys.stderr)


def get_brain_page():
    r = subprocess.run([GBRAIN, "get", "habits/activity-tracker"], capture_output=True, text=True)
    if r.returncode != 0:
        return ""
    return r.stdout


def parse_brain(text):
    out = {"total": 0, "target": DEFAULT_TARGET, "breakdown": {}}
    m = re.search(r"Total sessions this year:\s*(\d+)", text, re.IGNORECASE)
    if m:
        out["total"] = int(m.group(1))
    m = re.search(r"Target:\s*(\d+)", text, re.IGNORECASE)
    if m:
        out["target"] = int(m.group(1))
    # Breakdown — match lines like "- Tennis: 39"
    for category in ["Tennis", "Gym", "Running", "Others"]:
        m = re.search(rf"-\s*{category}:\s*(\d+)", text, re.IGNORECASE)
        if m:
            out["breakdown"][category] = int(m.group(1))
    return out


def count_this_week():
    """Count sessions in the current ISO week (Monday → Sunday inclusive)."""
    if not ACTIVITIES_TSV.exists():
        return None
    # Monday of the current week. weekday(): 0 = Monday, 6 = Sunday.
    start_of_week = TODAY - timedelta(days=TODAY.weekday())
    count = 0
    with ACTIVITIES_TSV.open() as f:
        next(f, None)  # header
        for line in f:
            parts = line.split("\t")
            if not parts:
                continue
            for tok in parts[:3]:
                m = re.match(r"(\d{4}-\d{2}-\d{2})", tok.strip())
                if m:
                    try:
                        d = datetime.fromisoformat(m.group(1)).date()
                        if d >= start_of_week:
                            count += 1
                    except ValueError:
                        pass
                    break
    return count


def pace_message(total, target):
    days_elapsed = (TODAY - START_OF_YEAR).days + 1
    expected = target * days_elapsed / DAYS_IN_YEAR
    diff = total - expected
    pct = total / target * 100
    if diff >= 5:
        return f"On pace: {round(diff)} sessions ahead 🔥", pct
    if diff >= -2:
        return f"On pace: tracking target", pct
    return f"Behind pace: {round(-diff)} sessions short ⚠️", pct


def main():
    text = get_brain_page()
    if not text:
        print("🏃 Fitness — activity tracker unavailable")
        return

    data = parse_brain(text)
    total = data["total"]
    target = data["target"]
    if not target:
        target = DEFAULT_TARGET

    pace, pct = pace_message(total, target)
    week_count = count_this_week()

    lines = [f"🏃 Fitness — {total} / {target} sessions ({pct:.1f}%)"]
    lines.append(f"• {pace}")
    if week_count is not None:
        unit = "session" if week_count == 1 else "sessions"
        lines.append(f"• This week: {week_count} {unit}")
    if data["breakdown"]:
        b = data["breakdown"]
        parts = [f"{k} {v}" for k, v in b.items() if v > 0]
        lines.append(f"• Breakdown: {' / '.join(parts)}")

    print("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {e}")
        print(f"🏃 Fitness — unavailable ({e})")
        sys.exit(1)
