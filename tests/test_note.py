"""Unit tests for note.py — Layer 3 note-taking.

The brain has stored ingested bookmarks since reading-list v2 shipped.
note.py adds the symmetric direction: Antriksh's own writing lands
under notes/ as first-class brain pages so brain-find can route to
them and brain-ask can quote from them.

These tests pin the user-facing contract:
  * The body is preserved verbatim (no paraphrasing, no envelope)
  * Slug derivation is deterministic and collision-safe
  * `--source` adds a back-link AND queues the xref so existing
    bookmarked pages link forward to the note
  * `--append-to` extends an existing note without losing its
    Connections section
  * Auto-link picks up entity mentions from people/ companies/
    concepts/ on disk
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "infra" / "note.py"


def _import():
    spec = importlib.util.spec_from_file_location("note", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["note"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Title + slug derivation
# ---------------------------------------------------------------------------

def test_derive_title_uses_first_sentence():
    mod = _import()
    text = "Founder-mode applies differently at seed vs growth. The asymmetry is in compounding."
    t = mod.derive_title(text)
    assert t == "Founder-mode applies differently at seed vs growth"


def test_derive_title_truncates_long_first_line():
    mod = _import()
    text = "x" * 250
    t = mod.derive_title(text)
    assert t.endswith("…")
    assert len(t) <= 81


def test_derive_title_empty_fallback():
    mod = _import()
    t = mod.derive_title("")
    assert t.startswith("note ")


def test_slugify_strips_punctuation_lowercases_joins_with_dashes():
    mod = _import()
    assert mod.slugify("Founder-mode at seed vs growth!") == "founder-mode-at-seed-vs-growth"


# ---------------------------------------------------------------------------
# write_new_note — fresh note
# ---------------------------------------------------------------------------

def test_write_new_note_preserves_body_verbatim(tmp_path, monkeypatch):
    """The body must land in the brain page exactly as typed —
    paraphrasing or "cleanup" is a regression."""
    mod = _import()
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "NOTES_DIR", tmp_path / "notes")
    # gbrain put becomes a no-op
    import subprocess as sp
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 0, "", ""))

    body = ('Nishank is wrong about persistence — the asymmetry is in '
            'seed-stage [[concepts/compounding]], not late-stage. '
            'Founders shouldn\'t mistake the "stay in arena" framing for '
            'a permission slip.')
    slug, title, links = mod.write_new_note(body)

    page_path = tmp_path / f"{slug}.md"
    assert page_path.exists()
    written = page_path.read_text()
    # Body present verbatim — including the apostrophe + dash + brackets
    assert body in written
    # Frontmatter shape
    assert "type: note" in written
    assert "author: antriksh" in written
    # Slug includes the date + a derived stem
    assert slug.startswith("notes/")
    assert mod.TODAY in slug


def test_write_new_note_collision_suffix(tmp_path, monkeypatch):
    """Two notes on the same day with the same opening line shouldn't
    clobber each other — the second gets a -2 suffix."""
    mod = _import()
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "NOTES_DIR", tmp_path / "notes")
    import subprocess as sp
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 0, "", ""))

    body = "Founder mode applies at seed."
    s1, _, _ = mod.write_new_note(body)
    s2, _, _ = mod.write_new_note(body)
    assert s1 != s2
    assert s2.endswith("-2") or s2.endswith("-3")
    assert (tmp_path / f"{s1}.md").exists()
    assert (tmp_path / f"{s2}.md").exists()


def test_write_new_note_with_source_links_back(tmp_path, monkeypatch):
    """--source emits a `source:` frontmatter field, a `## Source`
    section, AND enqueues an xref so the parent brain page gains a
    backlink to the note."""
    mod = _import()
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "NOTES_DIR", tmp_path / "notes")
    import subprocess as sp
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 0, "", ""))

    body = "The asymmetry is in seed-stage compounding."
    slug, _, links = mod.write_new_note(body, source_slug="media/social/x-nishank-20260512")
    written = (tmp_path / f"{slug}.md").read_text()
    assert "source: media/social/x-nishank-20260512" in written
    assert "## Source" in written
    assert "[[media/social/x-nishank-20260512]]" in written
    assert "media/social/x-nishank-20260512" in links
    # Xref queue file written so xref-drainer creates the backlink async
    queue = (tmp_path / ".xref-queue.jsonl").read_text().strip()
    payload = json.loads(queue.splitlines()[0])
    assert payload["slug"] == slug
    assert "media/social/x-nishank-20260512" in payload["entities"]


def test_write_new_note_auto_links_existing_entities(tmp_path, monkeypatch):
    """If the user mentions an entity by name AND that entity already
    has a brain page under people/ companies/ concepts/, auto-add a
    [[wikilink]] in the Connections section. Conservative: substring
    match on the slug stem."""
    mod = _import()
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "NOTES_DIR", tmp_path / "notes")
    import subprocess as sp
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 0, "", ""))

    # Seed two existing entities
    (tmp_path / "concepts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "concepts" / "founder-mode.md").write_text("---\ntype: concept\n---\n# Founder mode\n")
    (tmp_path / "people").mkdir(parents=True, exist_ok=True)
    (tmp_path / "people" / "nishank-singla.md").write_text("---\ntype: person\n---\n# Nishank Singla\n")

    body = "Nishank Singla's framing of founder mode is a survivorship-bias trap."
    slug, _, links = mod.write_new_note(body)
    written = (tmp_path / f"{slug}.md").read_text()
    assert "## Connections" in written
    assert "[[concepts/founder-mode]]" in written
    assert "[[people/nishank-singla]]" in written
    assert "concepts/founder-mode" in links
    assert "people/nishank-singla" in links


def test_explicit_wikilinks_in_body_carry_through(tmp_path, monkeypatch):
    """User-typed [[concepts/something]] in the body must be linked
    even if the target page doesn't exist yet — xref-drainer creates
    the stub. Parity with how ingest-x-thread handles wikilinks."""
    mod = _import()
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "NOTES_DIR", tmp_path / "notes")
    import subprocess as sp
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 0, "", ""))

    body = "This connects to [[concepts/path-dependence]] more than [[concepts/founder-mode]]."
    slug, _, links = mod.write_new_note(body)
    assert "concepts/path-dependence" in links
    assert "concepts/founder-mode" in links
    queue = (tmp_path / ".xref-queue.jsonl").read_text().strip()
    payload = json.loads(queue.splitlines()[0])
    assert "concepts/path-dependence" in payload["entities"]


# ---------------------------------------------------------------------------
# append_to_note — extending an existing note
# ---------------------------------------------------------------------------

def test_append_to_note_preserves_connections_section(tmp_path, monkeypatch):
    """Appended text must land ABOVE the Connections section, not
    after it — otherwise the file shape drifts and Connections moves
    to the middle of the doc on subsequent appends."""
    mod = _import()
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "NOTES_DIR", tmp_path / "notes")
    import subprocess as sp
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 0, "", ""))

    # Seed an existing note
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    slug = f"notes/{mod.TODAY}-founder-mode-at-seed"
    (tmp_path / f"{slug}.md").write_text(
        f"---\ntype: note\ncreated: {mod.TODAY}\nupdated: {mod.TODAY}\nauthor: antriksh\n---\n\n"
        "# Founder mode at seed\n\n"
        "Founder mode applies at seed differently than late stage.\n\n"
        "## Connections\n- [[concepts/founder-mode]] — mentioned in the note text\n"
    )
    result_slug = mod.append_to_note(slug, "Adding: the asymmetry is in compounding cycles.")
    assert result_slug == slug
    written = (tmp_path / f"{slug}.md").read_text()
    # New section appears between body and Connections
    addition_idx = written.find("## Addition")
    connections_idx = written.find("## Connections")
    assert addition_idx > 0
    assert connections_idx > addition_idx
    # Both sections intact
    assert "Adding: the asymmetry" in written
    assert "[[concepts/founder-mode]]" in written
    # updated: frontmatter refreshed
    assert f"updated: {mod.TODAY}" in written


def test_append_to_missing_note_raises(tmp_path, monkeypatch):
    mod = _import()
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    import pytest
    with pytest.raises(FileNotFoundError):
        mod.append_to_note("notes/2026-05-14-does-not-exist", "anything")


# ---------------------------------------------------------------------------
# CLI / envelope shape
# ---------------------------------------------------------------------------

def test_main_emits_envelope_with_source_citation(tmp_path, monkeypatch, capsys):
    """The Telegram-facing user_message must end with a `📎 source: <slug>`
    line (same convention as brain-ask) so the agent's relay doesn't
    strip a wikilink-shaped citation, and so the user can re-ask
    about the note later by slug."""
    mod = _import()
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(mod, "NOTES_DIR", tmp_path / "notes")
    import subprocess as sp
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 0, "", ""))

    monkeypatch.setattr(sys, "argv", [
        "note.py", "Founder mode applies differently at seed vs growth.",
    ])
    mod.main()
    env = json.loads(capsys.readouterr().out.strip())
    assert env["ok"] is True
    assert env["slug"].startswith("notes/")
    assert "🗒️" in env["user_message"]
    assert "📎 source:" in env["user_message"]
    assert env["slug"] in env["user_message"]


def test_main_empty_body_returns_friendly_envelope(tmp_path, monkeypatch, capsys):
    mod = _import()
    monkeypatch.setattr(sys, "argv", ["note.py", ""])
    mod.main()
    env = json.loads(capsys.readouterr().out.strip())
    assert env["ok"] is False
    assert env["reason"] == "empty_body"
    assert "🗒️" in env["user_message"]


def test_main_append_to_routes_to_append_path(tmp_path, monkeypatch, capsys):
    mod = _import()
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    slug = f"notes/{mod.TODAY}-existing-note"
    (tmp_path / f"{slug}.md").write_text(
        f"---\ntype: note\ncreated: {mod.TODAY}\nupdated: {mod.TODAY}\n---\n\n"
        "# Existing note\n\nFirst thought.\n"
    )
    import subprocess as sp
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(sys, "argv", [
        "note.py", "Second thought.", "--append-to", slug,
    ])
    mod.main()
    env = json.loads(capsys.readouterr().out.strip())
    assert env["ok"] is True
    assert env["appended"] is True
    assert env["slug"] == slug
    written = (tmp_path / f"{slug}.md").read_text()
    assert "Second thought." in written
    assert "## Addition" in written
