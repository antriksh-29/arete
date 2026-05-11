#!/usr/bin/env python3
# e2e-summarize.py — read a JSON result file from infra/e2e-harness.py
# and produce a categorised report:
#
#   - Passes (just the count)
#   - Timeouts (bot didn't reply at all → routing miss / coalesce / rate-limit)
#   - Assertion failures (bot replied but content didn't match expectations)
#   - Harness errors (regex compile, parser bug, etc.)
#
# Usage:
#   python3 infra/e2e-summarize.py /tmp/e2e-full3.json

import json
import sys
from pathlib import Path


def classify(result: dict) -> str:
    if result["pass"]:
        return "pass"
    failures = result.get("failures", [])
    if not failures:
        return "pass"
    if any("timeout" in f.lower() for f in failures):
        return "timeout"
    if any("harness exception" in f.lower() for f in failures):
        return "harness_error"
    if any("regex compile" in f.lower() for f in failures):
        return "harness_error"
    return "assertion_fail"


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: e2e-summarize.py <path-to-json>")
    data = json.loads(Path(sys.argv[1]).read_text())
    results = data["results"]

    buckets = {"pass": [], "timeout": [], "assertion_fail": [], "harness_error": []}
    for r in results:
        buckets[classify(r)].append(r)

    print(f"Total: {len(results)} tests against bot {data.get('bot','?')}\n")
    for name in ["pass", "assertion_fail", "timeout", "harness_error"]:
        items = buckets[name]
        symbol = {"pass": "✓", "assertion_fail": "✗", "timeout": "⏱", "harness_error": "⚠"}[name]
        label = {"pass": "PASSED", "assertion_fail": "ASSERTION FAILURES (bot replied, content mismatched)",
                 "timeout": "TIMEOUTS (no reply — routing / coalesce / rate-limit)",
                 "harness_error": "HARNESS ERRORS (test definition bug)"}[name]
        print(f"{symbol} {label}: {len(items)}")
        for r in items:
            print(f"    {r['id']:30s}  skill={r.get('skill','?'):20s}  elapsed={r.get('elapsed_s', '?')}s")
            for f in r.get("failures", []):
                print(f"      └─ {f}")
            if name in ("assertion_fail",) and r.get("reply_text"):
                preview = r["reply_text"][:120].replace("\n", " ")
                print(f"      └─ reply: {preview!r}")
        print()

    print("─" * 60)
    print(f" Pass rate: {len(buckets['pass'])}/{len(results)} = {100 * len(buckets['pass']) // len(results)}%")
    if buckets["timeout"]:
        print(" Next steps for TIMEOUTS:")
        print("   • Check bot journal: ssh bot 'journalctl --user -u openclaw-gateway --since \"15 min ago\"'")
        print("   • Look for OpenAI rate-limit retries or coalescing")
    if buckets["assertion_fail"]:
        print(" Next steps for ASSERTION FAILURES:")
        print("   • Review the reply previews above")
        print("   • Either tighten the bot's behavior OR loosen the test expectation")


if __name__ == "__main__":
    main()
