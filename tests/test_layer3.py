"""Unit tests for the Layer 3 brain primitives shipped 2026-05-14:

  - brain-search.py  (Pattern 7 — discovery)
  - brain-graph.py   (Pattern 3 — traversal)
  - brain-compare.py (Pattern 2 — cross-page synthesis)
  - brain-edit.py    (Pattern 5 — surgical mutation)
  - brain-ask.py     (Pattern 6 — --persona for contextual answers)

Patterns 1/4/9 (brain-ask, note, recall) are already covered in
tests/test_brain_ask.py / tests/test_brain_find.py / tests/test_note.py.

The fixture below mirrors a small but realistic brain state — one
X-thread bookmark each from Nishank and Tobi, a Paul Graham essay,
plus a note Antriksh has written about Nishank's argument. Many of
the tests deliberately reuse the same brain state so we exercise
cross-cutting behaviors (search → graph → compare flow).
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]


def _import(script):
    """Load a script under infra/ as a module."""
    path = REPO / "infra" / script
    spec = importlib.util.spec_from_file_location(script.replace("-", "_").replace(".py", ""), path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[script.replace("-", "_").replace(".py", "")] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

NISHANK_SLUG = "media/social/x-nishank-20260512"
TOBI_SLUG = "media/social/x-tobi-20260509"
PG_SLUG = "sources/paulgraham-foundermode"
NOTE_SLUG = "notes/2026-05-13-survivorship-stage-asymmetry"

LISTS_FIXTURE = f"""# To read

## Queue

| # | Title | Posted by | One-liner | Kind | URL | Added | Brain page |
|---|---|---|---|---|---|---|---|
| 1 | ambition, survivorship bias, persistence | Nishank Singla (@nishanksingla) | Founder-mode self-portrait — ambition meets reality | x_thread | https://x.com/nishanksingla/status/2052819273010238771 | 2026-05-12 | [[{NISHANK_SLUG}]] |
| 2 | Learning on the Shop floor | tobi lutke (@tobi) | Stay close to the product's operating system | x_thread | https://x.com/tobi/status/2053121182044451016 | 2026-05-13 | [[{TOBI_SLUG}]] |
| 3 | Founder Mode | Paul Graham (paulgraham.com) | Don't tell founders to act like managers | html | https://paulgraham.com/foundermode.html | 2026-05-08 | [[{PG_SLUG}]] |
"""

NISHANK_PAGE = f"""---
type: x-thread
created: 2026-05-12
updated: 2026-05-12
---

# nishank: ambition, survivorship bias, persistence

**Author:** Nishank Singla (@nishanksingla) | **Date:** 2026-05-12

## Why this matters (Antriksh's frame)
A founder-mode self-portrait. Connects to [[concepts/founder-mode]]
and [[concepts/survivorship-bias]].

## The argument
Most ambition advice is survivorship bias dressed as principle.
Persistence is the only credible signal.

## Connections
- [[concepts/founder-mode]] — the lens
- [[concepts/survivorship-bias]] — the trap
- [[people/nishank-singla]]
"""

PG_PAGE = f"""---
type: html
created: 2026-05-08
updated: 2026-05-08
---

# Founder Mode

**Author:** Paul Graham | **Date:** 2024-09 | **Link:** https://paulgraham.com/foundermode.html

## The argument
Founders shouldn't be coached to act like managers. The two modes
are different; pretending otherwise breaks the company.

## Connections
- [[concepts/founder-mode]]
- [[people/paul-graham]]
"""

NOTE_PAGE = f"""---
type: note
created: 2026-05-13
updated: 2026-05-13
author: antriksh
source: {NISHANK_SLUG}
---

# survivorship bias applies more at seed stage

I think Nishank's survivorship-bias argument is right but stronger
at seed-stage than late-stage. The asymmetry is in
[[concepts/compounding]] cycles, not in the topic.

## Source
- [[{NISHANK_SLUG}]]

