#!/usr/bin/env python3
"""e2e-direct.py — exercise the bot via `openclaw agent` direct calls
instead of Telethon → Telegram channel.

Why this exists: the Telegram-channel path adds ~10s signal-detector
spawn + Telegram delivery overhead + cumulative session-history bloat
per turn. That's fine for real users (45-90s per reply) but kills
rapid test loops, because once one turn exceeds the harness timeout,
the next test's wait window picks up the prior late reply and the
attribution cascades wrong.

This runner sidesteps all of that:
  • Per-test unique session_id (no cross-test pollution).
  • Direct CLI invocation (skips Telegram channel queue).
  • No signal-detector spawn (signal capture only fires through the
    inbound channel hook, not via direct `openclaw agent` calls).
  • Parses agent's structured JSON output (result.payloads[0].text).

Use it for skill validation. Use the Telethon harness for the real
end-to-end pipeline test once a small number of cases is shaking out
clean — Telethon is the higher-fidelity but slower checker.

Usage:
    python3 infra/e2e-direct.py --only brv-01,brv-02,brv-03
    python3 infra/e2e-direct.py --only brv-* (all matching prefix)
    python3 infra/e2e-direct.py --json /tmp/out.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from fnmatch import fnmatch
from pathlib import Path

# Minimal YAML-ish parser, same shape the Telethon harness uses.
TESTS_FILE = Path(__file__).resolve().parent / "e2e-tests.yaml"


def _strip_inline_comment(s: str) -> str:
    if s.startswith("'") or s.startswith('"'):
        return s
    h = s.find(" #")
    return s[:h].rstrip() if h >= 0 else s


def load_tests() -> list[dict]:
    txt = TESTS_FILE.read_text()
    out, cur, field = [], None, None
    for raw in txt.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("tests:"):
            continue
        if line.startswith("  - id:"):
            if cur is not None:
                out.append(cur)
            cur, field = {"id": _strip_inline_comment(line.split(":", 1)[1].strip())}, None
            continue
        if cur is None:
            continue
        m = re.match(r"^    (\w+):\s*(.*)$", line)
        if m:
            k, v = m.group(1), m.group(2).strip()
            if v:
                if k == "timeout":
                    cur[k] = int(v)
                else:
                    cur[k] = _strip_inline_comment(v).strip('"').strip("'")
            else:
                cur[k] = {}
                field = k
            continue
        if field == "expects" and line.startswith("      "):
            m2 = re.match(r"^      (\w+):\s*(.*)$", line)
            if m2:
                k, v = m2.group(1), m2.group(2).strip()
                if v:
                    if k == "reply_min_len":
                        cur["expects"][k] = int(v)
                    elif k in ("reply_regex", "reply_contains", "reply_not_contains") and v.startswith("["):
                        try:
                            cur["expects"][k] = json.loads(
                                v.replace("'", '"').replace("\\\\", "\\")
                            )
                        except Exception:
                            cur["expects"][k] = []
                    else:
                        cur["expects"][k] = v
    if cur is not None:
        out.append(cur)
    return out


def extract_reply(d: dict) -> str:
    """Pull the user-visible text from the agent's JSON output."""
    payloads = (d.get("result") or {}).get("payloads") or []
    if payloads:
        t = payloads[0].get("text")
        if t:
            return t
    # fallback walk
    def walk(o, depth=0):
        if depth > 6:
            return None
        if isinstance(o, dict):
            for k in ("text", "reply", "user_message", "content"):
                v = o.get(k)
                if isinstance(v, str) and len(v) > 5:
                    return v
            for v in o.values():
                r = walk(v, depth + 1)
                if r:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = walk(v, depth + 1)
                if r:
                    return r
        return None
    return walk(d) or ""


def check(reply: str, expects: dict) -> list[str]:
    fails = []
    if "reply_min_len" in expects and len(reply) < expects["reply_min_len"]:
        fails.append(f"reply too short ({len(reply)} < {expects['reply_min_len']} chars)")
    for rx in expects.get("reply_regex", []) or []:
        if not re.search(rx, reply, re.MULTILINE):
            fails.append(f"regex miss: {rx!r}")
    for s in expects.get("reply_contains", []) or []:
        if s.lower() not in reply.lower():
            fails.append(f"missing substring: {s!r}")
    for s in expects.get("reply_not_contains", []) or []:
        if s.lower() in reply.lower():
            fails.append(f"forbidden substring present: {s!r}")
    return fails


def run_one(t: dict, default_timeout: int = 240) -> dict:
    msg = t["message"]
    timeout = t.get("timeout", default_timeout)
    sid = f"e2e-direct-{t['id']}-{int(time.time())}"
    t0 = time.time()
    try:
        proc = subprocess.run(
            ["openclaw", "agent", "--message", msg, "--session-id", sid, "--json"],
            capture_output=True, text=True, timeout=timeout + 15,
        )
        elapsed = time.time() - t0
        if proc.returncode != 0:
            return {"id": t["id"], "pass": False, "elapsed_s": round(elapsed, 1),
                    "failures": [f"exit {proc.returncode}: {proc.stderr.strip()[:200]}"],
                    "reply_text": ""}
        try:
            d = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"id": t["id"], "pass": False, "elapsed_s": round(elapsed, 1),
                    "failures": [f"non-JSON output: {proc.stdout[:200]}"],
                    "reply_text": ""}
        reply = extract_reply(d)
        fails = check(reply, t.get("expects") or {})
        return {"id": t["id"], "pass": not fails, "elapsed_s": round(elapsed, 1),
                "failures": fails, "reply_text": reply,
                "duration_ms_agent": ((d.get("result") or {}).get("meta") or {}).get("durationMs")}
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        return {"id": t["id"], "pass": False, "elapsed_s": round(elapsed, 1),
                "failures": [f"timeout after {elapsed:.1f}s"], "reply_text": ""}


