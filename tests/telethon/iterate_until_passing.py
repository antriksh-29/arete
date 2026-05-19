#!/usr/bin/env python3
"""iterate_until_passing.py — drives the full Telethon suite until it
passes twice in a row across two separate runs.

Workflow per round:
  1. Run all 10 cases (`pytest tests/telethon/test_skill_authoring.py`).
  2. Collect failure transcripts.
  3. Classify each failure into one of:
       intent-detection-miss / safety-gate-false-positive /
       routing-gate-collision / commit-deploy-glitch / brain-page-missing /
       hot-reload-miss / telegram-callback-wording-wrong / unknown
  4. Persist `tests/telethon/failure-categories.json` with the run's failures.
  5. Surface the failures + classifications so the operator can patch.
  6. Re-run.

The suite is "green" only when two consecutive full runs pass with 0
failures. After that, write `tests/telethon/RESULTS.md` with the
passing transcripts and exit 0. Otherwise exit non-zero after
--max-rounds reached.

This is NOT a fully-automated patching loop — it's a structured surface
for the human + Claude Code to drive the fix-and-retry cycle. The
classifier exists so you spend zero time pattern-matching what kind of
failure each TC is.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TESTS = REPO / "tests" / "telethon"
RESULTS_MD = TESTS / "RESULTS.md"
CATEGORIES_JSON = TESTS / "failure-categories.json"


CATEGORY_RULES = [
    # (substring or regex of failure message, category)
    (r"expected (?:deploy callback|.*deployed|.*committed|live)", "telegram-callback-wording-wrong"),
    (r"D7 BROKEN", "unknown"),
    (r"forked_from", "commit-deploy-glitch"),
    (r"version did not bump", "commit-deploy-glitch"),
    (r"expected quarantine wording", "safety-gate-false-positive"),
    (r"evil skill was committed", "safety-gate-false-positive"),
    (r"expected collision wording", "routing-gate-collision"),
    (r"expected slug-bump wording", "commit-deploy-glitch"),
    (r"expected resume prompt", "telegram-callback-wording-wrong"),
    (r"use_count not incremented", "hot-reload-miss"),
    (r"no resolver hit logged", "hot-reload-miss"),
    (r"timed out|TimeoutError", "intent-detection-miss"),
    (r"morning-quote.*not.*exist|user-skills/[a-z0-9-]+/SKILL\.md", "commit-deploy-glitch"),
    (r"brain/skills/.*does not exist", "brain-page-missing"),
]


def classify(failure_text: str) -> str:
    for pattern, category in CATEGORY_RULES:
        if re.search(pattern, failure_text, re.IGNORECASE):
            return category
    return "unknown"


def run_pytest() -> dict:
    """Run the suite, return parsed result dict."""
    print(f"\n{'='*60}\n[{time.strftime('%H:%M:%S')}] starting pytest run\n{'='*60}")
    proc = subprocess.run(
        ["pytest", "tests/telethon/test_skill_authoring.py", "-v", "--tb=short", "--no-header"],
        capture_output=True, text=True, cwd=REPO,
    )
    output = proc.stdout + "\n" + proc.stderr
    failures = []
    # Crude parse: pytest's PASSED / FAILED lines
    for line in output.splitlines():
        m = re.match(r"tests/telethon/.* (FAILED|ERROR).*", line)
        if m:
            tc_match = re.search(r"::test_(tc\d+)_(\w+)", line)
            if tc_match:
                tc = f"TC{tc_match.group(1)[2:]}"
                failures.append({"tc": tc, "name": tc_match.group(2), "line": line})
    # Extract failure tracebacks
    for f in failures:
        tb_match = re.search(rf"{f['name']}.+?(?=___|^(?:tests/|=))", output, re.DOTALL | re.MULTILINE)
        f["traceback"] = tb_match.group(0)[:2000] if tb_match else ""
        f["category"] = classify(f["traceback"])
    return {
        "exit_code": proc.returncode,
        "output": output,
        "failures": failures,
        "summary": next((l for l in output.splitlines() if "passed" in l or "failed" in l), ""),
    }


def write_categories(round_num: int, result: dict) -> None:
    payload = {
        "round": round_num,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": result["summary"],
        "failures_by_category": {},
    }
    for f in result["failures"]:
        payload["failures_by_category"].setdefault(f["category"], []).append({
            "tc": f["tc"], "name": f["name"], "traceback_excerpt": f["traceback"][:500],
        })
    CATEGORIES_JSON.write_text(json.dumps(payload, indent=2))
    print(f"\nfailure categories written to {CATEGORIES_JSON}")
    for cat, fs in payload["failures_by_category"].items():
        print(f"  {cat}: {len(fs)} failure(s) — {', '.join(f['tc'] for f in fs)}")


def write_results_md(rounds: list[dict]) -> None:
    lines = [
        "# Telethon e2e results — skill-author flow",
        "",
        f"Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"Total rounds: {len(rounds)}",
        "",
        "## Final verdict",
        "",
    ]
    if len(rounds) >= 2 and rounds[-1]["exit_code"] == 0 and rounds[-2]["exit_code"] == 0:
        lines.append("**GREEN** — passed 2x consecutively. Safe to flip `ALFRED_SKILL_AUTHORING=on` for live traffic.")
    else:
        lines.append("**RED** — did not reach 2x consecutive green. Inspect `failure-categories.json` for the most recent round.")
    lines.append("")
    lines.append("## Per-round summary")
    lines.append("")
    lines.append("| Round | Exit | Summary |")
    lines.append("|-------|------|---------|")
    for i, r in enumerate(rounds, 1):
        lines.append(f"| {i} | {r['exit_code']} | {r['summary'][:120]} |")
    RESULTS_MD.write_text("\n".join(lines) + "\n")
    print(f"\nresults written to {RESULTS_MD}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-rounds", type=int, default=8, help="give up after this many rounds")
    ap.add_argument("--cooldown-s", type=int, default=30, help="wait between rounds")
    args = ap.parse_args()

    rounds = []
    consecutive_green = 0
    for round_num in range(1, args.max_rounds + 1):
        result = run_pytest()
        rounds.append(result)
        print(f"\n[round {round_num}] exit={result['exit_code']} — {result['summary']}")
        if result["failures"]:
            write_categories(round_num, result)
        if result["exit_code"] == 0:
            consecutive_green += 1
            print(f"[round {round_num}] GREEN ({consecutive_green}/2 consecutive)")
            if consecutive_green >= 2:
                write_results_md(rounds)
                print("\n🎉 GREEN twice in a row. Safe to flip ALFRED_SKILL_AUTHORING=on.")
                return 0
        else:
            consecutive_green = 0
        if round_num < args.max_rounds:
            print(f"\ncooling down {args.cooldown_s}s before next round...")
            time.sleep(args.cooldown_s)

    write_results_md(rounds)
    print("\n❌ max rounds reached without 2x consecutive green. Patch + re-run.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