## Connections
- [[concepts/survivorship-bias]]
- [[concepts/compounding]]
"""

NISHANK_RAW = """# Raw rendered page for media/social/x-nishank-20260512

## Rendered page text (document.body.innerText)
nishank @nishanksingla · May 12

Most takes on ambition are survivorship bias dressed as advice.

The signal isn't how loud you are about wanting it. The signal is
that you're still here next year.

I felt like quitting at least nine times in the last 18 months.
"""


def _seed_brain(tmp_path):
    (tmp_path / "lists").mkdir(parents=True, exist_ok=True)
    (tmp_path / "lists" / "to-read.md").write_text(LISTS_FIXTURE)
    (tmp_path / "media" / "social").mkdir(parents=True, exist_ok=True)
    (tmp_path / f"{NISHANK_SLUG}.md").write_text(NISHANK_PAGE)
    (tmp_path / f"{TOBI_SLUG}.md").write_text("# tobi: Learning on the Shop floor\n\nStay close.\n")
    (tmp_path / "sources").mkdir(parents=True, exist_ok=True)
    (tmp_path / f"{PG_SLUG}.md").write_text(PG_PAGE)
    (tmp_path / "notes").mkdir(parents=True, exist_ok=True)
    (tmp_path / f"{NOTE_SLUG}.md").write_text(NOTE_PAGE)
    (tmp_path / "_raw").mkdir(parents=True, exist_ok=True)
    (tmp_path / "_raw" / "x-nishank-20260512.raw.txt").write_text(NISHANK_RAW)
    return tmp_path


# ---------------------------------------------------------------------------
# brain-search
# ---------------------------------------------------------------------------

def test_search_finds_bookmark_by_topic_keyword(tmp_path, monkeypatch):
    """'ambition' should surface Nishank's page (title match) — pure
    lexical, no LLM."""
    mod = _import("brain-search.py")
    _seed_brain(tmp_path)
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "LIST_PATH", tmp_path / "lists" / "to-read.md")
    monkeypatch.setattr(mod, "NOTES_DIR", tmp_path / "notes")

    r = mod.search("ambition")
    assert r["ok"] is True
    slugs = [x["slug"] for x in r["results"]]
    assert NISHANK_SLUG in slugs


def test_search_includes_notes_alongside_bookmarks(tmp_path, monkeypatch):
    """Notes live under notes/ — they must be searchable too. Asking
    for 'survivorship' should find BOTH Nishank's bookmark and the
    user's own note on the topic."""
    mod = _import("brain-search.py")
    _seed_brain(tmp_path)
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "LIST_PATH", tmp_path / "lists" / "to-read.md")
    monkeypatch.setattr(mod, "NOTES_DIR", tmp_path / "notes")

    r = mod.search("survivorship")
    assert r["ok"] is True
    slugs = [x["slug"] for x in r["results"]]
    assert NISHANK_SLUG in slugs
    assert NOTE_SLUG in slugs


def test_search_kind_filter_narrows_results(tmp_path, monkeypatch):
    mod = _import("brain-search.py")
    _seed_brain(tmp_path)
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "LIST_PATH", tmp_path / "lists" / "to-read.md")
    monkeypatch.setattr(mod, "NOTES_DIR", tmp_path / "notes")
    r = mod.search("survivorship", kind_filter="note")
    slugs = [x["slug"] for x in r["results"]]
    assert NOTE_SLUG in slugs
    assert NISHANK_SLUG not in slugs


def test_search_no_match_returns_helpful_envelope(tmp_path, monkeypatch):
    mod = _import("brain-search.py")
    _seed_brain(tmp_path)
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "LIST_PATH", tmp_path / "lists" / "to-read.md")
    monkeypatch.setattr(mod, "NOTES_DIR", tmp_path / "notes")
    # Stub gbrain search to also return nothing
    import subprocess as sp
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 0, "", ""))
    r = mod.search("elephant kazoo orchestration")
    assert r["ok"] is False
    assert r["reason"] == "no_match"
    assert "🔎" in r["user_message"]


