"""Unit tests for the reading-list async add path (introduced 2026-05-13).

Guards two contracts:

1. `cmd_add` returns within a second with `pending: true` and a
   📥-prefixed ack message — never waits for the deep ingestor.
2. A detached worker is spawned exactly once for the URL.

The worker itself (`reading-list-worker.py`) is tested by giving it a
stub `run_ingestor` and asserting it updates the row, then fires the
Telegram follow-up.

Pinning READING_LIST_SYNC=1 still works for smoke tests — covered too.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
RL_SCRIPT = REPO / "infra" / "reading-list.py"
WORKER_SCRIPT = REPO / "infra" / "reading-list-worker.py"


def _import_reading_list():
    spec = importlib.util.spec_from_file_location("reading_list_async", RL_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reading_list_async"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _import_worker():
    spec = importlib.util.spec_from_file_location("reading_list_worker", WORKER_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reading_list_worker"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# cmd_add — async path
# ---------------------------------------------------------------------------

def test_cmd_add_async_returns_ack_without_running_ingestor(tmp_path, monkeypatch):
    rl = _import_reading_list()
    # Isolate brain dir & list path to a tmp tree
    list_path = tmp_path / "lists" / "to-read.md"
    monkeypatch.setattr(rl, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(rl, "LIST_PATH", list_path)
    # No git, no gbrain — save_state degrades gracefully
    monkeypatch.setattr(rl, "git_commit", lambda: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))

    # Stub run_ingestor so a leak would be obvious
    run_calls = []
    def explode(*a, **k):
        run_calls.append((a, k))
        raise AssertionError("run_ingestor must NOT run on the async path")
    monkeypatch.setattr(rl, "run_ingestor", explode)

    # Capture Popen so we can prove the worker was launched (without actually forking)
    popen_calls = []
    class FakePopen:
        def __init__(self, *args, **kwargs):
            popen_calls.append({"args": list(args[0]) if args else kwargs.get("args"), "kwargs": kwargs})
    monkeypatch.setattr(rl.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(os.path, "isfile", lambda p: p == rl.WORKER_BIN)

    # Capture stdout
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)

    url = "https://x.com/tobi/status/2053121182044451016"
    rl.cmd_add(url)

    out = buf.getvalue().strip()
    env = json.loads(out)

    assert env["ok"] is True
    assert env.get("pending") is True
    assert env["url"] == url
    assert env["title"].startswith("(saving")
    assert env["posted_by"] == "@tobi"
    assert "📥" in env["user_message"]
    assert "follow-up" in env["user_message"]
    # Critical: ingestor was never invoked
    assert run_calls == []
    # Worker was spawned exactly once for this URL
    assert len(popen_calls) == 1
    call = popen_calls[0]
    assert call["args"][0] == "python3"
    assert call["args"][1] == rl.WORKER_BIN
    assert call["args"][2] == url
    assert call["kwargs"].get("start_new_session") is True


def test_cmd_add_async_writes_placeholder_row_with_handle(tmp_path, monkeypatch):
    """The pending row must be visible in `show` immediately so the user
    can see what's in-flight (and so subsequent `add` of the same URL
    deduplicates correctly while the worker is still running)."""
    rl = _import_reading_list()
    list_path = tmp_path / "lists" / "to-read.md"
    monkeypatch.setattr(rl, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(rl, "LIST_PATH", list_path)
    monkeypatch.setattr(rl, "git_commit", lambda: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(rl.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(os.path, "isfile", lambda p: p == rl.WORKER_BIN)
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    url = "https://x.com/tobi/status/2053121182044451016"
    rl.cmd_add(url)

    # State must have one queued row for tobi with the placeholder title
    state = rl.load_state()
    assert len(state["queue"]) == 1
    row = state["queue"][0]
    assert row["url"] == url
    assert row["kind"] == "x_thread"
    assert row["posted_by"] == "@tobi"
    assert row["brain_page"] == ""
    assert "saving" in row["title"].lower()


def test_cmd_add_duplicate_short_circuits_without_spawn(tmp_path, monkeypatch):
    """If the URL is already on the queue (e.g. user re-sent the same
    link while a worker is still running), we must NOT spawn a second
    worker — that would double-write the brain page and double-send the
    Telegram follow-up."""
    rl = _import_reading_list()
    list_path = tmp_path / "lists" / "to-read.md"
    monkeypatch.setattr(rl, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(rl, "LIST_PATH", list_path)
    monkeypatch.setattr(rl, "git_commit", lambda: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    popen_calls = []
    monkeypatch.setattr(rl.subprocess, "Popen", lambda *a, **k: popen_calls.append(a))
    monkeypatch.setattr(os.path, "isfile", lambda p: p == rl.WORKER_BIN)
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    url = "https://x.com/tobi/status/2053121182044451016"
    rl.cmd_add(url)
    assert len(popen_calls) == 1

    # Same URL again with tracking param noise — normalize_url should
    # collapse them, and we should hit the duplicate path.
    buf2 = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf2)
    rl.cmd_add(url + "?s=46")
    env = json.loads(buf2.getvalue().strip())
    assert env["ok"] is False
    assert env["reason"] == "duplicate"
    assert "Already" in env["user_message"]
    # No second worker spawned
    assert len(popen_calls) == 1


def test_cmd_add_sync_mode_runs_ingestor_inline(tmp_path, monkeypatch):
    """READING_LIST_SYNC=1 must preserve the legacy synchronous behavior
    so smoke tests + the existing CI eval suite keep working."""
    rl = _import_reading_list()
    monkeypatch.setattr(rl, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(rl, "LIST_PATH", tmp_path / "lists" / "to-read.md")
    monkeypatch.setattr(rl, "git_commit", lambda: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))

    ingestor_calls = []
    def stub_run_ingestor(url, kind):
        ingestor_calls.append((url, kind))
        return {
            "title": "Real title from ingestor",
            "posted_by": "Tobi Lutke (@tobi)",
            "one_liner": "A real one-line takeaway.",
            "brain_page": "media/social/x-tobi-20260430",
        }
    monkeypatch.setattr(rl, "run_ingestor", stub_run_ingestor)
    # If async path leaks through, Popen will fail loud
    monkeypatch.setattr(rl.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sync mode must not spawn")))
    monkeypatch.setenv("READING_LIST_SYNC", "1")
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)

    rl.cmd_add("https://x.com/tobi/status/2053121182044451016")
    env = json.loads(buf.getvalue().strip())

    assert env["ok"] is True
    assert env.get("pending") is None or env.get("pending") is False
    assert env["title"] == "Real title from ingestor"
    assert "✅ Bookmarked" in env["user_message"]
    assert ingestor_calls == [("https://x.com/tobi/status/2053121182044451016", "x_thread")]


# ---------------------------------------------------------------------------
# Worker — finds row, hydrates, sends follow-up
# ---------------------------------------------------------------------------

def test_worker_hydrates_pending_row_and_sends_followup(tmp_path, monkeypatch):
    rl = _import_reading_list()
    worker = _import_worker()

    monkeypatch.setattr(rl, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(rl, "LIST_PATH", tmp_path / "lists" / "to-read.md")
    monkeypatch.setattr(rl, "git_commit", lambda: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    # Seed a pending row via the async path
    monkeypatch.setattr(rl.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(os.path, "isfile", lambda p: True)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    url = "https://x.com/tobi/status/2053121182044451016"
    rl.cmd_add(url)

    # Worker imports reading-list module fresh; force it to use the
    # same module instance we just primed by monkey-patching its loader.
    monkeypatch.setattr(worker, "_load_reading_list", lambda: rl)
    # Stub the deep ingestor — return fields as if the brain page was written
    monkeypatch.setattr(rl, "run_ingestor", lambda u, k: {
        "title": "tobi: Learning on the Shop floor",
        "posted_by": "Tobi Lutke (@tobi)",
        "one_liner": "A founder's note on staying close to the operating system of the company.",
        "brain_page": "media/social/x-tobi-20260430",
    })
    monkeypatch.setattr(worker, "_drain_xref_queue", lambda: None)
    telegram_messages = []
    monkeypatch.setattr(worker, "_send_telegram", lambda txt: telegram_messages.append(txt) or True)

    # Run worker main
    monkeypatch.setattr(sys, "argv", ["reading-list-worker.py", url])
    worker.main()

    # Row now hydrated
    state = rl.load_state()
    assert len(state["queue"]) == 1
    row = state["queue"][0]
    assert row["title"] == "tobi: Learning on the Shop floor"
    assert row["brain_page"] == "media/social/x-tobi-20260430"
    assert "founder" in row["one_liner"].lower()
    # Telegram follow-up sent exactly once
    assert len(telegram_messages) == 1
    msg = telegram_messages[0]
    assert msg.startswith("✅ Bookmark ready")
    assert "Brain page: media/social/x-tobi-20260430" in msg
    assert url in msg


def test_worker_missing_row_exits_clean(tmp_path, monkeypatch):
    """User dropped the pending row before worker started — worker must
    exit without crashing and without sending a misleading follow-up."""
    rl = _import_reading_list()
    worker = _import_worker()

    monkeypatch.setattr(rl, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(rl, "LIST_PATH", tmp_path / "lists" / "to-read.md")
    (tmp_path / "lists").mkdir(exist_ok=True)
    (tmp_path / "lists" / "to-read.md").write_text("# To read\n\n## Queue\n\nQueue is empty.\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(worker, "_load_reading_list", lambda: rl)
    sends = []
    monkeypatch.setattr(worker, "_send_telegram", lambda t: sends.append(t) or True)

    monkeypatch.setattr(sys, "argv", ["reading-list-worker.py", "https://x.com/tobi/status/2053121182044451016"])
    worker.main()
    assert sends == []


def test_worker_failed_ingest_sends_failure_followup(tmp_path, monkeypatch):
    rl = _import_reading_list()
    worker = _import_worker()
    monkeypatch.setattr(rl, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(rl, "LIST_PATH", tmp_path / "lists" / "to-read.md")
    monkeypatch.setattr(rl, "git_commit", lambda: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(rl.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(os.path, "isfile", lambda p: True)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    url = "https://example.com/some-article"
    rl.cmd_add(url)
    # Ingestor returns no brain page (simulates ingest failure)
    monkeypatch.setattr(rl, "run_ingestor", lambda u, k: {
        "title": "", "posted_by": "", "one_liner": "", "brain_page": "",
    })
    monkeypatch.setattr(worker, "_load_reading_list", lambda: rl)
    monkeypatch.setattr(worker, "_drain_xref_queue", lambda: None)
    sends = []
    monkeypatch.setattr(worker, "_send_telegram", lambda t: sends.append(t) or True)

    monkeypatch.setattr(sys, "argv", ["reading-list-worker.py", url])
    worker.main()

    assert len(sends) == 1
    assert "⚠️" in sends[0] or "failed" in sends[0].lower()
    assert "migrate" in sends[0].lower()


# ---------------------------------------------------------------------------
# Xref deferral — ingest scripts must write to the queue file, not call
# gbrain inline.
# ---------------------------------------------------------------------------

def test_xref_drainer_enqueue_and_drain_roundtrip(tmp_path, monkeypatch):
    drainer_path = REPO / "infra" / "xref-drainer.py"
    spec = importlib.util.spec_from_file_location("xref_drainer", drainer_path)
    assert spec is not None and spec.loader is not None
    drainer = importlib.util.module_from_spec(spec)
    sys.modules["xref_drainer"] = drainer
    spec.loader.exec_module(drainer)  # type: ignore[union-attr]

    monkeypatch.setattr(drainer, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(drainer, "QUEUE_PATH", tmp_path / ".xref-queue.jsonl")
    monkeypatch.setattr(drainer, "PROCESSED_PATH", tmp_path / ".xref-processed.jsonl")
    monkeypatch.setattr(drainer, "LOCK_PATH", tmp_path / ".xref-queue.lock")

    drainer.enqueue("media/social/x-tobi-20260430",
                    ["people/tobi-lutke", "concepts/founder-mode"],
                    "X thread")
    drainer.enqueue("sources/example-blog-post",
                    ["concepts/strength"],
                    "Web article")

    # Drain — stub gbrain calls so we don't actually need bun on PATH
    calls = []
    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        if cmd[0:2] == [drainer.GBRAIN, "get"]:
            return subprocess.CompletedProcess(cmd, 1, "", "")  # page missing -> stub
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(drainer.subprocess, "run", fake_run)

    n = drainer.drain_once()
    assert n == 2

    # Queue cleared, processed log written
    assert (tmp_path / ".xref-queue.jsonl").read_text() == ""
    processed_lines = (tmp_path / ".xref-processed.jsonl").read_text().strip().splitlines()
    assert len(processed_lines) == 2
    for line in processed_lines:
        rec = json.loads(line)
        assert "processed_at" in rec
        assert isinstance(rec["linked_ok"], list)
        assert isinstance(rec["linked_failed"], list)


def test_cmd_migrate_async_spawns_worker_returns_ack_fast(tmp_path, monkeypatch):
    """Migrate was hitting the 120s per-turn timeout because it
    iterated rows synchronously. New behavior: cmd_migrate counts
    pending rows, spawns infra/migrate-worker.py detached, and prints
    an ack envelope with `pending: true` and a "I'll send a summary
    when done" user_message — same shape as cmd_add's async path."""
    rl = _import_reading_list()
    monkeypatch.setattr(rl, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(rl, "LIST_PATH", tmp_path / "lists" / "to-read.md")
    monkeypatch.setattr(rl, "git_commit", lambda: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))

    # Seed two queue rows that need re-ingest (no brain_page)
    list_path = tmp_path / "lists" / "to-read.md"
    list_path.parent.mkdir(parents=True, exist_ok=True)
    list_path.write_text(
        "# To read\n\n## Queue\n\n"
        "| # | Title | Posted by | One-liner | Kind | URL | Added | Brain page |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| 1 | t1 | @a | | x_thread | https://x.com/a/status/1 | 2026-05-12 |  |\n"
        "| 2 | t2 | @b | | x_thread | https://x.com/b/status/2 | 2026-05-12 |  |\n"
    )

    # Stub ingestor — if cmd_migrate runs INLINE, this gets called.
    # Async path must NOT call it (worker runs ingest, not cmd_migrate).
    ingest_calls = []
    monkeypatch.setattr(rl, "run_ingestor", lambda u, k: (ingest_calls.append((u, k)), {
        "title": "x", "posted_by": "x", "one_liner": "x", "brain_page": "x"
    })[1])

    popen_calls = []
    class FakePopen:
        def __init__(self, args, **kwargs):
            popen_calls.append({"args": list(args), "kwargs": kwargs})
    monkeypatch.setattr(rl.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(os.path, "isfile", lambda p: p == rl.MIGRATE_WORKER_BIN or p == rl.WORKER_BIN)

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rl.cmd_migrate(force=False)

    env = json.loads(buf.getvalue().strip())
    assert env["ok"] is True
    assert env["pending"] is True
    assert env["todo"] == 2
    assert "background" in env["user_message"]
    assert "summary" in env["user_message"]
    # No inline ingest happened
    assert ingest_calls == []
    # Worker was spawned exactly once
    assert len(popen_calls) == 1
    call = popen_calls[0]
    assert call["args"][:2] == ["python3", rl.MIGRATE_WORKER_BIN]
    assert "--force" not in call["args"]
    assert call["kwargs"].get("start_new_session") is True


def test_cmd_migrate_async_passes_force_flag(tmp_path, monkeypatch):
    rl = _import_reading_list()
    monkeypatch.setattr(rl, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(rl, "LIST_PATH", tmp_path / "lists" / "to-read.md")
    monkeypatch.setattr(rl, "git_commit", lambda: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    list_path = tmp_path / "lists" / "to-read.md"
    list_path.parent.mkdir(parents=True, exist_ok=True)
    # Already-complete row — without --force this is skipped (todo=0)
    list_path.write_text(
        "# To read\n\n## Queue\n\n"
        "| # | Title | Posted by | One-liner | Kind | URL | Added | Brain page |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| 1 | Full | @a | hi | x_thread | https://x.com/a/status/1 | 2026-05-12 | media/social/x-a-20260512 |\n"
    )
    popen_calls = []
    monkeypatch.setattr(rl.subprocess, "Popen",
                        lambda args, **kw: popen_calls.append(list(args)))
    monkeypatch.setattr(os.path, "isfile", lambda p: p == rl.MIGRATE_WORKER_BIN)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    rl.cmd_migrate(force=True)
    assert len(popen_calls) == 1
    assert "--force" in popen_calls[0]


def test_cmd_migrate_async_nothing_to_do_short_circuits(tmp_path, monkeypatch):
    """When every row already has brain_page + posted_by + one_liner
    AND --force isn't set, no worker spawn — just a friendly "nothing
    to migrate" envelope."""
    rl = _import_reading_list()
    monkeypatch.setattr(rl, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(rl, "LIST_PATH", tmp_path / "lists" / "to-read.md")
    monkeypatch.setattr(rl, "git_commit", lambda: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    list_path = tmp_path / "lists" / "to-read.md"
    list_path.parent.mkdir(parents=True, exist_ok=True)
    list_path.write_text(
        "# To read\n\n## Queue\n\n"
        "| # | Title | Posted by | One-liner | Kind | URL | Added | Brain page |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| 1 | Full | @a | hi | x_thread | https://x.com/a/status/1 | 2026-05-12 | media/social/x-a-20260512 |\n"
    )
    popen_calls = []
    monkeypatch.setattr(rl.subprocess, "Popen",
                        lambda *a, **k: popen_calls.append(a))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rl.cmd_migrate(force=False)
    env = json.loads(buf.getvalue().strip())
    assert env["ok"] is True
    assert env.get("pending") in (False, None)
    assert "Nothing to migrate" in env["user_message"]
    assert popen_calls == []


def test_xref_drain_idempotent_when_queue_empty(tmp_path, monkeypatch):
    drainer_path = REPO / "infra" / "xref-drainer.py"
    spec = importlib.util.spec_from_file_location("xref_drainer_empty", drainer_path)
    assert spec is not None and spec.loader is not None
    drainer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drainer)  # type: ignore[union-attr]
    monkeypatch.setattr(drainer, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(drainer, "QUEUE_PATH", tmp_path / ".xref-queue.jsonl")
    monkeypatch.setattr(drainer, "PROCESSED_PATH", tmp_path / ".xref-processed.jsonl")
    monkeypatch.setattr(drainer, "LOCK_PATH", tmp_path / ".xref-queue.lock")
    assert drainer.drain_once() == 0
    # Re-run — still zero, no crash
    assert drainer.drain_once() == 0
