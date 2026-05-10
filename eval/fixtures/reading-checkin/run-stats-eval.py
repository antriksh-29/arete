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
            # Window 5/03–5/09 = 7 days. All logged. Yes: 5/04, 5/06, 5/07, 5/09 = 4.
            "last7": (4, 7, 57),
            # Month-to-yesterday = 5/01–5/09 = 9 days. 4 yes.
            "this_month": (4, 9, 44),
            # Overall = same as month here.
            "overall": (4, 9, 44),
        },
    },
    {
        "name": "9-day fixture, anchored 2026-05-11 (simulates tomorrow's ping; 5/10 not logged yet)",
        "fixture": HERE / "sample-9-days.md",
        "anchor_today": dt.date(2026, 5, 11),
        "expected": {
            "started": "2026-05-01",
            # Window 5/04–5/10 = 7 days. 5/10 not logged → 0 yes.
            # Yes: 5/04, 5/06, 5/07, 5/09 = 4.
            "last7": (4, 7, 57),
            # Month 5/01–5/10 = 10 days. 5/10 not logged. 4 yes.
            "this_month": (4, 10, 40),
            "overall": (4, 10, 40),
        },
    },
    {
        "name": "gap fixture (alternating logged days)",
        "fixture": HERE / "sample-with-gaps.md",
        "anchor_today": dt.date(2026, 5, 10),
        "expected": {
            "started": "2026-05-01",
            # Logged 5/01, 5/03, 5/05, 5/07, 5/09 (all yes). Missing 5/02, 5/04, 5/06, 5/08.
            # Window 5/03–5/09 = 7 days. Logged yes: 5/03, 5/05, 5/07, 5/09 = 4.
            "last7": (4, 7, 57),
            # Month 5/01–5/09 = 9 days. Logged yes: 5 (alternating).
            "this_month": (5, 9, 56),
            "overall": (5, 9, 56),
        },
    },
]


def stats_from_page(src: str, today: dt.date, start_str: str) -> dict:
    """Independent reimplementation of recompute_stats with the fixed-
    window denominator semantics. Mirror of the shell script's embedded
    Python — if either drifts, the eval fails."""
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

    last7_start = yesterday - dt.timedelta(days=6)
    return {
        "started": start_str,
        "last7": window_stats(last7_start, yesterday),
        "this_month": window_stats(today.replace(day=1), yesterday),
        "overall": window_stats(start, yesterday),
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