def test_search_user_message_is_numbered_list(tmp_path, monkeypatch):
    """The Telegram-relayable message must be a numbered list so the
    user can follow up with 'tell me more about #2' etc."""
    mod = _import("brain-search.py")
    _seed_brain(tmp_path)
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "LIST_PATH", tmp_path / "lists" / "to-read.md")
    monkeypatch.setattr(mod, "NOTES_DIR", tmp_path / "notes")
    r = mod.search("founder")
    msg = r["user_message"]
    assert "1." in msg
    # Each entry has a slug citation
    assert "📎 " in msg


# ---------------------------------------------------------------------------
# brain-graph
# ---------------------------------------------------------------------------

def test_graph_returns_forward_links_and_backlinks(tmp_path, monkeypatch):
    """From Nishank's slug:
       forward = [concepts/founder-mode, concepts/survivorship-bias, people/nishank-singla]
       backlinks = [the user's note, since it references Nishank as source]"""
    mod = _import("brain-graph.py")
    _seed_brain(tmp_path)
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "LAST_BOOKMARK_PATH", tmp_path / ".last-bookmark.json")

    r = mod.graph(NISHANK_SLUG)
    assert r["ok"] is True
    fwd = [x["slug"] for x in r["forward"]]
    bk = [x["slug"] for x in r["backlinks"]]
    assert "concepts/founder-mode" in fwd
    assert "concepts/survivorship-bias" in fwd
    assert NOTE_SLUG in bk


def test_graph_skips_raw_sidecar_dir(tmp_path, monkeypatch):
    """The _raw/ sidecars live as plain text files under ${BRAIN_PATH}/_raw/
    — they MUST be excluded from the backlink scan so the citation
    inside the raw text doesn't show up as a fake backlink."""
    mod = _import("brain-graph.py")
    _seed_brain(tmp_path)
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    # Put a synthetic raw sidecar that mentions Nishank's slug as a
    # wikilink — graph must not return it as a backlink
    (tmp_path / "_raw" / "x-tobi-20260509.raw.txt").write_text(
        f"this rendered page mentions [[{NISHANK_SLUG}]] in passing"
    )
    r = mod.graph(NISHANK_SLUG)
    bk_slugs = [x["slug"] for x in r["backlinks"]]
    assert not any(s.startswith("_raw/") for s in bk_slugs)


def test_graph_no_edges_returns_friendly_envelope(tmp_path, monkeypatch):
    mod = _import("brain-graph.py")
    _seed_brain(tmp_path)
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    # A page with no wikilinks AND nothing pointing to it
    isolated = "concepts/isolated-concept-xyz"
    (tmp_path / "concepts").mkdir(exist_ok=True)
    (tmp_path / f"{isolated}.md").write_text("---\ntype: concept\n---\n# Isolated\nNothing links here.\n")
    r = mod.graph(isolated)
    assert r["ok"] is True
    assert r["forward"] == []
    assert r["backlinks"] == []
    assert "No graph edges" in r["user_message"]


# ---------------------------------------------------------------------------
# brain-compare
# ---------------------------------------------------------------------------

