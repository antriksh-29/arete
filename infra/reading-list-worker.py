#!/usr/bin/env python3
# reading-list-worker.py — background deep-ingestor for the reading-list
# async path. Spawned by reading-list.py cmd_add after the user-facing
# ack has been returned to the agent (and from there to Telegram).
#
# Why this exists: the deep ingestors for thin X tweets (media-led,
# link-only) trigger a browser-harness fallback that pushes total
# wall time past the 120s per-turn timeout documented in
# docs/E2E-FINDINGS.md — Telegram's webhook reply window closes and
# the bot returns an "ingest didn't finish" apology. By running the
# ingest off the turn, the user gets a fast ack and a separate
# Telegram follow-up when the brain page is ready.
#
# Usage:
#   reading-list-worker.py <url>
#
# Exit code is informational only; the parent has already returned.

import importlib.util
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def log(msg):
    print(f"[reading-list-worker] {msg}", file=sys.stderr)


def _load_reading_list():
    here = Path(__file__).resolve().parent
    rl_path = here / "reading-list.py"
    if not rl_path.exists():
        rl_path = Path("/home/botuser/bin/reading-list.py")
    spec = importlib.util.spec_from_file_location("reading_list", rl_path)
    assert spec is not None and spec.loader is not None
    rl = importlib.util.module_from_spec(spec)
    sys.modules["reading_list"] = rl
    spec.loader.exec_module(rl)
    return rl


def _vault_get(key):
    r = subprocess.run(["bot-cred", "get", key], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _send_telegram(text):
    token = _vault_get("TELEGRAM_BOT_TOKEN")
    chat_id = _vault_get("TELEGRAM_USER_ID")
    if not token or not chat_id:
        log("TELEGRAM_BOT_TOKEN or TELEGRAM_USER_ID missing from vault; skipping follow-up")
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
        log(f"Telegram send failed: {e}")
        return False
    if not resp.get("ok"):
        log(f"Telegram rejected: {json.dumps(resp)[:300]}")
        return False
    return True


def _write_last_bookmark_pointer(row):
    """Persist the slug of the most recently completed bookmark so the
    agent's follow-up questions ("what does that article say?",
    "quote from it") can resolve to a brain page without the user
    naming the slug. brain-ask.py reads ~/.brain/.last-bookmark.json
    when invoked with the literal pointer string `last`.

    Best-effort — failures don't tank the worker, just mean the
    convenience pointer is stale until the next bookmark."""
    try:
        from datetime import datetime, timezone
        path = Path.home() / ".brain" / ".last-bookmark.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "slug": row.get("brain_page", ""),
            "url": row.get("url", ""),
            "title": row.get("title", ""),
            "posted_by": row.get("posted_by", ""),
            "one_liner": row.get("one_liner", ""),
            "kind": row.get("kind", ""),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(payload, indent=2))
        log(f"wrote .last-bookmark.json → {payload['slug']}")
    except Exception as e:
        log(f"last-bookmark pointer write failed (non-fatal): {e}")


def _drain_xref_queue():
    drainer = Path(__file__).resolve().parent / "xref-drainer.py"
    if not drainer.exists():
        drainer = Path("/home/botuser/bin/xref-drainer.py")
    if not drainer.exists():
        return
    # Best-effort, short timeout — the systemd timer is the real drain.
    subprocess.run(
        ["python3", str(drainer), "--once"],
        capture_output=True, timeout=120, check=False,
    )


def _format_done_message(row):
    """Follow-up message shape — mirrors the post-ack confirmation the
    user would have seen synchronously in the legacy path."""
    lines = [f"✅ Bookmark ready: {row['title'][:80]}",
             f"Posted by: {row['posted_by']}"]
    if row.get("one_liner"):
        lines.append(f"About: {row['one_liner']}")
    lines.append(row["url"])
    if row.get("brain_page"):
        lines.append(f"\nBrain page: {row['brain_page']}")
    return "\n".join(lines)


def _format_failed_message(row, reason):
    return (
        f"⚠️ Bookmark saved but deep ingest failed: {row['title'][:80]}\n"
        f"{row['url']}\n"
        f"Reason: {reason[:200]}\n"
        f"Run `migrate bookmarks` to retry."
    )


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: reading-list-worker.py <url>")
    raw_url = sys.argv[1]

    rl = _load_reading_list()
    url = rl.normalize_url(raw_url)

    state = rl.load_state()
    row_idx = None
    for i, r in enumerate(state["queue"]):
        if rl.normalize_url(r["url"]) == url:
            row_idx = i
            break
    if row_idx is None:
        # User may have dropped the pending row between ack and worker
        # start, or another worker beat us to it. Either way, nothing
        # to do — exit clean.
        log(f"pending row for {url} not found; user dropped it or already hydrated")
        return

    row = state["queue"][row_idx]
    kind = row.get("kind") or rl.url_kind(url)

    log(f"hydrating row #{row_idx+1} kind={kind} url={url}")
    fields = rl.run_ingestor(url, kind)

    if fields["brain_page"]:
        row["title"] = fields["title"] or row["title"]
        row["posted_by"] = fields["posted_by"] or row["posted_by"]
        row["one_liner"] = fields["one_liner"]
        row["brain_page"] = fields["brain_page"]
        # Re-load state immediately before save to pick up any concurrent
        # edits (e.g. user dropped a different row). Single-user bot so
        # collision is rare, but the cost is one extra disk read.
        fresh = rl.load_state()
        for i, r in enumerate(fresh["queue"]):
            if rl.normalize_url(r["url"]) == url:
                fresh["queue"][i] = row
                break
        else:
            # Row was dropped during ingest — write the brain page but
            # don't resurrect the bookmark.
            log("row dropped during ingest; brain page kept, bookmark not resurrected")
            return
        rl.save_state(fresh)
        _drain_xref_queue()
        _write_last_bookmark_pointer(row)
        _send_telegram(_format_done_message(row))
        log(f"done: {row['brain_page']}")
    else:
        # Deep ingest failed — leave the placeholder row in place so the
        # user can `migrate bookmarks` later, and tell them.
        _send_telegram(_format_failed_message(row, "ingestor returned no brain page"))
        log("ingest failed; placeholder row left for migrate retry")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        # Best-effort notify even on crash so the user knows the deep
        # ingest didn't silently die.
        try:
            _send_telegram(f"⚠️ Reading-list worker crashed: {str(e)[:200]}")
        except Exception:
            pass
        sys.exit(1)
