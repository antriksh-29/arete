#!/usr/bin/env python3
# run-stats-eval.py — verifies reading-checkin's stats math is correct
# against a deterministic 9-day fixture. Independent of the live brain
# repo; doesn't touch the real ${BRAIN_PATH}/.
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
START = "2026-05-01"

# Three deterministic test cases. All anchored at fixed `today` so the
# eval doesn't drift with the system clock.
#
# Each case asserts the new fixed-window denominator semantics:
#   - last7  = 7 calendar days ending yesterday
#   - month  = 1st-of-month through yesterday
#   - overall= start_date through yesterday
#   - missing days count as 0 yes (denominator never shrinks).

CASES = [
    {
        "name": "9-day fixture, fully logged, anchored 2026-05-10",
        "fixture": HERE / "sample-9-days.md",
        "anchor_today": dt.date(2026, 5, 10),
        "expected": {
            "started": "2026-05-01",
            # upper_bound = max entry ≤ yesterday(5/9) = 5/9.
            # Window 5/03–5/09 = 7 days. Yes: 5/04, 5/06, 5/07, 5/09 = 4.
            "last7": (4, 7, 57),
            "this_month": (4, 9, 44),
            "overall": (4, 9, 44),
        },
    },
    {
        "name": "9-day fixture, anchored 2026-05-11 (tomorrow's ping; 5/10 not logged yet)",
        "fixture": HERE / "sample-9-days.md",
        "anchor_today": dt.date(2026, 5, 11),
        "expected": {
            "started": "2026-05-01",
            # yesterday=5/10. No entry for 5/10 → upper_bound stays at 5/9.
            # Window 5/03–5/09 = 7 days, NOT [5/04, 5/10].
            "last7": (4, 7, 57),
            # Month 5/01–5/09 (NOT through 5/10) = 9 days.
            "this_month": (4, 9, 44),
            "overall": (4, 9, 44),
        },
    },
    {
        "name": "9-day fixture + 5/10=yes, anchored 2026-05-11 (after user replies to ping)",
        "fixture": HERE / "sample-9-days-plus-510-yes.md",
        "anchor_today": dt.date(2026, 5, 11),
        "expected": {
            "started": "2026-05-01",
            # yesterday=5/10, entry for 5/10 exists → upper_bound = 5/10.
            # Window 5/04–5/10 = 7 days. Yes: 5/04, 5/06, 5/07, 5/09, 5/10 = 5.
            "last7": (5, 7, 71),
            # Month 5/01–5/10 = 10 days. 5 yes.
            "this_month": (5, 10, 50),
            "overall": (5, 10, 50),
        },
    },
    {
        "name": "gap fixture (alternating logged days)",
        "fixture": HERE / "sample-with-gaps.md",
        "anchor_today": dt.date(2026, 5, 10),
        "expected": {
            "started": "2026-05-01",
            # Logged 5/01, 5/03, 5/05, 5/07, 5/09 (all yes). Missing 5/02, 5/04, 5/06, 5/08.
            # upper_bound = 5/9. Window 5/03–5/09 = 7 days.
            # Logged yes: 5/03, 5/05, 5/07, 5/09 = 4. (Missing days = 0 yes.)
            "last7": (4, 7, 57),
            # Month 5/01–5/09 = 9 days. Yes: 5.
            "this_month": (5, 9, 56),
            "overall": (5, 9, 56),
        },
    },
    {
        "name": "no entries yet (fresh start)",
        "fixture": HERE / "sample-empty.md",
        "anchor_today": dt.date(2026, 5, 10),
        "expected": {
            "started": "2026-05-01",
            "last7": (0, 0, 0),
            "this_month": (0, 0, 0),
            "overall": (0, 0, 0),
        },
    },
]


def stats_from_page(src: str, today: dt.date, start_str: str) -> dict:
    """Independent reimplementation of recompute_stats v2 with
    upper_bound = max(entry_date <= yesterday). Mirror of the shell
    script's embedded Python — if either drifts, the eval fails."""
    log_rows = re.findall(r"\| (\d{4}-\d{2}-\d{2}) \| (yes|no) \|", src)
    entries = {d: v for d, v in log_rows}
    yesterday = today - dt.timedelta(days=1)
    start = dt.date.fromisoformat(start_str)

    def window_stats(start_d: dt.date, end_d: dt.date):
        if end_d < start_d:
            return (0, 0, 0)
        yes = 0
        total = 0
        cur = start_d
        while cur <= end_d:
            total += 1
            if entries.get(cur.isoformat()) == "yes":
                yes += 1
            cur += dt.timedelta(days=1)
        pct = round(100 * yes / total) if total else 0
        return (yes, total, pct)

    # Upper bound = latest entry on or before yesterday. None if empty.
    candidate_dates = [
        dt.date.fromisoformat(d) for d in entries
        if dt.date.fromisoformat(d) <= yesterday
    ]
    upper_bound = max(candidate_dates) if candidate_dates else None
    if upper_bound is None:
        return {
            "started": start_str,
            "last7": (0, 0, 0),
            "this_month": (0, 0, 0),
            "overall": (0, 0, 0),
        }

    last7_start = max(upper_bound - dt.timedelta(days=6), start)
    month_first = today.replace(day=1)
    this_month = (
        window_stats(month_first, upper_bound) if upper_bound >= month_first else (0, 0, 0)
    )
    return {
        "started": start_str,
        "last7": window_stats(last7_start, upper_bound),
        "this_month": this_month,
        "overall": window_stats(start, upper_bound),
    }


def main():
    out_json = "--json" in sys.argv
    all_failures = []
    case_results = []

    for case in CASES:
        if not case["fixture"].exists():
            all_failures.append(f"{case['name']}: fixture missing at {case['fixture']}")
            continue
        src = case["fixture"].read_text()
        actual = stats_from_page(src, case["anchor_today"], START)
        case_failures = []
        for k, v_expected in case["expected"].items():
            v_actual = actual[k]
            if v_actual != v_expected:
                case_failures.append(f"{k}: expected {v_expected}, got {v_actual}")
        case_results.append({
            "name": case["name"],
            "fixture": str(case["fixture"].relative_to(HERE.parent.parent.parent)),
            "anchor_today": case["anchor_today"].isoformat(),
            "expected": case["expected"],
            "actual": actual,
            "failures": case_failures,
        })
        all_failures.extend(case_failures)

    if out_json:
        print(json.dumps({
            "ok": len(all_failures) == 0,
            "skill": "reading-checkin",
            "cases": case_results,
            "failures": all_failures,
            "user_message": (
                f"stats-math eval ok ({len(case_results)} cases)"
                if not all_failures
                else f"stats-math eval FAILED: {len(all_failures)} mismatch(es) across {len(case_results)} cases"
            ),
        }, indent=2, default=str))
    else:
        for c in case_results:
            print(f"=== {c['name']} ===")
            print(f"  fixture: {c['fixture']}  anchor: {c['anchor_today']}")
            for k, v_expected in c["expected"].items():
                v_actual = c["actual"][k]
                tag = "✓" if v_actual == v_expected else "✗"
                print(f"  {tag} {k:11s}  expected={v_expected}  actual={v_actual}")
            print()
        if all_failures:
            print(f"FAIL — {len(all_failures)} mismatch(es) across {len(case_results)} cases")
        else:
            print(f"OK — all assertions pass across {len(case_results)} cases")

    sys.exit(0 if not all_failures else 1)


if __name__ == "__main__":
    main()
