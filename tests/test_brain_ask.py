"""Unit tests for brain-ask.py — the Layer-1 capability that answers
questions about bookmarked content by loading the actual brain page +
raw sidecar (Layer 2) instead of letting the agent improvise from the
bookmark row's one-line blurb.

Failure mode this guards: image-v100, where the agent told the user
"I can't quote — thread text isn't in what I have" and asked them to
paste the article. With brain-ask wired in, that response is no
longer possible — the script ALWAYS loads the page body and (when
present) the verbatim sidecar before answering.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "infra" / "brain-ask.py"


def _import():
    spec = importlib.util.spec_from_file_location("brain_ask", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brain_ask"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


NISHANK_SLUG = "media/social/x-nishank-20260512"

# Fake brain page that mirrors the real ingest-x-thread output shape
NISHANK_PAGE = """---
type: x-thread
created: 2026-05-12
updated: 2026-05-12
---

# nishank: ambition, survivorship bias, persistence

**Author:** Nishank Singla (@nishanksingla) | **Date:** 2026-05-12 | **Link:** https://x.com/nishanksingla/status/2052819273010238771

## Why this matters (Antriksh's frame)
This is a founder-mode self-portrait — Nishank arguing that ambition
without survivorship-bias awareness becomes founder cosplay.

## The argument
The core theme is ambition colliding with reality. Nishank's claim is
that persistence — staying in the arena — is the only credible signal,
not how loud you are about wanting it.

## Raw thread
- Most takes on ambition are survivorship bias dressed as advice.
- The "man in the arena" frame is useful exactly to mute the gallery.
- I felt like quitting at least nine times in the last 18 months.
"""

# Verbatim sidecar — what the user expects to be quoted from
NISHANK_RAW = """# Raw rendered page for media/social/x-nishank-20260512
Source URL: https://x.com/nishanksingla/status/2052819273010238771
Captured: 2026-05-12
Page title: Nishank Singla (@nishanksingla) on X

## Rendered page text (document.body.innerText)
nishank @nishanksingla · May 12

Most takes on ambition are survivorship bias dressed as advice. The
ones who made it tell you to "stay in the arena" — but they don't
mention the ten thousand who stayed in the arena and lost.

The signal isn't how loud you are about wanting it. The signal is
that you're still here next year.

I felt like quitting at least nine times in the last 18 months.
The only thing that kept me going was that I'd already told everyone
I would.

