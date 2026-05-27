#!/usr/bin/env python3
# migrate-worker.py — background re-ingest of every reading-list row.
#
# Spawned by reading-list.py cmd_migrate. Runs serially through the
# queue (browser-harness has a single Chrome — concurrent runs would
# fight for it), updates each row in place, and posts a single
# Telegram summary at the end. Eliminates the 120s per-turn timeout
# that previously hit migrate ("Migration got partway through, then
# the run was terminated").
#
# Usage:
#   migrate-worker.py [--force]
#     --force   Re-ingest every row, even rows whose brain_page is set.
#               Without this, only rows missing brain_page / posted_by
#               / one_liner are touched.
#
# Reads cred from the arete-cred vault (TELEGRAM_BOT_TOKEN / TELEGRAM_USER_ID).
# Exit code is informational only; nobody is waiting.

import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def log(msg):
    print(f"[migrate-worker] {msg}", file=sys.stderr)


def _load_reading_list():
    here = Path(__file__).resolve().parent
    rl_path = here / "reading-list.py"
    if not rl_path.exists():
        rl_path = Path("${STATE_HOME}/bin/reading-list.py")
    spec = importlib.util.spec_from_file_location("reading_list", rl_path)
    assert spec is not None and spec.loader is not None
    rl = importlib.util.module_from_spec(spec)
    sys.modules["reading_list"] = rl
    spec.loader.exec_module(rl)
    return rl


def _vault_get(key):
    try:
        r = subprocess.run(["arete-cred", "get", key], capture_output=True, text=True)
    except FileNotFoundError:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _send_telegram(text):
    token = _vault_get("TELEGRAM_BOT_TOKEN")
    chat_id = _vault_get("TELEGRAM_USER_ID")
    if not token or not chat_id:
        log("TELEGRAM creds missing; skipping follow-up")
        return False
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read())
    except Exception as e:
        log(f"telegram send failed: {e}")
        return False
    if not resp.get("ok"):
        log(f"telegram rejected: {json.dumps(resp)[:200]}")
        return False
    return True


def _drain_xref_queue():
    drainer = Path(__file__).resolve().parent / "xref-drainer.py"
    if not drainer.exists():
        drainer = Path("${STATE_HOME}/bin/xref-drainer.py")
    if not drainer.exists():
        return
    subprocess.run(
        ["python3", str(drainer), "--once"],
        capture_output=True, timeout=120, check=False,
    )


def _write_last_bookmark_pointer(row):
    """Mirror reading-list-worker — keep the convenience pointer fresh
    so brain-ask `last` resolves to the most-recently-migrated row."""
    try:
        path = Path.home() / ".brain" / ".last-bookmark.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "slug": row.get("brain_page", ""),
            "url": row.get("url", ""),
            "title": row.get("title", ""),
            "posted_by": row.get("posted_by", ""),
            "one_liner": row.get("one_liner", ""),
            "kind": row.get("kind", ""),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "via": "migrate-worker",
        }, indent=2))
    except Exception as e:
        log(f"last-bookmark write failed (non-fatal): {e}")


def main():
    force = "--force" in sys.argv[1:]
    rl = _load_reading_list()

    state = rl.load_state()
    queue = state["queue"]
    read = state["read"]
    log(f"loaded state: {len(queue)} queue, {len(read)} read; force={force}")

    # Plan: serial iteration with state-reload between rows so concurrent
    # adds/drops by the user during migrate don't get clobbered.
    targets = []
    for bucket in ("queue", "read"):
        for r in state[bucket]:
            needs = force or not r.get("brain_page") or not r.get("posted_by") or not r.get("one_liner")
            if needs:
                targets.append((bucket, rl.normalize_url(r["url"])))
    total = len(targets)
    log(f"will process {total} row(s)")

    if total == 0:
        _send_telegram("📚 Migration: nothing to do — every bookmark already has a brain page + sidecar.")
        return

    # Initial "starting" message so the user knows the worker accepted.
    _send_telegram(
        f"📚 Migrating {total} bookmark{'s' if total != 1 else ''} in the background. "
        f"I'll send a summary when done (usually a few minutes)."
    )

    updated = 0
    failed = 0
    last_completed_row = None
    started = time.time()

    for i, (bucket, url) in enumerate(targets, 1):
        # Reload state every iteration so the user can drop/add during migrate
        # without us writing a stale snapshot.
        fresh = rl.load_state()
        # Find the row in the fresh state by URL
        row_idx = None
        for j, r in enumerate(fresh[bucket]):
            if rl.normalize_url(r["url"]) == url:
                row_idx = j
                break
        if row_idx is None:
            log(f"[{i}/{total}] row gone during migrate: {url}")
            continue
        row = fresh[bucket][row_idx]
        if not row.get("kind"):
            row["kind"] = rl.url_kind(url)
        log(f"[{i}/{total}] hydrating {bucket}[{row_idx}] kind={row['kind']} url={url}")
        try:
            f = rl.run_ingestor(url, row["kind"])
        except Exception as e:
            log(f"[{i}/{total}] ingestor crashed: {e}")
            failed += 1
            continue
        had_brain = bool(f.get("brain_page"))
        if had_brain:
            row["title"] = f["title"] or rl.clean_cell(row.get("title"))
            row["posted_by"] = f["posted_by"] or rl.url_host(url)
            row["one_liner"] = f["one_liner"]
            row["brain_page"] = f["brain_page"]
            updated += 1
            last_completed_row = dict(row)
        else:
            failed += 1
        fresh[bucket][row_idx] = row
        rl.save_state(fresh)
        log(f"[{i}/{total}] done (updated={updated} failed={failed} elapsed={time.time()-started:.0f}s)")

    # Drain xref queue once at the end so all the per-row entity links
    # populate in one pass instead of N timer ticks.
    _drain_xref_queue()
    if last_completed_row:
        _write_last_bookmark_pointer(last_completed_row)

    elapsed = int(time.time() - started)
    msg_lines = [
        f"📚 Migration complete in {elapsed}s",
        f"• {updated} ingested deep (brain page + raw sidecar)",
    ]
    if failed:
        msg_lines.append(f"• {failed} failed (kept with shallow fields)")
    if last_completed_row and last_completed_row.get("brain_page"):
        msg_lines.append(f"\nLatest brain page: {last_completed_row['brain_page']}")
    _send_telegram("\n".join(msg_lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        try:
            _send_telegram(f"⚠️ Migrate worker crashed: {str(e)[:200]}")
        except Exception:
            pass
        sys.exit(1)
