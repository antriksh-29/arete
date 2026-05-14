#!/usr/bin/env python3
"""migrate-daily-tasks-v2.py — migrate v1 flat state to v2 per-task lineage.

v1 layout (per-date flat):
  ~/.brain-bootstrap-raw/state/daily-tasks/<YYYY-MM-DD>.json
    {date, tasks: [title], statuses: {title: status}}

v2 layout (task-centric):
  ~/.brain-bootstrap-raw/state/daily-tasks/v2/
    tasks/<uuid>.json     full lineage per logical task
    daily/<YYYY-MM-DD>.json   pointer list per day
    index.json            derived view of open tasks + title_norm

Migration algorithm:
1. Walk all v1 files chronologically.
2. For each task title on each day, normalize. If title_norm already
   exists in the in-memory map AND that task is still open at the time
   of this day → reuse UUID (cross-day lineage). Otherwise create a new
   UUID.
3. Append an event for that day with the recorded status (default
   `open` if no status).
4. Update open-task map: if final status is terminal (yes/delete),
   remove from map; otherwise keep mapped under title_norm.
5. Write task files + daily files + index.

Modes:
  --dry-run   print the proposed mapping table, no writes.
  --apply     write v2 layout + archive v1 files to .v1-backup-<ts>/.
  --rollback <timestamp>   restore v1 from a specific backup.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sys
import uuid
from pathlib import Path

# Reuse the runtime module's constants + helpers — keeps title_norm
# semantically identical to the live script.
sys.path.insert(0, str(Path(__file__).parent))
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("dt_mod", Path(__file__).parent / "daily-tasks.py")
    dt_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dt_mod)
except Exception as e:
    print(f"ERROR: failed to import daily-tasks.py for shared helpers: {e}", file=sys.stderr)
    sys.exit(1)

STATE_ROOT = dt_mod.STATE_ROOT
V2_ROOT = dt_mod.V2_ROOT
TASKS_DIR = dt_mod.TASKS_DIR
DAILY_DIR = dt_mod.DAILY_DIR
INDEX_FILE = dt_mod.INDEX_FILE
title_norm = dt_mod.title_norm
now_iso = dt_mod.now_iso
STATUS_ALIASES = dt_mod.STATUS_ALIASES
STATUS_EMOJI = dt_mod.STATUS_EMOJI  # canonical status set is the keys

TERMINAL = {"yes", "delete"}
OPEN_LIKE = {"open", "tomorrow", "later"}


def normalize_status(raw: str) -> str:
    """Map any v1 status string to a canonical v2 status. Unknown
    inputs fall through to `open` rather than crashing the migration."""
    raw = (raw or "").strip().lower()
    if raw in STATUS_EMOJI:  # already canonical
        return raw
    if raw in STATUS_ALIASES:
        return STATUS_ALIASES[raw]
    # Try first word (e.g. "push to tomorrow" → "push" → "tomorrow")
    first = raw.split()[0] if raw.split() else ""
    if first in STATUS_ALIASES:
        return STATUS_ALIASES[first]
    return "open"


def list_v1_files() -> list[Path]:
    if not STATE_ROOT.exists():
        return []
    out: list[Path] = []
    for p in STATE_ROOT.glob("*.json"):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem):
            out.append(p)
    out.sort(key=lambda p: p.stem)
    return out


def dry_run() -> int:
    files = list_v1_files()
    if not files:
        print("No v1 state files found.")
        return 0
    print(f"Found {len(files)} v1 file(s):")
    for p in files:
        print(f"  {p.name}")
    print()

    # Simulate the migration to build the mapping table.
    mapping, tasks_by_id = _simulate(files)
    _print_mapping(files, mapping, tasks_by_id)
    print()
    print(f"=== Summary ===")
    print(f"v1 (task, date) entries  → {sum(len(m) for m in mapping.values())}")
    print(f"v2 unique tasks created  → {len(tasks_by_id)}")
    reused = sum(1 for m in mapping.values() for entry in m if entry["reused"])
    print(f"  cross-day reuses         → {reused}")
    print()
    print("Run with --apply to write v2 + archive v1.")
    return 0


def apply() -> int:
    files = list_v1_files()
    if not files:
        print("No v1 state files found — nothing to migrate.")
        return 0

    # Refuse to apply if v2 already has data (would create double-counted
    # lineage). User must --rollback first.
    if TASKS_DIR.exists() and any(TASKS_DIR.iterdir()):
        print("ERROR: v2 tasks/ already populated. To re-run migration:", file=sys.stderr)
        print(f"  1. archive existing v2: mv {V2_ROOT} {V2_ROOT}.bak-$(date +%s)", file=sys.stderr)
        print(f"  2. re-run --apply", file=sys.stderr)
        return 2

    mapping, tasks_by_id = _simulate(files)

    # 1. Write task files
    dt_mod.ensure_dirs()
    for task_id, task in tasks_by_id.items():
        dt_mod.atomic_write_json(dt_mod.task_path(task_id), task)

    # 2. Write daily files
    for p in files:
        date_str = p.stem
        entries = mapping.get(date_str, [])
        dt_mod.atomic_write_json(
            dt_mod.daily_path(date_str),
            {"date": date_str, "task_ids": [e["task_id"] for e in entries]},
        )

    # 3. Rebuild index
    dt_mod.rebuild_index()

    # 4. Archive v1 files
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = STATE_ROOT / f".v1-backup-{ts}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for p in files:
        shutil.move(str(p), str(backup_dir / p.name))

    # 5. Rebuild brain pages
    dt_mod.rebuild_all_brain_pages(force=True)

    print(f"✓ Migrated {len(files)} v1 file(s) → {len(tasks_by_id)} v2 task(s)")
    print(f"✓ Archived v1 files to: {backup_dir}")
    print(f"✓ Brain pages rebuilt.")
    print(f"\nRollback: python3 {Path(__file__).name} --rollback {ts}")
    return 0


def rollback(ts: str) -> int:
    backup_dir = STATE_ROOT / f".v1-backup-{ts}"
    if not backup_dir.exists():
        print(f"ERROR: backup dir not found: {backup_dir}", file=sys.stderr)
        return 1
    # Restore v1 files
    restored = 0
    for p in backup_dir.glob("*.json"):
        target = STATE_ROOT / p.name
        if target.exists():
            print(f"  skip (already exists): {target.name}")
            continue
        shutil.copy(str(p), str(target))
        restored += 1
    # Remove v2 dir entirely (caller can manually mv if they want to keep it)
    if V2_ROOT.exists():
        archive = V2_ROOT.with_name(f"v2.archived-{ts}")
        shutil.move(str(V2_ROOT), str(archive))
        print(f"  v2 archived to: {archive}")
    print(f"✓ Restored {restored} v1 file(s) from {backup_dir}")
    print(f"  Backup dir preserved at: {backup_dir}")
    return 0


def _simulate(files: list[Path]) -> tuple[dict, dict]:
    """Return (mapping, tasks_by_id).

    mapping[date_str] = list of {task_id, title, status, reused}.
    tasks_by_id[uuid] = the final task dict.
    """
    open_by_norm: dict[str, str] = {}  # title_norm → task_id (still open)
    mapping: dict[str, list] = {}
    tasks_by_id: dict[str, dict] = {}

    for p in files:
        date_str = p.stem
        try:
            v1 = json.loads(p.read_text())
        except Exception as e:
            print(f"  WARN: failed to parse {p}: {e}", file=sys.stderr)
            continue
        statuses = v1.get("statuses", {})
        day_entries = []
        for title in v1.get("tasks", []):
            norm = title_norm(title)
            existing_id = open_by_norm.get(norm)

            if existing_id and existing_id in tasks_by_id:
                # Reuse UUID. Append a fresh `open` event for THIS day.
                task = tasks_by_id[existing_id]
                _append_event(task, date_str, "open")
                reused = True
                task_id = existing_id
            else:
                task_id = str(uuid.uuid4())
                task = {
                    "id": task_id,
                    "title": title,
                    "title_norm": norm,
                    "created_date": date_str,
                    "created_at": _date_to_ts(date_str),
                    "events": [{"date": date_str, "status": "open", "ts": _date_to_ts(date_str)}],
                    "current_status": "open",
                    "closed_at": None,
                }
                tasks_by_id[task_id] = task
                reused = False

            # Now apply the recorded status (if any) for this day,
            # normalizing v1 aliases (`done` → `yes`, `defer` →
            # `tomorrow`, etc.) so v2 only ever stores canonical
            # statuses.
            raw_status = statuses.get(title)
            if raw_status:
                norm_status = normalize_status(raw_status)
                if norm_status != "open":
                    _append_event(task, date_str, norm_status)

            # Update open-map
            if task["current_status"] in TERMINAL:
                open_by_norm.pop(norm, None)
            else:
                open_by_norm[norm] = task_id

            day_entries.append({
                "task_id": task_id,
                "title": title,
                "status": task["current_status"],
                "reused": reused,
            })
        mapping[date_str] = day_entries
    return mapping, tasks_by_id


def _append_event(task: dict, date_str: str, status: str) -> None:
    task["events"].append({"date": date_str, "status": status, "ts": _date_to_ts(date_str)})
    task["current_status"] = status
    if status in TERMINAL:
        task["closed_at"] = _date_to_ts(date_str)
    else:
        task["closed_at"] = None


def _date_to_ts(date_str: str) -> str:
    # Synthetic timestamp for historical events: noon IST of that day.
    return f"{date_str}T12:00:00+05:30"


def _print_mapping(files, mapping, tasks_by_id) -> None:
    print("=== Mapping table (eyeball this before --apply) ===")
    print()
    for p in files:
        date_str = p.stem
        entries = mapping.get(date_str, [])
        print(f"━━━ {date_str} — {len(entries)} task(s) ━━━")
        for e in entries:
            tag = "REUSE" if e["reused"] else "NEW  "
            print(f"  [{tag}] {e['task_id'][:8]}  status={e['status']:9}  {e['title']}")
        print()


def main() -> int:
    p = argparse.ArgumentParser(prog="migrate-daily-tasks-v2", description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--rollback", metavar="TIMESTAMP",
                   help="restore v1 from .v1-backup-<TIMESTAMP>/")
    args = p.parse_args()
    if args.dry_run:
        return dry_run()
    if args.apply:
        return apply()
    if args.rollback:
        return rollback(args.rollback)
    return 1


if __name__ == "__main__":
    sys.exit(main())
