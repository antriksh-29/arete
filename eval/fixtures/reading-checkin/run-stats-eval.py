#!/usr/bin/env python3
# run-stats-eval.py — verifies reading-checkin's stats math is correct
# against a deterministic 9-day fixture. Independent of the live brain
# repo; doesn't touch the real ~/.brain/.
#
# Reproduces recompute_stats logic from infra/reading-checkin.sh and
# asserts the per-window counts match what we expect for a fixed
# (date_anchor, fixture) pair.
#
# Usage:
#   python3 eval/fixtures/reading-checkin/run-stats-eval.py
#   python3 ... --json     (machine-readable verdict)

import datetime as dt
import json
import re
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "sample-9-days.md"
START = "2026-05-01"
# Anchor "today" deterministically so the eval doesn't drift with the
# system clock. The fixture has entries through 2026-05-09; we evaluate
# as if today were 2026-05-10 (yesterday IST = 2026-05-09).
ANCHOR_TODAY = dt.date(2026, 5, 10)

EXPECTED = {
    "started": "2026-05-01",
    "last7": (4, 7, 57),       # 5/3 → 5/9 → 7 entries, 4 yes
    "this_month": (4, 9, 44),  # all of May so far
    "overall": (4, 9, 44),     # same as month here
}


def stats_from_page(src: str, today: dt.date, start_str: str) -> dict:
    """Independent reimplementation of recompute_stats. Pure function;
    no file IO, no global state. Same algorithm as the shell script's
    embedded Python — if these two ever drift, the eval fails."""
    log_rows = re.findall(r"\| (\d{4}-\d{2}-\d{2}) \| (yes|no) \|", src)
    entries = {d: v for d, v in log_rows}
    entries_dates = {dt.date.fromisoformat(d) for d in entries}
    yesterday = today - dt.timedelta(days=1)
    start = dt.date.fromisoformat(start_str)

    last7 = [
        (d, entries[d.isoformat()])
        for d in entries_dates
        if (today - d).days <= 7 and d <= yesterday
    ]
    month_first = today.replace(day=1)
    this_month = [
        (d, entries[d.isoformat()])
        for d in entries_dates
        if d >= month_first and d <= yesterday
    ]
    overall = [
        (d, entries[d.isoformat()])
        for d in entries_dates
        if d >= start and d <= yesterday
    ]

    def pack(window):
        yes = sum(1 for _, v in window if v == "yes")
        total = len(window)
        pct = round(100 * yes / total) if total else 0
        return (yes, total, pct)

    return {
        "started": start_str,
        "last7": pack(last7),
        "this_month": pack(this_month),
        "overall": pack(overall),
    }


def main():
    out_json = "--json" in sys.argv
    src = FIXTURE.read_text()
    actual = stats_from_page(src, ANCHOR_TODAY, START)

    failures = []
    for k, v_expected in EXPECTED.items():
        v_actual = actual[k]
        if v_actual != v_expected:
            failures.append(f"{k}: expected {v_expected}, got {v_actual}")

    if out_json:
        print(json.dumps({
            "ok": len(failures) == 0,
            "skill": "reading-checkin",
            "fixture": str(FIXTURE.relative_to(HERE.parent.parent.parent)),
            "anchor_today": ANCHOR_TODAY.isoformat(),
            "expected": EXPECTED,
            "actual": actual,
            "failures": failures,
            "user_message": (
                "stats-math eval ok"
                if not failures
                else f"stats-math eval FAILED: {len(failures)} mismatch(es)"
            ),
        }, indent=2, default=str))
    else:
        print(f"fixture: {FIXTURE.name}  anchor: {ANCHOR_TODAY.isoformat()}")
        for k, v_expected in EXPECTED.items():
            v_actual = actual[k]
            tag = "✓" if v_actual == v_expected else "✗"
            print(f"  {tag} {k:11s}  expected={v_expected}  actual={v_actual}")
        print()
        if failures:
            print(f"FAIL — {len(failures)} mismatch(es)")
        else:
            print("OK — all stats-math assertions pass")

    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