47 replies · 312 reposts · 2.1K likes
"""


def test_ask_uses_raw_sidecar_when_present_for_quotes(tmp_path, monkeypatch):
    """When the raw sidecar exists, the LLM payload must include the
    verbatim source so quotes are extractable. This is the v100 fix:
    the agent USED to say 'I can't quote — text isn't here'; now the
    script puts the verbatim text in front of the LLM."""
    mod = _import()
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "RAW_DIR", tmp_path / "_raw")

    # Seed both brain page and raw sidecar on disk
    page_path = tmp_path / f"{NISHANK_SLUG}.md"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(NISHANK_PAGE)
    (tmp_path / "_raw").mkdir(parents=True, exist_ok=True)
    raw_path = tmp_path / "_raw" / "x-nishank-20260512.raw.txt"
    raw_path.write_text(NISHANK_RAW)

    captured = {}
    def fake_llm_call(system_prompt, user_msg, tier="mini", max_tokens=2500, **kwargs):
        captured["system"] = system_prompt
        captured["user"] = user_msg
        return ("1. \"Most takes on ambition are survivorship bias dressed as advice\" — Nishank, ¶1\n2. The arena framing exists to mute the gallery.\n3. \"I felt like quitting at least nine times\" — ¶3", "openai")
    fake_llm_mod = mock.MagicMock()
    fake_llm_mod.call = fake_llm_call
    monkeypatch.setitem(sys.modules, "llm", fake_llm_mod)

    result = mod.ask(NISHANK_SLUG, "what are the top 3 takeaways with quotes?")

    assert result["ok"] is True
    assert result["slug"] == NISHANK_SLUG
    assert result["source"].endswith(".raw.txt") or result["source"] == "brain_page"
    assert result["raw_chars"] > 0
    assert result["page_chars"] > 0
    # The LLM must have seen BOTH brain page and raw source
    assert "BRAIN PAGE BODY" in captured["user"]
    assert "RAW SOURCE" in captured["user"]
    # The verbatim sentence must be in the LLM context — that's the v100 fix
    assert "Most takes on ambition are survivorship bias dressed as advice" in captured["user"]
    # The system prompt forbids speculation + requires verbatim quotes
    assert "quote VERBATIM" in captured["system"]
    assert "If the answer isn't in the material" in captured["system"]
    # The user_message field (relayed verbatim to Telegram) ends with a
    # plain-text citation — wikilink syntax got stripped by the agent on
    # relay in production, so we use 📎 + bare slug instead.
    assert "📎 source: media/social/x-nishank-20260512" in result["user_message"]
    # And contains the actual quote
    assert "survivorship bias" in result["user_message"]


def test_ask_falls_back_to_brain_page_when_no_raw_sidecar(tmp_path, monkeypatch):
    """If browser-harness wasn't used at ingest (e.g. legacy bookmarks
    pre-Layer-2, or API-only path), the raw sidecar is absent. The
    brain page body alone must still be usable — just without
    verbatim-quote guarantee."""
    mod = _import()
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "RAW_DIR", tmp_path / "_raw")
    page_path = tmp_path / f"{NISHANK_SLUG}.md"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(NISHANK_PAGE)

    captured = {}
    fake_llm = mock.MagicMock()
    fake_llm.call = lambda system_prompt, user_msg, **kw: (captured.setdefault("u", user_msg), ("answer body", "openai"))[1]
    monkeypatch.setitem(sys.modules, "llm", fake_llm)

    result = mod.ask(NISHANK_SLUG, "what is the core argument?")
    assert result["ok"] is True
    assert result["raw_chars"] == 0
    assert result["page_chars"] > 0
    assert "BRAIN PAGE BODY" in captured["u"]
    assert "RAW SOURCE" not in captured["u"]


def test_ask_returns_not_found_for_missing_slug(tmp_path, monkeypatch):
    """Only after brain-ask returns ok=false reason=not_found may the
    agent ask the user to paste content. This shape is what AGENTS.md
    keys on for that escape hatch."""
    mod = _import()
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "RAW_DIR", tmp_path / "_raw")
    # gbrain fallback also fails
    import subprocess as sp
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 1, "", "not found"))

    result = mod.ask("nonexistent/slug", "anything")
    assert result["ok"] is False
    assert result["reason"] == "not_found"
    assert "Bookmark or ingest" in result["user_message"]


def test_resolve_pointer_free_text_delegates_to_brain_find(tmp_path, monkeypatch):
    """Free-text pointers (anything that isn't a slug / URL / "last")
    delegate to brain-find.py. This is the 2026-05-14 fix for the
    v100 follow-up where `last` had drifted off the user's intended
    article."""
    mod = _import()
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "LAST_BOOKMARK_PATH", tmp_path / ".last-bookmark.json")

    # Stub the subprocess that would shell out to brain-find.py
    import subprocess as sp
    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, list) and any("brain-find" in c for c in cmd):
            envelope = {
                "ok": True,
                "best": "media/social/x-nishank-20260512",
                "confident": True,
                "candidates": [{"slug": "media/social/x-nishank-20260512", "score": 1.0, "reason": "handle==nishanksingla"}],
            }
            return sp.CompletedProcess(cmd, 0, json.dumps(envelope), "")
        return sp.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    # find() expects brain-find.py to exist next to brain-ask.py
    monkeypatch.setattr(mod.Path, "exists", lambda self: True)

    # "Nishank's article" is neither a slug nor a URL nor "last" —
    # must fall through to brain-find.
    slug = mod._resolve_pointer("Nishank's article")
    assert slug == "media/social/x-nishank-20260512"


def test_resolve_pointer_slug_shape_bypasses_brain_find(tmp_path, monkeypatch):
    """Literal slug shape ('a/b' with no spaces) must be passed through
    without calling brain-find — would be wasted work."""
    mod = _import()
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "LAST_BOOKMARK_PATH", tmp_path / ".last-bookmark.json")

    import subprocess as sp
    calls = []
    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return sp.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    slug = mod._resolve_pointer("media/social/x-nishank-20260512")
    assert slug == "media/social/x-nishank-20260512"
    # No subprocess shell-out happened (brain-find not called)
    assert not any(isinstance(c, list) and any("brain-find" in str(p) for p in c) for c in calls)


def test_resolve_pointer_handles_last_url_and_slug(tmp_path, monkeypatch):
    mod = _import()
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "LAST_BOOKMARK_PATH", tmp_path / ".last-bookmark.json")

    # Literal slug → returned as-is
    assert mod._resolve_pointer("media/social/x-foo-20260101") == "media/social/x-foo-20260101"

    # "last" with no pointer file → empty
    assert mod._resolve_pointer("last") == ""

    # "last" with pointer file present
    (tmp_path / ".last-bookmark.json").write_text(json.dumps({"slug": NISHANK_SLUG, "url": "x"}))
    assert mod._resolve_pointer("last") == NISHANK_SLUG

    # URL → resolved via lists/to-read.md
    list_path = tmp_path / "lists" / "to-read.md"
    list_path.parent.mkdir(parents=True, exist_ok=True)
    list_path.write_text(
        "# To read\n\n"
        "## Queue\n\n"
        "| # | Title | Posted by | One-liner | Kind | URL | Added | Brain page |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| 1 | ambition | @nishanksingla | line | x_thread | "
        "https://x.com/nishanksingla/status/2052819273010238771 | 2026-05-12 | "
        f"[[{NISHANK_SLUG}]] |\n"
    )
    # URL with tracking param ?s=12 — normalize must strip it before lookup
    resolved = mod._resolve_pointer("https://x.com/nishanksingla/status/2052819273010238771?s=12")
    assert resolved == NISHANK_SLUG


def test_main_top_takeaways_with_quotes_end_to_end(tmp_path, monkeypatch, capsys):
    """End-to-end through main(): mimics what the agent would call.
    Asserts the printed envelope is well-formed and that the answer
    body propagates from the (stubbed) LLM to user_message verbatim
    with the citation appended."""
    mod = _import()
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "RAW_DIR", tmp_path / "_raw")
    monkeypatch.setattr(mod, "LAST_BOOKMARK_PATH", tmp_path / ".last-bookmark.json")

    page_path = tmp_path / f"{NISHANK_SLUG}.md"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(NISHANK_PAGE)
    (tmp_path / "_raw").mkdir(parents=True, exist_ok=True)
    (tmp_path / "_raw" / "x-nishank-20260512.raw.txt").write_text(NISHANK_RAW)
    (tmp_path / ".last-bookmark.json").write_text(json.dumps({"slug": NISHANK_SLUG, "url": "x"}))

    answer_text = (
        "1. \"Most takes on ambition are survivorship bias dressed as advice\" "
        "— Nishank, ¶1. The framing reframes ambition advice as selection bias.\n"
        "2. \"The signal is that you're still here next year\" — ¶2. Persistence "
        "is the only credible proof.\n"
        "3. \"I felt like quitting at least nine times\" — ¶3. The honest part is "
        "that the survivor's narrative quietly hides the near-quits."
    )
    fake_llm = mock.MagicMock()
    fake_llm.call = lambda system_prompt, user_msg, **kw: (answer_text, "openai")
    monkeypatch.setitem(sys.modules, "llm", fake_llm)

    monkeypatch.setattr(sys, "argv", ["brain-ask.py", "last", "top 3 takeaways with quotes"])
    mod.main()

    out = capsys.readouterr().out.strip()
    env = json.loads(out)
    assert env["ok"] is True
    assert env["slug"] == NISHANK_SLUG
    assert env["raw_chars"] > 0
    # All three quoted phrases appear in the user-relayed reply
    assert "survivorship bias dressed as advice" in env["user_message"]
    assert "still here next year" in env["user_message"]
    assert "felt like quitting" in env["user_message"]
    # Citation appended in plain-text form so the agent doesn't strip
    # it as markdown-only on the way to Telegram.
    assert "📎 source: media/social/x-nishank-20260512" in env["user_message"]