def test_compare_loads_both_sources_and_calls_llm_once(tmp_path, monkeypatch):
    """The LLM must see BOTH brain pages plus the raw sidecar when
    one's present. Stub the LLM so the test is hermetic; assert the
    user_msg the LLM saw contained both source bodies."""
    mod = _import("brain-compare.py")
    _seed_brain(tmp_path)
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "RAW_DIR", tmp_path / "_raw")

    captured = {}
    fake_llm = mock.MagicMock()
    def call(system_prompt, user_msg, **kw):
        captured["sys"] = system_prompt
        captured["user"] = user_msg
        return ("Nishank emphasizes persistence ([[%s]]); PG emphasizes the structural distinction ([[%s]]). They overlap on…" % (NISHANK_SLUG, PG_SLUG), "openai")
    fake_llm.call = call
    monkeypatch.setitem(sys.modules, "llm", fake_llm)

    r = mod.compare([NISHANK_SLUG, PG_SLUG], question="what do they each say about persistence")
    assert r["ok"] is True
    # Both source bodies present in the LLM payload
    assert "ambition, survivorship bias" in captured["user"]
    assert "Founder Mode" in captured["user"]
    # Raw sidecar for Nishank also present
    assert "survivorship bias dressed as advice" in captured["user"]
    # Question forwarded
    assert "what do they each say about persistence" in captured["user"]
    # Output carries both citations
    assert NISHANK_SLUG in r["user_message"]
    assert PG_SLUG in r["user_message"]
    # System prompt enforces verbatim quotes
    assert "quote VERBATIM" in captured["sys"]


def test_compare_needs_two_sources(tmp_path, monkeypatch):
    mod = _import("brain-compare.py")
    r = mod.compare([NISHANK_SLUG])
    assert r["ok"] is False
    assert r["reason"] == "needs_two_sources"


def test_compare_missing_source_reports_clearly(tmp_path, monkeypatch):
    mod = _import("brain-compare.py")
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "RAW_DIR", tmp_path / "_raw")
    import subprocess as sp
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 1, "", ""))
    r = mod.compare(["media/social/x-foo-1", "media/social/x-bar-2"])
    assert r["ok"] is False
    assert r["reason"] == "missing_source"


# ---------------------------------------------------------------------------
# brain-edit
# ---------------------------------------------------------------------------

def test_edit_add_link_creates_or_extends_connections(tmp_path, monkeypatch):
    mod = _import("brain-edit.py")
    _seed_brain(tmp_path)
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)

    r = mod.cmd_add_link(NISHANK_SLUG, "concepts/path-dependence",
                         reason="alt frame on the same trap")
    assert r["ok"] is True and r["added"] is True
    written = (tmp_path / f"{NISHANK_SLUG}.md").read_text()
    assert "[[concepts/path-dependence]]" in written
    # Xref queued for the drainer to create the backlink
    q = (tmp_path / ".xref-queue.jsonl").read_text().splitlines()
    payload = json.loads(q[0])
    assert "concepts/path-dependence" in payload["entities"]


def test_edit_add_link_dedupes_existing(tmp_path, monkeypatch):
    """Linking an already-linked entity is a no-op (the agent might
    repeat the user's request mid-flow). Must report added=False."""
    mod = _import("brain-edit.py")
    _seed_brain(tmp_path)
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    r = mod.cmd_add_link(NISHANK_SLUG, "concepts/founder-mode")
    assert r["ok"] is True
    assert r["added"] is False


def test_edit_append_preserves_trailing_connections(tmp_path, monkeypatch):
    """Appended content goes ABOVE the Connections section so the
    structural shape stays stable on repeated appends."""
    mod = _import("brain-edit.py")
    _seed_brain(tmp_path)
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    r = mod.cmd_append(NISHANK_SLUG, "## Antriksh's reaction\nThe stage axis matters more than the topic axis.")
    assert r["ok"] is True
    written = (tmp_path / f"{NISHANK_SLUG}.md").read_text()
    reaction_idx = written.find("## Antriksh's reaction")
    conn_idx = written.find("## Connections")
    assert reaction_idx > 0 and conn_idx > reaction_idx


def test_edit_append_picks_up_wikilinks_for_xref(tmp_path, monkeypatch):
    mod = _import("brain-edit.py")
    _seed_brain(tmp_path)
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    mod.cmd_append(NISHANK_SLUG, "Related: [[concepts/loss-aversion]] also lurks here.")
    payload = json.loads((tmp_path / ".xref-queue.jsonl").read_text().splitlines()[0])
    assert "concepts/loss-aversion" in payload["entities"]


