#!/usr/bin/env python3
# xref-drainer.py — drains ~/.brain/.xref-queue.jsonl, the entity
# cross-reference work that ingest-x-thread / ingest-youtube /
# ingest-html now defer instead of doing inline.
#
# Why this exists: every ingestor used to make 2-3 gbrain calls per
# extracted entity (ensure stub → link → timeline-add). For a 10-entity
# brain page that's 20-30s of synchronous subprocess fan-out, on top of
# the LLM call and (for thin tweets) browser-harness fetch. Combined
# the pipeline overshot the 120s per-turn timeout, surfaced as the
# bot's "ingest didn't finish, got terminated" apology. By moving the
# cross-ref to a queue + timer-driven drain, the ingestor returns
# quickly and the entity stubs / links / timeline entries get filled
# in over the next minute by this drainer.
#
# Queue file format: one JSON object per line:
#   {"slug": "media/social/x-tobi-20260430",
#    "entities": ["concepts/founder-mode", "people/tobi-lutke"],
#    "source_label": "X thread",
#    "queued_at": "2026-05-13T12:34:56Z"}
#
# Usage:
#   xref-drainer.py --once     # drain everything currently queued
#   xref-drainer.py --watch    # loop with sleep (used by systemd timer)

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BRAIN_DIR = Path.home() / ".brain"
QUEUE_PATH = BRAIN_DIR / ".xref-queue.jsonl"
PROCESSED_PATH = BRAIN_DIR / ".xref-processed.jsonl"
LOCK_PATH = BRAIN_DIR / ".xref-queue.lock"
GBRAIN = "/home/botuser/.bun/bin/gbrain"
TODAY = datetime.now(timezone.utc).date().isoformat()

# Match the PATH shim used by every other script — gbrain's shebang is
# `#!/usr/bin/env bun`, so bun must be on PATH or every call exits 127.
os.environ["PATH"] = "/home/botuser/.bun/bin:" + os.environ.get("PATH", "")


def log(msg):
    print(f"[xref-drainer] {msg}", file=sys.stderr)


def page_exists(slug):
    r = subprocess.run([GBRAIN, "get", slug], capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip().startswith("---")


TYPE_MAP = {
    "people": "person", "companies": "company",
    "concepts": "concept", "projects": "project",
}


def ensure_stub(ent_slug, source_slug, source_label):
    if page_exists(ent_slug):
        return False
    type_prefix = ent_slug.split("/", 1)[0] if "/" in ent_slug else "concept"
    page_type = TYPE_MAP.get(type_prefix, "concept")
    name = ent_slug.split("/", 1)[-1].replace("-", " ").title()
    stub = (
        f"---\ntype: {page_type}\ncreated: {TODAY}\nupdated: {TODAY}\n---\n\n"
        f"# {name}\n\n"
        f"_Auto-created stub from {source_label} ingestion on {TODAY}._ "
        f"First referenced by [[{source_slug}]]. Fill in details on next mention.\n"
    )
    page_path = BRAIN_DIR / f"{ent_slug}.md"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(stub)
    subprocess.run([GBRAIN, "put", ent_slug], input=stub, text=True, capture_output=True, check=False)
    return True


def link_entity(source_slug, ent_slug, source_label):
    """Returns True on full success (link + timeline both wrote)."""
    try:
        ensure_stub(ent_slug, source_slug, source_label)
    except Exception as e:
        log(f"stub creation failed for {ent_slug}: {e}")
        return False
    r1 = subprocess.run([GBRAIN, "link", source_slug, ent_slug], capture_output=True, check=False, text=True)
    r2 = subprocess.run(
        [GBRAIN, "timeline-add", ent_slug, TODAY,
         f"Mentioned in [[{source_slug}]] [Source: {source_label} ingestion {TODAY}]"],
        capture_output=True, check=False, text=True,
    )
    if r1.returncode != 0 or r2.returncode != 0:
        log(f"link/timeline failed for {ent_slug}: link={r1.stdout.strip()[:120]} timeline={r2.stdout.strip()[:120]}")
        return False
    return True


def _acquire_lock():
    """Best-effort cooperative lock so concurrent drainer runs don't
    double-process the queue. The lock is a sentinel file with the
    PID; if the holder is stale we steal it. Not a real fcntl flock
    because the systemd timer + the in-worker invocation may race."""
    try:
        if LOCK_PATH.exists():
            try:
                pid = int(LOCK_PATH.read_text().strip())
                # Steal if the holder is dead
                os.kill(pid, 0)
                # Holder is alive
                return False
            except (ValueError, OSError, ProcessLookupError):
                pass
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOCK_PATH.write_text(str(os.getpid()))
        return True
    except OSError as e:
        log(f"lock acquire failed: {e}")
        return False


def _release_lock():
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass


def enqueue(slug, entities, source_label):
    """Module-callable helper. Ingestors import this instead of doing
    gbrain calls inline."""
    if not entities:
        return
    BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "slug": slug,
        "entities": sorted(set(entities)),
        "source_label": source_label,
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(QUEUE_PATH, "a") as f:
        f.write(json.dumps(payload) + "\n")


def _read_and_clear_queue():
    if not QUEUE_PATH.exists():
        return []
    text = QUEUE_PATH.read_text()
    QUEUE_PATH.write_text("")
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            log(f"skipping malformed queue line: {line[:120]}")
    return items


def _append_processed(item, linked_ok, linked_failed):
    PROCESSED_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        **item,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "linked_ok": linked_ok,
        "linked_failed": linked_failed,
    }
    with open(PROCESSED_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def drain_once():
    if not _acquire_lock():
        log("another drainer holds the lock; exiting")
        return 0
    try:
        items = _read_and_clear_queue()
        if not items:
            return 0
        log(f"draining {len(items)} queued cross-ref batches")
        processed = 0
        for item in items:
            slug = item.get("slug", "")
            entities = item.get("entities", []) or []
            label = item.get("source_label", "ingestion")
            if not slug or not entities:
                continue
            linked_ok = []
            linked_failed = []
            for ent in entities:
                if link_entity(slug, ent, label):
                    linked_ok.append(ent)
                else:
                    linked_failed.append(ent)
            _append_processed(item, linked_ok, linked_failed)
            processed += 1
        # Best-effort gbrain sync after a drain pass so the index
        # reflects the new stubs.
        subprocess.run([GBRAIN, "sync"], capture_output=True, check=False)
        log(f"drained {processed} batches")
        return processed
    finally:
        _release_lock()


def main():
    args = sys.argv[1:]
    if not args or "--once" in args:
        drain_once()
        return
    if "--watch" in args:
        # Used by the systemd timer in long-poll mode if the operator
        # prefers a single long-lived process. Default deploy uses a
        # short-lived --once invocation per timer tick instead.
        while True:
            drain_once()
            time.sleep(60)
        return
    sys.exit("Usage: xref-drainer.py [--once|--watch]")


if __name__ == "__main__":
    main()
