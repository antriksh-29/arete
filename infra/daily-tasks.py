#!/usr/bin/env python3
"""daily-tasks.py — v2 task-centric data model + CLI.

Replaces the v1 bash script's per-date flat state with per-task lineage:

  ~/.brain-bootstrap-raw/state/daily-tasks/v2/
    tasks/<uuid>.json    one logical task with full event history
    daily/<YYYY-MM-DD>.json   pointer list of task_ids for that day
    index.json           derived view: open tasks + title_norm lookup

v1 flat files (~/.brain-bootstrap-raw/state/daily-tasks/<YYYY-MM-DD>.json)
are READ for backward-compat queries but no longer written. Migration via
infra/migrate-daily-tasks-v2.py moves v1 history into v2.

Why Python over bash:
  - relational queries (backlog, stats, history span) need real data
    structures; bash + jq miserable for this shape.
  - argparse gives proper --reply / --date / --on-conflict flag handling
    without the silent-stdin footguns that broke v1 on 2026-05-13.
  - exit codes are explicit and structured (2 = no input, 3 = no
    parseable lines, 4 = collision needs caller decision).

The shell shim at ~/bin/daily-tasks.sh forwards all args here, so cron,
SKILL.md and AGENTS.md references at that path keep working.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Optional


# ── paths + constants ───────────────────────────────────────────

HOME = Path(os.environ.get("HOME", os.path.expanduser("~")))
STATE_ROOT = HOME / ".brain-bootstrap-raw" / "state" / "daily-tasks"
V2_ROOT = STATE_ROOT / "v2"
TASKS_DIR = V2_ROOT / "tasks"
DAILY_DIR = V2_ROOT / "daily"
INDEX_FILE = V2_ROOT / "index.json"

BRAIN_DIR = HOME / ".brain"
BRAIN_PAGE_TODAY = BRAIN_DIR / "habits" / "daily-tasks.md"
BRAIN_PAGE_BACKLOG = BRAIN_DIR / "habits" / "daily-tasks-backlog.md"
BRAIN_PAGE_HISTORY = BRAIN_DIR / "habits" / "daily-tasks-history.md"
BRAIN_PAGE_STATS = BRAIN_DIR / "habits" / "daily-tasks-stats.md"

GBRAIN_BIN = HOME / ".bun" / "bin" / "gbrain"

# Statuses. open is the implicit default for a new task.
STATUS_OPEN = "open"
STATUS_YES = "yes"           # done
STATUS_TOMORROW = "tomorrow"  # carry forward
STATUS_LATER = "later"        # parked (no auto-carry; backlog only)
STATUS_DELETE = "delete"      # removed
TERMINAL_STATUSES = {STATUS_YES, STATUS_DELETE}
OPEN_STATUSES = {STATUS_OPEN, STATUS_TOMORROW, STATUS_LATER}
CARRY_STATUSES = {STATUS_TOMORROW}  # statuses that auto-flow to next day

STATUS_EMOJI = {
    STATUS_OPEN: "•",
    STATUS_YES: "✅",
    STATUS_TOMORROW: "➡️",
    STATUS_LATER: "⏸️",
    STATUS_DELETE: "🗑️",
}

# Status synonyms accepted in `review` and `mark`.
STATUS_ALIASES = {
    "yes": STATUS_YES, "y": STATUS_YES, "done": STATUS_YES,
    "completed": STATUS_YES, "complete": STATUS_YES,
    "tomorrow": STATUS_TOMORROW, "tmrw": STATUS_TOMORROW,
    "defer": STATUS_TOMORROW, "push": STATUS_TOMORROW,
    "later": STATUS_LATER, "park": STATUS_LATER,
    "backlog": STATUS_LATER, "indefinite": STATUS_LATER,
    "no": STATUS_DELETE, "n": STATUS_DELETE, "skip": STATUS_DELETE,
    "delete": STATUS_DELETE, "remove": STATUS_DELETE, "drop": STATUS_DELETE,
}

BACKLOG_AGE_DEFAULT = 7  # task is "in backlog" once it's been open ≥7d

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


# ── small helpers ───────────────────────────────────────────────

def today_str() -> str:
    return dt.datetime.now(IST).strftime("%Y-%m-%d")


def now_iso() -> str:
    return dt.datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S+05:30")


def parse_date(s: str) -> str:
    """Accept YYYY-MM-DD, 'today', 'yesterday', '-N' (N days back)."""
    if not s or s == "today":
        return today_str()
    if s == "yesterday":
        d = dt.datetime.now(IST) - dt.timedelta(days=1)
        return d.strftime("%Y-%m-%d")
    if s.startswith("-") and s[1:].isdigit():
        n = int(s[1:])
        d = dt.datetime.now(IST) - dt.timedelta(days=n)
        return d.strftime("%Y-%m-%d")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    sys.exit(f"Unrecognized date: {s!r} (use YYYY-MM-DD, today, yesterday, or -N)")


def title_norm(s: str) -> str:
    """Normalize a task title for cross-day reuse matching.

    - lowercase
    - strip leading numbering ("1.", "2)", "3 -", etc.)
    - collapse whitespace
    - strip trailing punctuation
    """
    s = s.strip().lower()
    s = re.sub(r"^\d+[.)\-:\s]+", "", s)
    s = re.sub(r"\s+", " ", s)
    s = s.strip(".,;:!? -–—")
    return s


def ensure_dirs() -> None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


def read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


# ── task model ──────────────────────────────────────────────────

def new_task(title: str, on_date: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "title": title,
        "title_norm": title_norm(title),
        "created_date": on_date,
        "created_at": now_iso(),
        "events": [{"date": on_date, "status": STATUS_OPEN, "ts": now_iso()}],
        "current_status": STATUS_OPEN,
        "closed_at": None,
    }


def task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def load_task(task_id: str) -> Optional[dict]:
    return read_json(task_path(task_id))


def save_task(task: dict) -> None:
    atomic_write_json(task_path(task["id"]), task)


def append_event(task: dict, on_date: str, status: str) -> dict:
    task["events"].append({"date": on_date, "status": status, "ts": now_iso()})
    task["current_status"] = status
    if status in TERMINAL_STATUSES:
        task["closed_at"] = now_iso()
    else:
        task["closed_at"] = None
    return task


# ── daily pointer files ─────────────────────────────────────────

def daily_path(date_str: str) -> Path:
    return DAILY_DIR / f"{date_str}.json"


def load_daily(date_str: str) -> dict:
    data = read_json(daily_path(date_str))
    if data is None:
        return {"date": date_str, "task_ids": []}
    return data


def save_daily(daily: dict) -> None:
    atomic_write_json(daily_path(daily["date"]), daily)


# ── index ───────────────────────────────────────────────────────

def rebuild_index() -> dict:
    """Walk all tasks, build {open_tasks: [ids], by_title_norm: {norm: id}}.
    by_title_norm only maps OPEN tasks — closed tasks shouldn't shadow
    a fresh attempt with the same title."""
    open_ids: list[str] = []
    by_norm: dict[str, str] = {}
    if TASKS_DIR.exists():
        for p in TASKS_DIR.glob("*.json"):
            t = read_json(p)
            if not t:
                continue
            if t.get("current_status") in OPEN_STATUSES:
                open_ids.append(t["id"])
                by_norm[t["title_norm"]] = t["id"]
    index = {
        "version": 2,
        "rebuilt_at": now_iso(),
        "open_tasks": open_ids,
        "by_title_norm": by_norm,
    }
    atomic_write_json(INDEX_FILE, index)
    return index


def load_index() -> dict:
    idx = read_json(INDEX_FILE)
    if idx is None:
        return rebuild_index()
    return idx


# ── carryover ───────────────────────────────────────────────────

def carryover_task_ids(for_date: str) -> list[str]:
    """Return task_ids from `for_date - 1` whose current_status is
    `tomorrow`. These auto-flow into `for_date`'s daily list."""
    prev = (dt.datetime.strptime(for_date, "%Y-%m-%d") - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    daily_prev = read_json(daily_path(prev))
    if not daily_prev:
        return []
    carry: list[str] = []
    for tid in daily_prev.get("task_ids", []):
        t = load_task(tid)
        if t and t.get("current_status") == STATUS_TOMORROW:
            carry.append(tid)
    return carry


# ── commands: writes ────────────────────────────────────────────

def cmd_set(args) -> int:
    """Replace today's list. Each new title is matched against the
    open-task index by title_norm; matches REUSE the existing UUID
    (cross-day lineage), non-matches create new tasks. Yesterday's
    `tomorrow` tasks auto-carry into the new daily list.

    When the user's new list overlaps (by title_norm) with a
    carried-over `tomorrow` task, the default behaviour is to EXIT 4
    with a structured collision response so the bot can prompt the
    user. Pass --on-collision merge|keep-both|skip to bypass."""
    titles = read_titles_from_args_or_stdin(args)
    if not titles:
        print("ERROR: set requires at least one task (positional args or stdin)", file=sys.stderr)
        return 2
    return _apply_list(titles, mode="set", date_str=today_str(),
                       no_merge=args.no_merge, on_collision=args.on_collision)


def cmd_add(args) -> int:
    titles = read_titles_from_args_or_stdin(args)
    if not titles:
        print("ERROR: add requires at least one task", file=sys.stderr)
        return 2
    return _apply_list(titles, mode="add", date_str=today_str(),
                       no_merge=args.no_merge, on_collision=args.on_collision)


def read_titles_from_args_or_stdin(args) -> list[str]:
    """Collect task titles from positional args, or stdin if none. If
    a single arg contains commas or newlines, split on those."""
    raw: list[str] = []
    if args.tasks:
        if len(args.tasks) == 1 and ("," in args.tasks[0] or "\n" in args.tasks[0]):
            raw = re.split(r"[,\n]", args.tasks[0])
        else:
            raw = list(args.tasks)
    elif not sys.stdin.isatty():
        raw = sys.stdin.read().splitlines()
    return [t.strip() for t in raw if t.strip()]


def _collision_user_message(collisions: list[tuple[str, str, str]]) -> str:
    """Render a human-readable prompt for the bot to relay to the user.
    The bot reads `user_message` from the JSON envelope and asks the
    user verbatim."""
    if len(collisions) == 1:
        nt, _, ct = collisions[0]
        return (
            f"I already have \"{ct}\" carried over from yesterday's tomorrow list. "
            f"You're trying to add \"{nt}\" which looks like the same thing.\n\n"
            "Reply with one of:\n"
            "• `merge` — same task, just keep yesterday's lineage (recommended)\n"
            "• `keep both` — treat them as separate tasks\n"
            "• `skip` — drop the new one, keep just yesterday's copy"
        )
    lines = [
        f"I have {len(collisions)} tasks carried over from yesterday that look like "
        "duplicates of what you're adding now:",
        "",
    ]
    for nt, _, ct in collisions:
        lines.append(f"  • new: \"{nt}\"   vs   carryover: \"{ct}\"")
    lines += [
        "",
        "Reply with one of:",
        "• `merge` — same tasks, keep yesterday's lineage (recommended)",
        "• `keep both` — treat them all as separate tasks",
        "• `skip` — drop the new ones, keep just yesterday's copies",
    ]
    return "\n".join(lines)


def _apply_list(titles: list[str], mode: str, date_str: str, no_merge: bool,
                on_collision: Optional[str] = None) -> int:
    """Apply a list of titles as either set or add.

    Carryover-collision handling (eng-review O2 path B): when `mode=set`
    and yesterday had `tomorrow`-tagged tasks that title-norm-match an
    incoming title, the default behaviour is to ASK the caller.

    on_collision values:
      None (default) — collisions trigger exit 4 + structured stdout so
                       the bot can prompt the user.
      "merge"         — silently reuse the carried-over UUID (no dup).
      "keep-both"     — let the new title create a fresh task alongside
                        the carried-over one (--no-merge effect for
                        colliding titles only).
      "skip"          — drop the colliding new title; keep just the
                        carried-over copy.
    """
    ensure_dirs()
    index = load_index()
    by_norm = index["by_title_norm"]

    daily = load_daily(date_str)
    existing_ids = list(daily["task_ids"]) if mode == "add" else []

    # Carryover yesterday's tomorrow-tagged tasks BEFORE applying the
    # new list, so we can detect carry-over collisions explicitly.
    carry_ids: list[str] = []
    if mode == "set":
        carry_ids = carryover_task_ids(date_str)
        existing_ids = list(carry_ids)

    # Pre-scan for collisions between incoming titles and carryover tasks.
    # Only `set` mode + non-no-merge produces real collisions worth
    # prompting on.
    collisions: list[tuple[str, str, str]] = []  # (new_title, carry_task_id, carry_title)
    if mode == "set" and not no_merge and carry_ids:
        carry_norm_to_id = {}
        for cid in carry_ids:
            ct = load_task(cid)
            if ct:
                carry_norm_to_id[ct["title_norm"]] = (cid, ct["title"])
        for title in titles:
            norm = title_norm(title)
            if norm in carry_norm_to_id:
                cid, ctitle = carry_norm_to_id[norm]
                collisions.append((title, cid, ctitle))

    # If unresolved collisions exist, ask the caller — exit 4.
    if collisions and on_collision is None:
        out = {
            "status": "collision",
            "date": date_str,
            "collisions": [
                {"new_title": nt, "carryover_task_id": cid[:8], "carryover_title": ct}
                for nt, cid, ct in collisions
            ],
            "next_action": "Re-run with --on-collision <merge|keep-both|skip>",
            "user_message": _collision_user_message(collisions),
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 4

    final_ids: list[str] = list(existing_ids)
    reused_count = 0
    new_count = 0
    carry_collision_count = 0
    collision_norms = {title_norm(nt) for nt, _, _ in collisions}

    for title in titles:
        norm = title_norm(title)
        if not norm:
            continue

        # Apply on_collision strategy for the specific colliding titles.
        # Non-colliding titles flow through the normal match-or-create
        # path below.
        is_collision = norm in collision_norms
        if is_collision:
            if on_collision == "skip":
                # Drop the new title; carryover copy stays in final_ids.
                carry_collision_count += 1
                continue
            if on_collision == "keep-both":
                # Force-create a new task, ignoring the carryover match.
                t = new_task(title, date_str)
                save_task(t)
                final_ids.append(t["id"])
                new_count += 1
                continue
            # on_collision == "merge" (or None and we got past the gate):
            # fall through to normal match-by-norm path which reuses the
            # carryover task's UUID.

        match_id = None if no_merge else by_norm.get(norm)

        if match_id and match_id in final_ids:
            # Already in today's list (via carryover or earlier in this
            # same call). Skip silently — dedupe.
            carry_collision_count += 1
            continue

        if match_id:
            # Cross-day lineage: append a fresh `open` event so the
            # task shows up in today's daily file with a current status
            # of open (not the prior `tomorrow`).
            t = load_task(match_id)
            if t is None:
                # Stale index entry — fall through to create new.
                match_id = None
            else:
                append_event(t, date_str, STATUS_OPEN)
                save_task(t)
                final_ids.append(match_id)
                reused_count += 1
                continue

        # No match (or --no-merge): create a new task.
        t = new_task(title, date_str)
        save_task(t)
        final_ids.append(t["id"])
        new_count += 1

    # Dedup task_ids preserving order (covers the case where `add`
    # accidentally re-adds a task already in the daily list).
    seen: set[str] = set()
    ordered: list[str] = []
    for tid in final_ids:
        if tid not in seen:
            seen.add(tid)
            ordered.append(tid)
    daily["task_ids"] = ordered
    save_daily(daily)
    rebuild_index()
    rebuild_all_brain_pages()

    total = len(daily["task_ids"])
    if mode == "set":
        msg = f"✓ Stored {total} task(s) for {date_str} (replaced any prior list)"
    else:
        msg = f"✓ Added {new_count + reused_count}; {date_str} now has {total} task(s)"
    parts = [msg]
    if reused_count:
        parts.append(f"  ({reused_count} reused from open backlog/tomorrow)")
    if carry_collision_count:
        parts.append(f"  ({carry_collision_count} skipped — already on today's list)")
    print("\n".join(parts))
    return 0


def cmd_remove(args) -> int:
    date_str = today_str()
    daily = load_daily(date_str)
    if not daily["task_ids"]:
        print(f"No tasks for {date_str}.", file=sys.stderr)
        return 1

    target_id = _resolve_task_in_day(daily, args.target)
    if target_id is None:
        print(f"No task matching {args.target!r} in {date_str}", file=sys.stderr)
        return 1

    t = load_task(target_id)
    title = t["title"] if t else target_id
    if t:
        append_event(t, date_str, STATUS_DELETE)
        save_task(t)
    daily["task_ids"] = [x for x in daily["task_ids"] if x != target_id]
    save_daily(daily)
    rebuild_index()
    rebuild_all_brain_pages()
    print(f"✓ Removed: {title}")
    return 0


def cmd_mark(args) -> int:
    date_str = parse_date(args.date) if args.date else today_str()
    daily = load_daily(date_str)
    if not daily["task_ids"]:
        print(f"No state for {date_str}", file=sys.stderr)
        return 1

    target_id = _resolve_task_in_day(daily, args.index)
    if target_id is None:
        print(f"No task at index/match {args.index!r}", file=sys.stderr)
        return 1

    status = STATUS_ALIASES.get(args.status.lower(), args.status.lower())
    if status not in STATUS_EMOJI:
        print(f"Unknown status {args.status!r}. Use: yes|tomorrow|later|delete", file=sys.stderr)
        return 1

    t = load_task(target_id)
    if t is None:
        print(f"Task {target_id} not found", file=sys.stderr)
        return 1
    append_event(t, date_str, status)
    save_task(t)
    rebuild_index()
    rebuild_all_brain_pages()
    print(f"✓ Marked '{t['title']}' as: {status}")
    return 0


def _resolve_task_in_day(daily: dict, target: str) -> Optional[str]:
    """Resolve a 1-based index OR a substring match to a task_id
    from the day's list."""
    ids = daily.get("task_ids", [])
    if not ids:
        return None
    if target.isdigit():
        idx = int(target) - 1
        if 0 <= idx < len(ids):
            return ids[idx]
        return None
    # substring match on title
    target_lc = target.lower()
    matches = []
    for tid in ids:
        t = load_task(tid)
        if t and target_lc in t["title"].lower():
            matches.append(tid)
    if len(matches) == 1:
        return matches[0]
    return None


def cmd_review(args) -> int:
    """Apply a multi-line numbered status reply. Inherits the B1
    regression guards from v1: empty input → exit 2; non-empty input
    with zero parseable status lines → exit 3."""
    date_str = parse_date(args.date) if args.date else today_str()

    # Input resolution mirrors the v1 bash semantics so the bot's
    # invocations don't need to change.
    if args.reply is not None:
        input_text = args.reply  # may be ""
        seen_reply = True
    else:
        seen_reply = False
        if args.rest:
            input_text = " ".join(args.rest)
        elif not sys.stdin.isatty():
            input_text = sys.stdin.read()
        else:
            input_text = ""

    if not input_text.strip():
        print("ERROR: review called with no input. Pass --reply \"<text>\" or pipe a numbered reply via stdin.", file=sys.stderr)
        print("       e.g. daily-tasks.py review --reply \"1. done\\n2. tomorrow\"", file=sys.stderr)
        return 2

    daily = load_daily(date_str)
    if not daily["task_ids"]:
        print(f"No tasks for {date_str}.", file=sys.stderr)
        return 1

    applied = 0
    for line in input_text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\s*(\d+)\s*[.):\-\s]*\s*(.+?)\s*$", line)
        if not m:
            continue
        idx, status_raw = int(m.group(1)) - 1, m.group(2).strip().lower()
        status = STATUS_ALIASES.get(status_raw)
        if not status:
            # try first word of status_raw (e.g. "push to tomorrow")
            first = status_raw.split()[0] if status_raw.split() else ""
            status = STATUS_ALIASES.get(first)
        if not status:
            print(f"  skipping line (unrecognized status): {line}", file=sys.stderr)
            continue
        if not (0 <= idx < len(daily["task_ids"])):
            print(f"  skipping #{idx + 1} (no task at that index for {date_str})", file=sys.stderr)
            continue
        tid = daily["task_ids"][idx]
        t = load_task(tid)
        if t is None:
            continue
        append_event(t, date_str, status)
        save_task(t)
        applied += 1
        print(f"  ✓ #{idx + 1} → {status}: {t['title']}")

    if applied == 0:
        print(f"ERROR: review input contained no parseable numbered status lines for {date_str}.", file=sys.stderr)
        print("       Expected lines like: 1. done   2. tomorrow   3. later   4. skip", file=sys.stderr)
        return 3

    rebuild_index()
    rebuild_all_brain_pages()
    print(f"✓ Applied {applied} status update(s) to {date_str}")
    return 0


# ── commands: reads ─────────────────────────────────────────────

def cmd_show(args) -> int:
    date_str = parse_date(args.date) if args.date else today_str()
    daily = read_json(daily_path(date_str))

    # v2 → v1 fallback so the migration window doesn't show empty.
    if not daily or not daily.get("task_ids"):
        v1 = read_json(STATE_ROOT / f"{date_str}.json")
        if v1 and v1.get("tasks"):
            return _show_v1(v1, date_str)
        if date_str == today_str():
            print(f"📋 No tasks set for today ({date_str}). Share your list when ready.")
        else:
            print(f"📋 No record for {date_str}.")
        return 0

    label = _label_for_date(date_str)
    tasks = [load_task(tid) for tid in daily["task_ids"]]
    tasks = [t for t in tasks if t]

    n = len(tasks)
    counts = _status_counts(tasks)
    summary = _summary_line(counts)
    header = f"📋 {label} — {n} task" + ("s" if n != 1 else "")
    if summary:
        header += f" ({summary})"
    print(header)
    print()
    for i, t in enumerate(tasks, start=1):
        emoji = STATUS_EMOJI.get(t.get("current_status", STATUS_OPEN), "•")
        print(f"{emoji} {i}. {t['title']}")
    return 0


def _show_v1(v1: dict, date_str: str) -> int:
    label = _label_for_date(date_str)
    n = len(v1["tasks"])
    statuses = v1.get("statuses", {})
    print(f"📋 {label} — {n} task" + ("s" if n != 1 else "") + " (v1)")
    print()
    for i, title in enumerate(v1["tasks"], start=1):
        st = statuses.get(title, STATUS_OPEN)
        emoji = STATUS_EMOJI.get(st, "•")
        print(f"{emoji} {i}. {title}")
    return 0


def _label_for_date(date_str: str) -> str:
    if date_str == today_str():
        return "Today"
    yest = (dt.datetime.now(IST) - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    if date_str == yest:
        return "Yesterday"
    return date_str


def _status_counts(tasks: list[dict]) -> dict[str, int]:
    c: dict[str, int] = {}
    for t in tasks:
        st = t.get("current_status", STATUS_OPEN)
        c[st] = c.get(st, 0) + 1
    return c


def _summary_line(counts: dict[str, int]) -> str:
    parts = []
    for st in (STATUS_YES, STATUS_TOMORROW, STATUS_LATER, STATUS_OPEN, STATUS_DELETE):
        n = counts.get(st, 0)
        if n:
            label = {"yes": "done", "tomorrow": "tomorrow", "later": "later",
                     "open": "open", "delete": "deleted"}[st]
            parts.append(f"{n} {label}")
    return " · ".join(parts)


def cmd_carryover(args) -> int:
    """Print yesterday's tomorrow-tagged tasks (for the morning briefing).
    Backward-compatible output shape with v1 — morning briefing reads
    this verbatim."""
    today = today_str()
    yest = (dt.datetime.now(IST) - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    carry: list[str] = []
    parked: list[str] = []
    daily_y = read_json(daily_path(yest))
    if daily_y:
        for tid in daily_y.get("task_ids", []):
            t = load_task(tid)
            if not t:
                continue
            st = t.get("current_status")
            if st == STATUS_TOMORROW:
                carry.append(t["title"])
            elif st == STATUS_LATER:
                parked.append(t["title"])
    if carry:
        print("## Backlog for Today (from yesterday)")
        for title in carry:
            print(f" - {title}")
        print()
    if parked:
        print("## Indefinite Queue")
        for title in parked:
            print(f" - {title}")
        print()
    return 0


def cmd_backlog(args) -> int:
    """List all open tasks (status in {open, tomorrow, later}) aged ≥
    --age-gte days. Oldest first. By default excludes `later` —
    those are explicitly parked, not nagged about."""
    index = load_index()
    today_dt = dt.datetime.strptime(today_str(), "%Y-%m-%d")
    threshold = args.age_gte

    rows = []
    for tid in index["open_tasks"]:
        t = load_task(tid)
        if not t:
            continue
        if t["current_status"] == STATUS_LATER and not args.include_later:
            continue
        if t["current_status"] == STATUS_OPEN:
            # An `open` task on TODAY isn't backlog — it's just today.
            # Only count it if first seen earlier than today.
            pass
        created = dt.datetime.strptime(t["created_date"], "%Y-%m-%d")
        age = (today_dt - created).days
        if age < threshold:
            continue
        rows.append((age, t))

    rows.sort(key=lambda x: -x[0])  # oldest first

    if not rows:
        print(f"📋 No backlog (no tasks open ≥ {threshold} days).")
        return 0

    print(f"📋 Backlog — {len(rows)} task(s) open ≥ {threshold} days")
    print()
    for age, t in rows:
        emoji = STATUS_EMOJI.get(t["current_status"], "•")
        print(f"{emoji} ({age}d) {t['title']}  — since {t['created_date']}")
    return 0


def cmd_history(args) -> int:
    """Print a date-range archive. Format: 'YYYY-MM-DD..YYYY-MM-DD'."""
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", args.span)
    if not m:
        print("Usage: history <YYYY-MM-DD..YYYY-MM-DD>", file=sys.stderr)
        return 1
    start, end = m.group(1), m.group(2)
    s_dt = dt.datetime.strptime(start, "%Y-%m-%d")
    e_dt = dt.datetime.strptime(end, "%Y-%m-%d")
    if e_dt < s_dt:
        print("End date must be ≥ start date", file=sys.stderr)
        return 1

    cur = s_dt
    while cur <= e_dt:
        date_str = cur.strftime("%Y-%m-%d")
        daily = read_json(daily_path(date_str))
        v1 = read_json(STATE_ROOT / f"{date_str}.json")
        if daily and daily.get("task_ids"):
            tasks = [load_task(tid) for tid in daily["task_ids"]]
            tasks = [t for t in tasks if t]
            print(f"━━━ {date_str} — {len(tasks)} task(s) ━━━")
            for i, t in enumerate(tasks, start=1):
                emoji = STATUS_EMOJI.get(t.get("current_status", STATUS_OPEN), "•")
                print(f"{emoji} {i}. {t['title']}")
            print()
        elif v1 and v1.get("tasks"):
            print(f"━━━ {date_str} — {len(v1['tasks'])} task(s) (v1) ━━━")
            for i, title in enumerate(v1["tasks"], start=1):
                st = v1.get("statuses", {}).get(title, STATUS_OPEN)
                emoji = STATUS_EMOJI.get(st, "•")
                print(f"{emoji} {i}. {title}")
            print()
        cur += dt.timedelta(days=1)
    return 0


def cmd_stats(args) -> int:
    """Roll up completion / push / drop stats over a window."""
    window = args.window
    days = {"week": 7, "month": 30, "all": 9999}[window]
    cutoff = (dt.datetime.now(IST) - dt.timedelta(days=days)).date()

    # Count UNIQUE tasks first seen in window, by current_status.
    counts = {STATUS_YES: 0, STATUS_TOMORROW: 0, STATUS_LATER: 0,
              STATUS_OPEN: 0, STATUS_DELETE: 0}
    total = 0
    if TASKS_DIR.exists():
        for p in TASKS_DIR.glob("*.json"):
            t = read_json(p)
            if not t:
                continue
            created = dt.datetime.strptime(t["created_date"], "%Y-%m-%d").date()
            if created < cutoff:
                continue
            st = t.get("current_status", STATUS_OPEN)
            counts[st] = counts.get(st, 0) + 1
            total += 1

    if total == 0:
        print(f"📊 Stats ({window}): no tasks in window.")
        return 0

    done = counts[STATUS_YES]
    pct = (100.0 * done) / total if total else 0
    label = {"week": "last 7 days", "month": "last 30 days", "all": "all-time"}[window]
    print(f"📊 Stats — {label} ({total} task(s))")
    print()
    for st in (STATUS_YES, STATUS_TOMORROW, STATUS_LATER, STATUS_OPEN, STATUS_DELETE):
        n = counts.get(st, 0)
        if n:
            emoji = STATUS_EMOJI[st]
            name = {"yes": "Done", "tomorrow": "Tomorrow", "later": "Parked",
                    "open": "Still open", "delete": "Deleted"}[st]
            print(f"  {emoji} {name}: {n}")
    print()
    print(f"  Completion: {done}/{total} ({pct:.0f}%)")
    return 0


def cmd_task(args) -> int:
    """Detail view + full lineage for a task by uuid-prefix or substring."""
    matches: list[dict] = []
    if TASKS_DIR.exists():
        prefix = args.target.lower()
        for p in TASKS_DIR.glob("*.json"):
            t = read_json(p)
            if not t:
                continue
            if t["id"].startswith(prefix) or prefix in t["title"].lower():
                matches.append(t)

    if not matches:
        print(f"No task matching {args.target!r}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"Ambiguous — {len(matches)} matches:")
        for t in matches:
            print(f"  - {t['id'][:8]}  {t['title']}")
        return 1

    t = matches[0]
    emoji = STATUS_EMOJI.get(t["current_status"], "•")
    print(f"{emoji} {t['title']}")
    print(f"  id:          {t['id']}")
    print(f"  status:      {t['current_status']}")
    print(f"  created:     {t['created_date']} ({t['created_at']})")
    if t.get("closed_at"):
        print(f"  closed:      {t['closed_at']}")
    print(f"  events:")
    for ev in t["events"]:
        em = STATUS_EMOJI.get(ev["status"], "•")
        print(f"    {em} {ev['date']}  {ev['status']:10}  ({ev['ts']})")
    return 0


def cmd_status(args) -> int:
    """Raw JSON for debugging today's state."""
    daily = load_daily(today_str())
    tasks = [load_task(tid) for tid in daily.get("task_ids", [])]
    out = {"daily": daily, "tasks": [t for t in tasks if t]}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_rebuild_index(args) -> int:
    idx = rebuild_index()
    print(f"✓ Rebuilt index — {len(idx['open_tasks'])} open task(s)")
    return 0


def cmd_rebuild_brain(args) -> int:
    rebuild_all_brain_pages(force=True)
    print(f"✓ Rebuilt brain pages")
    return 0


# ── brain page rendering ────────────────────────────────────────

def _content_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _write_brain_page(path: Path, content: str, slug: str, force: bool = False) -> bool:
    """Write only if content changed. Returns True if file changed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not force and path.exists():
        existing = path.read_text()
        if _content_hash(existing) == _content_hash(content):
            return False
    path.write_text(content)
    # Best-effort gbrain put. Skip silently on failure.
    if GBRAIN_BIN.exists():
        try:
            subprocess.run(
                [str(GBRAIN_BIN), "put", slug],
                input=content,
                text=True,
                check=False,
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass
    return True


def rebuild_all_brain_pages(force: bool = False) -> None:
    changed = []
    if _write_brain_page(BRAIN_PAGE_TODAY, _render_today_page(), "habits/daily-tasks", force):
        changed.append(BRAIN_PAGE_TODAY)
    if _write_brain_page(BRAIN_PAGE_BACKLOG, _render_backlog_page(), "habits/daily-tasks-backlog", force):
        changed.append(BRAIN_PAGE_BACKLOG)
    if _write_brain_page(BRAIN_PAGE_HISTORY, _render_history_page(), "habits/daily-tasks-history", force):
        changed.append(BRAIN_PAGE_HISTORY)
    if _write_brain_page(BRAIN_PAGE_STATS, _render_stats_page(), "habits/daily-tasks-stats", force):
        changed.append(BRAIN_PAGE_STATS)

    if changed:
        _git_commit_brain([p.relative_to(BRAIN_DIR) for p in changed if p.is_relative_to(BRAIN_DIR)])


def _render_today_page() -> str:
    today = today_str()
    daily = read_json(daily_path(today)) or {"date": today, "task_ids": []}
    tasks = [load_task(tid) for tid in daily.get("task_ids", [])]
    tasks = [t for t in tasks if t]
    counts = _status_counts(tasks)
    summary = _summary_line(counts)

    lines = [
        "---",
        "type: habit",
        "title: Daily Tasks (today's list, what's on my plate)",
        "name: Daily Tasks",
        f"updated: {today}",
        "tags: [daily, productivity, tasks, todo, today]",
        "aliases: [todays-tasks, my-tasks, whats-on-my-plate, daily-todo]",
        "---",
        "",
        "# Daily Tasks — today's list (whats on my plate, my tasks today)",
        "",
        "What I'm working on today. Use for *what's on my plate*, *whats on my "
        "plate*, *my tasks today*, *today's task list*, *share today's tasks*, "
        "*what am I working on*.",
        "",
        "Source of truth: `~/bin/daily-tasks.sh show`. Per-task lineage at",
        "`~/.brain-bootstrap-raw/state/daily-tasks/v2/tasks/<uuid>.json`.",
        "",
        f"## Today ({today})",
        "",
    ]
    if tasks:
        lines.append(f"{len(tasks)} task(s)" + (f" — {summary}" if summary else ""))
        lines.append("")
        for i, t in enumerate(tasks, start=1):
            emoji = STATUS_EMOJI.get(t.get("current_status", STATUS_OPEN), "•")
            lines.append(f"- {emoji} {i}. {t['title']}")
    else:
        lines.append("_No tasks set yet._")

    lines.append("")
    lines.append("## Last 7 days")
    lines.append("")
    today_dt = dt.datetime.strptime(today, "%Y-%m-%d")
    for delta in range(1, 8):
        d = (today_dt - dt.timedelta(days=delta)).strftime("%Y-%m-%d")
        daily_d = read_json(daily_path(d))
        if daily_d and daily_d.get("task_ids"):
            ts = [load_task(tid) for tid in daily_d["task_ids"]]
            ts = [t for t in ts if t]
            c = _status_counts(ts)
            s = _summary_line(c)
            lines.append(f"- **{d}**: {len(ts)} task(s)" + (f" — {s}" if s else ""))
        else:
            v1 = read_json(STATE_ROOT / f"{d}.json")
            if v1 and v1.get("tasks"):
                lines.append(f"- **{d}**: {len(v1['tasks'])} task(s) (v1)")
    return "\n".join(lines) + "\n"


def _render_backlog_page() -> str:
    index = load_index()
    today_dt = dt.datetime.strptime(today_str(), "%Y-%m-%d")
    rows = []
    for tid in index["open_tasks"]:
        t = load_task(tid)
        if not t:
            continue
        created = dt.datetime.strptime(t["created_date"], "%Y-%m-%d")
        age = (today_dt - created).days
        if age >= BACKLOG_AGE_DEFAULT:
            rows.append((age, t))
    rows.sort(key=lambda x: -x[0])

    # Lead paragraph is intentionally keyword-dense so gbrain semantic
    # search ranks this page for queries like "pending tasks", "what's
    # open", "stale tasks", "stuck items".
    lines = [
        "---",
        "type: habit",
        "title: Daily Tasks — Backlog (pending, open, stale, stuck tasks)",
        "name: Daily Tasks Backlog",
        f"updated: {today_str()}",
        "tags: [daily, productivity, backlog, pending, open, stale, stuck]",
        "aliases: [pending-tasks, open-tasks, stale-tasks, stuck-tasks, my-backlog]",
        "---",
        "",
        "# Daily Tasks — Backlog (pending tasks, open tasks, stale tasks, stuck tasks)",
        "",
        "This page lists my **pending tasks** / **open tasks** / **stale tasks** / **stuck tasks** —",
        f"things still to be done that have been open ≥ {BACKLOG_AGE_DEFAULT} days. "
        "Use this for queries like *what's pending from my end*, *my backlog*, "
        "*what's stuck*, *whats stuck*, *aged tasks*, *open tasks from earlier "
        "this week/month*.",
        "",
        f"Source of truth: `~/bin/daily-tasks.sh backlog`. Re-run that script for live numbers.",
        "",
        "## Backlog (oldest first)",
        "",
    ]
    if not rows:
        lines.append("_Backlog empty — nothing aged past threshold._")
    else:
        for age, t in rows:
            emoji = STATUS_EMOJI.get(t["current_status"], "•")
            lines.append(f"- {emoji} **{t['title']}** — {age}d open (since {t['created_date']})")
    return "\n".join(lines) + "\n"


def _render_history_page() -> str:
    lines = [
        "---",
        "type: habit",
        "title: Daily Tasks — History (archive, past tasks, what I did)",
        "name: Daily Tasks History",
        f"updated: {today_str()}",
        "tags: [daily, productivity, history, archive, past]",
        "aliases: [task-history, past-tasks, task-archive, what-i-did]",
        "---",
        "",
        "# Daily Tasks — History archive",
        "",
        "Past task lists, reverse-chronological. Use for *what did I do "
        "yesterday/last week/last month*, *tasks from <date>*, *task archive*, "
        "*history of my tasks*.",
        "",
        "Source of truth: `~/bin/daily-tasks.sh history <start>..<end>`.",
        "",
    ]
    # Collect all dates from v2 daily + v1 flat
    dates: set[str] = set()
    if DAILY_DIR.exists():
        for p in DAILY_DIR.glob("*.json"):
            dates.add(p.stem)
    if STATE_ROOT.exists():
        for p in STATE_ROOT.glob("*.json"):
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem):
                dates.add(p.stem)
    for date_str in sorted(dates, reverse=True):
        daily = read_json(daily_path(date_str))
        if daily and daily.get("task_ids"):
            tasks = [load_task(tid) for tid in daily["task_ids"]]
            tasks = [t for t in tasks if t]
            c = _status_counts(tasks)
            s = _summary_line(c)
            lines.append(f"## {date_str} — {len(tasks)} task(s)" + (f" ({s})" if s else ""))
            lines.append("")
            for i, t in enumerate(tasks, start=1):
                emoji = STATUS_EMOJI.get(t.get("current_status", STATUS_OPEN), "•")
                lines.append(f"- {emoji} {i}. {t['title']}")
            lines.append("")
        else:
            v1 = read_json(STATE_ROOT / f"{date_str}.json")
            if v1 and v1.get("tasks"):
                lines.append(f"## {date_str} — {len(v1['tasks'])} task(s) (v1)")
                lines.append("")
                for i, title in enumerate(v1["tasks"], start=1):
                    st = v1.get("statuses", {}).get(title, STATUS_OPEN)
                    emoji = STATUS_EMOJI.get(st, "•")
                    lines.append(f"- {emoji} {i}. {title}")
                lines.append("")
    return "\n".join(lines) + "\n"


def _render_stats_page() -> str:
    today_dt = dt.datetime.now(IST)
    week_cutoff = (today_dt - dt.timedelta(days=7)).date()
    month_cutoff = (today_dt - dt.timedelta(days=30)).date()

    week_counts: dict[str, int] = {}
    month_counts: dict[str, int] = {}
    all_counts: dict[str, int] = {}
    if TASKS_DIR.exists():
        for p in TASKS_DIR.glob("*.json"):
            t = read_json(p)
            if not t:
                continue
            created = dt.datetime.strptime(t["created_date"], "%Y-%m-%d").date()
            st = t.get("current_status", STATUS_OPEN)
            all_counts[st] = all_counts.get(st, 0) + 1
            if created >= month_cutoff:
                month_counts[st] = month_counts.get(st, 0) + 1
            if created >= week_cutoff:
                week_counts[st] = week_counts.get(st, 0) + 1

    def fmt(name: str, c: dict[str, int]) -> list[str]:
        total = sum(c.values())
        done = c.get(STATUS_YES, 0)
        pct = (100.0 * done) / total if total else 0
        out = [f"### {name} ({total} task(s))"]
        if total == 0:
            out.append("_No tasks in window._")
        else:
            for st in (STATUS_YES, STATUS_TOMORROW, STATUS_LATER, STATUS_OPEN, STATUS_DELETE):
                n = c.get(st, 0)
                if n:
                    emoji = STATUS_EMOJI[st]
                    label = {"yes": "Done", "tomorrow": "Tomorrow", "later": "Parked",
                             "open": "Still open", "delete": "Deleted"}[st]
                    out.append(f"- {emoji} {label}: {n}")
            out.append(f"- **Completion**: {done}/{total} ({pct:.0f}%)")
        out.append("")
        return out

    lines = [
        "---",
        "type: habit",
        "title: Daily Tasks — Stats (completion rate, productivity metrics)",
        "name: Daily Tasks Stats",
        f"updated: {today_str()}",
        "tags: [daily, productivity, stats, completion, metrics]",
        "aliases: [task-stats, completion-rate, productivity-stats]",
        "---",
        "",
        "# Daily Tasks — Stats",
        "",
        "Completion rate + push/drop breakdown. Use for *how many tasks did I "
        "close this week/month*, *task completion rate*, *productivity stats*.",
        "",
        "Source of truth: `~/bin/daily-tasks.sh stats --window week|month|all`.",
        "",
    ]
    lines += fmt("Last 7 days", week_counts)
    lines += fmt("Last 30 days", month_counts)
    lines += fmt("All-time", all_counts)
    return "\n".join(lines) + "\n"


def _git_commit_brain(rel_paths: list[Path]) -> None:
    """Commit + push only if there are real changes. Best-effort."""
    if not (BRAIN_DIR / ".git").exists():
        return
    try:
        for rel in rel_paths:
            subprocess.run(
                ["git", "-C", str(BRAIN_DIR), "add", str(rel)],
                check=False, capture_output=True, timeout=10,
            )
        diff = subprocess.run(
            ["git", "-C", str(BRAIN_DIR), "diff", "--cached", "--quiet"],
            capture_output=True, timeout=10,
        )
        if diff.returncode == 0:
            return  # no real changes
        subprocess.run(
            ["git", "-C", str(BRAIN_DIR),
             "-c", "user.email=alfred@bot",
             "-c", "user.name=Alfred",
             "commit", "-m", f"daily-tasks: state update {today_str()}"],
            check=False, capture_output=True, timeout=15,
        )
        # Push best-effort; cron / openclaw don't have shell on push errors.
        log = subprocess.run(
            ["git", "-C", str(BRAIN_DIR), "log", "origin/main..HEAD", "--oneline"],
            capture_output=True, text=True, timeout=10,
        )
        if log.stdout.strip():
            subprocess.run(
                ["git", "-C", str(BRAIN_DIR), "push", "origin", "main"],
                check=False, capture_output=True, timeout=30,
            )
    except Exception as e:
        print(f"[daily-tasks] WARN: brain git push failed: {e}", file=sys.stderr)


# ── CLI ────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="daily-tasks", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=False)

    # --smoke (top-level)
    p.add_argument("--smoke", action="store_true",
                   help="cold-boot callability check, no writes")

    p_set = sub.add_parser("set", help="replace today's list")
    p_set.add_argument("tasks", nargs="*")
    p_set.add_argument("--no-merge", action="store_true",
                       help="don't reuse UUIDs across days even on title match")
    p_set.add_argument("--on-collision", choices=["merge", "keep-both", "skip"],
                       default=None,
                       help="strategy when new title overlaps a carried-over task")
    p_set.set_defaults(func=cmd_set)

    p_add = sub.add_parser("add", help="append to today's list")
    p_add.add_argument("tasks", nargs="*")
    p_add.add_argument("--no-merge", action="store_true")
    p_add.add_argument("--on-collision", choices=["merge", "keep-both", "skip"],
                       default=None,
                       help="strategy when new title overlaps a carried-over task")
    p_add.set_defaults(func=cmd_add)

    p_remove = sub.add_parser("remove", aliases=["rm", "delete"], help="remove a task")
    p_remove.add_argument("target", help="1-based index or substring")
    p_remove.set_defaults(func=cmd_remove)

    p_mark = sub.add_parser("mark", help="set a task's status")
    p_mark.add_argument("--date", help="target date (default: today)")
    p_mark.add_argument("index", help="1-based index or substring")
    p_mark.add_argument("status", help="yes|tomorrow|later|delete")
    p_mark.set_defaults(func=cmd_mark)

    p_rev = sub.add_parser("review", help="apply a numbered-status reply to a day")
    p_rev.add_argument("--date", help="target date (default: today)")
    p_rev.add_argument("--reply", default=None,
                       help="inline reply text (preferred over stdin)")
    p_rev.add_argument("rest", nargs=argparse.REMAINDER)
    p_rev.set_defaults(func=cmd_review)

    p_show = sub.add_parser("show", aliases=["today", "list"], help="show tasks for a date")
    p_show.add_argument("date", nargs="?", default=None,
                        help="YYYY-MM-DD | today | yesterday | -N")
    p_show.set_defaults(func=cmd_show)

    p_carry = sub.add_parser("carryover", help="yesterday's tomorrow/later (for morning briefing)")
    p_carry.set_defaults(func=cmd_carryover)

    p_back = sub.add_parser("backlog", help="open tasks aged ≥ N days")
    p_back.add_argument("--age-gte", type=int, default=BACKLOG_AGE_DEFAULT)
    p_back.add_argument("--include-later", action="store_true",
                        help="include `later`-parked tasks (default: hidden)")
    p_back.set_defaults(func=cmd_backlog)

    p_hist = sub.add_parser("history", help="archive across a date span")
    p_hist.add_argument("span", help="YYYY-MM-DD..YYYY-MM-DD")
    p_hist.set_defaults(func=cmd_history)

    p_stat = sub.add_parser("stats", help="completion / push / drop rollup")
    p_stat.add_argument("--window", choices=["week", "month", "all"], default="week")
    p_stat.set_defaults(func=cmd_stats)

    p_task = sub.add_parser("task", help="detail view + lineage for a task")
    p_task.add_argument("target", help="uuid-prefix or title substring")
    p_task.set_defaults(func=cmd_task)

    p_status = sub.add_parser("status", help="raw JSON for today (debug)")
    p_status.set_defaults(func=cmd_status)

    p_rebuild_idx = sub.add_parser("rebuild-index", help="force index rebuild")
    p_rebuild_idx.set_defaults(func=cmd_rebuild_index)

    p_rebuild_brain = sub.add_parser("rebuild-brain", help="force brain page rebuild")
    p_rebuild_brain.set_defaults(func=cmd_rebuild_brain)

    args = p.parse_args(argv)

    if args.smoke:
        print(json.dumps({
            "ok": True,
            "skill": "daily-tasks",
            "user_message": "daily-tasks smoke ok",
            "version": 2,
        }))
        return 0

    if not getattr(args, "func", None):
        p.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
