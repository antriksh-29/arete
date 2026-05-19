#!/usr/bin/env python3
"""usage-tracker.py — tail Axiom log shipper output, update
user-skills/<slug>/.usage.json on every resolver hit.

Why this lives outside OpenClaw: we don't own the OpenClaw resolver's
internals (it's a vendored npm package). The cleanest seam is the
existing log stream — every skill invocation emits an Axiom-shipped
log line, so we tail that and update the per-skill telemetry sidecar.

Log signature we match (emitted by the existing log shipper):
    [resolver] hit=user-skills/<slug>            (success)
    [resolver] hit=user-skills/<slug> error=...  (skill ran and failed)

Output: per-slug .usage.json that looks like:
    {
      "use_count": 42,
      "last_used_at": "2026-05-19T10:14:32Z",
      "error_count": 3,
      "last_error": "rate limit exceeded",
      "last_error_at": "2026-05-18T22:01:10Z"
    }

Runs as a systemd user service (infra/usage-tracker.service — installed
by deploy-vps.sh). Idempotent — restart-safe. Latency budget: <5ms per
log line (it's just a JSON write).

CLI:
    usage-tracker.py --tail FILE           # follow FILE (live shipping)
    usage-tracker.py --replay FILE         # process a static file once
    usage-tracker.py --smoke               # self-test (writes a fixture)

Author/owner: Alfred runtime. Hand-edits to .usage.json files are
clobbered on the next resolver hit; that's intentional — the sidecar
is a runtime-managed file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# Repo root (so we can find user-skills/ from the VPS deploy location).
REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parent.parent))
USER_SKILLS = REPO_ROOT / "user-skills"

# Match: `[resolver] hit=user-skills/<slug>` followed by optional ` error=<msg>`.
RESOLVER_LINE = re.compile(
    r"\[resolver\]\s+hit=user-skills/([a-zA-Z0-9_-]+)(?:\s+(.+))?"
)
ERROR_FIELD = re.compile(r"\berror=(?:\"([^\"]*)\"|(\S+))")


def update_usage(slug: str, error: str | None) -> bool:
    """Update user-skills/<slug>/.usage.json atomically. Returns True if
    the file was written, False if the slug doesn't exist (skill was
    deleted but a stale log line is still flowing — log and skip)."""
    skill_dir = USER_SKILLS / slug
    if not skill_dir.is_dir():
        return False
    target = skill_dir / ".usage.json"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # Load existing (or seed)
    data: dict = {}
    if target.exists():
        try:
            data = json.loads(target.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    data.setdefault("use_count", 0)
    data.setdefault("error_count", 0)
    data["use_count"] = int(data["use_count"]) + 1
    data["last_used_at"] = now_iso
    if error:
        data["error_count"] = int(data["error_count"]) + 1
        data["last_error"] = error[:200]   # cap to keep file small
        data["last_error_at"] = now_iso
    # Atomic write: temp file + rename.
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(target)
    return True


def process_line(line: str) -> str | None:
    """Extract slug + error from a log line. Returns the slug if matched,
    else None. Also writes the usage update side-effect."""
    m = RESOLVER_LINE.search(line)
    if not m:
        return None
    slug = m.group(1)
    extra = m.group(2) or ""
    err_match = ERROR_FIELD.search(extra)
    error = (err_match.group(1) or err_match.group(2)) if err_match else None
    written = update_usage(slug, error)
    if not written:
        print(f"[usage-tracker] stale resolver hit for missing slug: {slug}", file=sys.stderr)
    return slug


def tail(path: Path, poll_s: float = 0.25):
    """Naive tail -f. Restart-safe: resumes from EOF on each connect.
    For a log shipper that rotates aggressively, switch to a notify-based
    watcher; for our daily-rotated logs this is fine."""
    if not path.exists():
        print(f"[usage-tracker] waiting for {path} to appear...", file=sys.stderr)
        while not path.exists():
            time.sleep(1)
    with path.open("r") as fh:
        fh.seek(0, 2)   # EOF
        while True:
            line = fh.readline()
            if not line:
                time.sleep(poll_s)
                continue
            process_line(line)


def replay(path: Path) -> int:
    """One-shot replay of a static log file. Returns number of updates."""
    count = 0
    with path.open("r") as fh:
        for line in fh:
            if process_line(line):
                count += 1
    return count


def smoke() -> int:
    """Self-test: writes a fixture log line, processes it, asserts the
    .usage.json was updated. Exits 0 on pass, non-zero on fail."""
    # Create a fake slug
    fake_slug = "usage-tracker-smoke"
    skill_dir = USER_SKILLS / fake_slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("---\nname: usage-tracker-smoke\n---\nsmoke\n")
    # Process a synthetic line
    process_line("[resolver] hit=user-skills/usage-tracker-smoke")
    target = skill_dir / ".usage.json"
    if not target.exists():
        print("smoke FAIL: .usage.json not written", file=sys.stderr)
        return 1
    data = json.loads(target.read_text())
    if data.get("use_count") != 1:
        print(f"smoke FAIL: use_count={data.get('use_count')!r} (expected 1)", file=sys.stderr)
        return 1
    # Now simulate an error
    process_line("[resolver] hit=user-skills/usage-tracker-smoke error=\"thing broke\"")
    data = json.loads(target.read_text())
    if data.get("use_count") != 2 or data.get("error_count") != 1 or data.get("last_error") != "thing broke":
        print(f"smoke FAIL: state={data!r}", file=sys.stderr)
        return 1
    # Cleanup
    target.unlink()
    (skill_dir / "SKILL.md").unlink()
    skill_dir.rmdir()
    print("smoke OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tail", help="follow this log file (live mode)")
    g.add_argument("--replay", help="process this log file once and exit")
    g.add_argument("--smoke", action="store_true", help="self-test")
    args = ap.parse_args()
    if args.smoke:
        return smoke()
    if args.replay:
        n = replay(Path(args.replay))
        print(f"processed {n} resolver hit(s)")
        return 0
    if args.tail:
        try:
            tail(Path(args.tail))
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