def test_edit_set_field_creates_or_replaces_frontmatter(tmp_path, monkeypatch):
    mod = _import("brain-edit.py")
    _seed_brain(tmp_path)
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    # Field that doesn't exist yet
    r = mod.cmd_set_field(NISHANK_SLUG, "rating", "5")
    assert r["ok"] is True
    written = (tmp_path / f"{NISHANK_SLUG}.md").read_text()
    assert re.search(r"^rating:\s*5", written, re.MULTILINE)
    # Then replace it
    mod.cmd_set_field(NISHANK_SLUG, "rating", "4")
    written2 = (tmp_path / f"{NISHANK_SLUG}.md").read_text()
    assert re.search(r"^rating:\s*4", written2, re.MULTILINE)
    # Only one rating line — no duplicates
    assert len(re.findall(r"^rating:", written2, re.MULTILINE)) == 1


def test_edit_missing_page_reports_not_found(tmp_path, monkeypatch):
    mod = _import("brain-edit.py")
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    r = mod.cmd_add_link("media/social/x-doesntexist-99999999", "concepts/x")
    assert r["ok"] is False
    assert r["reason"] == "not_found"


# ---------------------------------------------------------------------------
# brain-ask --persona (Pattern 6)
# ---------------------------------------------------------------------------

def test_brain_ask_persona_loads_reader_frame_into_prompt(tmp_path, monkeypatch):
    """When --persona is passed, brain-ask must load the persona page
    (defaulting to people/antriksh) and inject it into the LLM prompt
    as 'READER FRAME'. The system prompt and verbatim-quote discipline
    are unchanged."""
    mod = _import("brain-ask.py")
    _seed_brain(tmp_path)
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "RAW_DIR", tmp_path / "_raw")
    monkeypatch.setattr(mod, "LAST_BOOKMARK_PATH", tmp_path / ".last-bookmark.json")
    # Seed an Antriksh entity page
    (tmp_path / "people").mkdir(parents=True, exist_ok=True)
    (tmp_path / "people" / "antriksh.md").write_text(
        "---\ntype: person\n---\n# Antriksh\n"
        "Founder building AI agents and personal assistant. Thinks in "
        "[[founder-mode]] terms. Current focus: brain compounding.\n"
    )

    captured = {}
    fake_llm = mock.MagicMock()
    def call(system_prompt, user_msg, **kw):
        captured["user"] = user_msg
        return ("Calibrated answer using Antriksh's context.", "openai")
    fake_llm.call = call
    monkeypatch.setitem(sys.modules, "llm", fake_llm)

    r = mod.ask(NISHANK_SLUG, "explain Nishank's thesis given who I am",
                persona_slug="people/antriksh")
    assert r["ok"] is True
    assert r["persona"] == "people/antriksh"
    assert "READER FRAME" in captured["user"]
    assert "Founder building AI agents" in captured["user"]
    # Question and brain page also present
    assert "Nishank's thesis" in captured["user"]
    assert "ambition, survivorship bias" in captured["user"]


def test_brain_ask_no_persona_omits_reader_frame(tmp_path, monkeypatch):
    """Without --persona, the LLM prompt MUST NOT include the
    READER FRAME block — otherwise every answer drags in the persona
    page even when not requested."""
    mod = _import("brain-ask.py")
    _seed_brain(tmp_path)
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "RAW_DIR", tmp_path / "_raw")
    monkeypatch.setattr(mod, "LAST_BOOKMARK_PATH", tmp_path / ".last-bookmark.json")

    captured = {}
    fake_llm = mock.MagicMock()
    fake_llm.call = lambda system_prompt, user_msg, **kw: (captured.setdefault("u", user_msg), ("answer", "openai"))[1]
    monkeypatch.setitem(sys.modules, "llm", fake_llm)

    mod.ask(NISHANK_SLUG, "what's the core argument")
    assert "READER FRAME" not in captured["u"]