ROUTE_ONLY_SYSTEM_PROMPT = (
    "ROUTING-ONLY MODE: do NOT execute any skill. Do NOT compose a normal reply. "
    "Do NOT call any tool other than this single response. "
    "Inspect the user's message and respond with exactly one line: "
    "`ROUTED_SKILL=<name>` where <name> is the SKILL.md folder name you "
    "would invoke (e.g. `ingest-pdf`, `brain-ask`, `skill-author`), or "
    "`ROUTED_SKILL=default` if no skill would route and you would compose "
    "a freeform chat reply. Output ONLY that single line and nothing else."
)


def route_only(message: str, timeout: int = 30) -> tuple[str, float, str]:
    """Ask the agent which skill it WOULD route to, without executing it.

    Returns (skill_name, elapsed_seconds, raw_reply). skill_name is `"error"`
    if the call failed or the reply didn't contain a ROUTED_SKILL line.
    """
    sid = f"route-only-{int(time.time() * 1000)}"
    t0 = time.time()
    try:
        proc = subprocess.run(
            ["openclaw", "agent",
             "--system", ROUTE_ONLY_SYSTEM_PROMPT,
             "--message", message,
             "--session-id", sid,
             "--json"],
            capture_output=True, text=True, timeout=timeout,
        )
        elapsed = time.time() - t0
        if proc.returncode != 0:
            return ("error", elapsed, f"exit {proc.returncode}: {proc.stderr.strip()[:200]}")
        try:
            d = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return ("error", elapsed, f"non-JSON output: {proc.stdout[:200]}")
        reply = extract_reply(d)
        m = re.search(r"ROUTED_SKILL=([a-zA-Z0-9_./-]+)", reply)
        if m:
            return (m.group(1).strip(), elapsed, reply)
        return ("error", elapsed, f"no ROUTED_SKILL in reply: {reply[:200]}")
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        return ("error", elapsed, f"timeout after {elapsed:.1f}s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated test IDs or glob (brv-*) — full e2e mode")
    ap.add_argument("--json", help="write full results JSON to this path")
    # NEW: routing-eval support (used by meta/skill-author/scripts/routing-eval.ts).
    # When --route-only is set, --only is ignored; --message is the utterance
    # to classify. Output is a single JSON line: {"skill": "...", "elapsed_s": ..., "ok": true|false}
    ap.add_argument("--route-only", action="store_true",
                    help="ask the agent which skill it would route to, without executing")
    ap.add_argument("--message", help="utterance to route (with --route-only)")
    ap.add_argument("--timeout", type=int, default=30, help="seconds (route-only mode)")
    args = ap.parse_args()

    # ── Route-only mode (single call) ─────────────────────────────
    if args.route_only:
        if not args.message:
            print(json.dumps({"ok": False, "error": "--message required with --route-only"}))
            return 2
        skill, elapsed, raw = route_only(args.message, timeout=args.timeout)
        ok = skill != "error"
        print(json.dumps({
            "ok": ok,
            "skill": skill,
            "elapsed_s": round(elapsed, 2),
            "message": args.message,
            "raw_excerpt": raw[:200] if not ok else None,
        }))
        return 0 if ok else 1

    if not args.only:
        ap.error("either --only (full mode) or --route-only + --message is required")

    tests = load_tests()
    by_id = {t["id"]: t for t in tests}
    patterns = [s.strip() for s in args.only.split(",") if s.strip()]
    selected = []
    for p in patterns:
        if "*" in p or "?" in p:
            selected.extend(t for t in tests if fnmatch(t["id"], p))
        elif p in by_id:
            selected.append(by_id[p])
        else:
            print(f"WARN: test {p!r} not found", file=sys.stderr)

    if not selected:
        print("no tests matched", file=sys.stderr)
        return 2

    print(f"[direct] {len(selected)} test(s) queued via direct openclaw agent (per-session isolation)")
    results = []
    for i, t in enumerate(selected, 1):
        print(f"\n[{i}/{len(selected)}] {t['id']:30}", end=" ", flush=True)
        r = run_one(t)
        results.append(r)
        mark = "✓" if r["pass"] else "✗"
        agent_ms = r.get("duration_ms_agent")
        agent_s = f" agent={agent_ms/1000:.1f}s" if agent_ms else ""
        print(f"→  {mark} ({r['elapsed_s']:.1f}s{agent_s})")
        if not r["pass"]:
            for f in r["failures"]:
                print(f"     - {f}")

    n_pass = sum(1 for r in results if r["pass"])
    print(f"\n{'='*60}\n {n_pass}/{len(results)} passed\n{'='*60}\n")
    if args.json:
        Path(args.json).write_text(json.dumps({"results": results, "summary": {"passed": n_pass, "total": len(results)}}, indent=2))
        print(f"  full results → {args.json}")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
