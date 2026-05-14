"""Unit tests for brain-find.py — the free-text → brain page slug
resolver introduced 2026-05-14 after the post-#82 Telethon probe
showed that defaulting to `last` quoted from the wrong article when
the user said "Nishank's article".

The reading-list state below mirrors the kind of queue the bot is
running in production: a mix of X threads and HTML sources, with
real-ish handles, posted_by strings, and titles. Each test asserts
that the user's words map to the correct slug — and that
intentionally ambiguous queries surface multiple candidates rather
than confidently picking one.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "infra" / "brain-find.py"


def _import():
    spec = importlib.util.spec_from_file_location("brain_find", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brain_find"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


QUEUE_FIXTURE = """# To read

## Queue

| # | Title | Posted by | One-liner | Kind | URL | Added | Brain page |
|---|---|---|---|---|---|---|---|
| 1 | ambition, survivorship bias, persistence | Nishank Singla (@nishanksingla) | Founder-mode self-portrait | x_thread | https://x.com/nishanksingla/status/2052819273010238771 | 2026-05-12 | [[media/social/x-nishank-20260512]] |
| 2 | Learning on the Shop floor | tobi lutke (@tobi) | Stay embedded in your product OS | x_thread | https://x.com/tobi/status/2053121182044451016 | 2026-05-13 | [[media/social/x-tobi-20260509]] |
| 3 | QOVES start your glow up without surgery | qoves.com | Personalized appearance protocol | html | https://qoves.com/qoves-start-your-glow-up-without-surgery | 2026-05-14 | [[sources/qoves-com-qoves-start-your-glow-up-without-surgery]] |
| 4 | How to Get New Ideas | paulgraham.com | New ideas come from the frontier | html | https://paulgraham.com/getideas.html | 2026-05-10 | [[sources/paulgraham-getideas]] |
| 5 | Founder Mode | Paul Graham (paulgraham.com) | Don't tell founders to act like managers | html | https://paulgraham.com/foundermode.html | 2026-05-08 | [[sources/paulgraham-foundermode]] |

## Recently read

| # | Title | Posted by | One-liner | Kind | URL | Added | Read | Brain page |
|---|---|---|---|---|---|---|---|---|
| 1 | Empty thread, no substantive claim | Siddharth (@siddhxrth10) | (none) | x_thread | https://x.com/siddhxrth10/status/2048008587923149140 | 2026-04-30 | 2026-05-01 | [[media/social/x-siddhxrth10-20260430]] |
"""


def _seed_state(tmp_path):
    list_path = tmp_path / "lists" / "to-read.md"
    list_path.parent.mkdir(parents=True, exist_ok=True)
    list_path.write_text(QUEUE_FIXTURE)
    return list_path


def test_resolves_nishank_by_author_name(tmp_path, monkeypatch):
    """The v100 regression: 'Nishank's article' must NOT match the
    qoves.com row (which it did via `last` pre-fix). brain-find scores
    posted_by-substring strongly enough that Nishank wins outright."""
    mod = _import()
    monkeypatch.setattr(mod, "LIST_PATH", _seed_state(tmp_path))

    result = mod.find("Nishank's article")
    assert result["ok"] is True
    assert result["best"] == "media/social/x-nishank-20260512"
    assert result["confident"] is True
    # The qoves row must not even be a candidate
    slugs = [c["slug"] for c in result["candidates"]]
    assert "sources/qoves-com-qoves-start-your-glow-up-without-surgery" not in slugs


def test_resolves_handle_only(tmp_path, monkeypatch):
    """User says 'what does @tobi say' — direct handle match should
    dominate over any title overlap with other rows."""
    mod = _import()
    monkeypatch.setattr(mod, "LIST_PATH", _seed_state(tmp_path))
    result = mod.find("what does @tobi say about the shop floor")
    assert result["ok"] is True
    assert result["best"] == "media/social/x-tobi-20260509"


def test_resolves_qoves_by_topic_keyword(tmp_path, monkeypatch):
    """Topic-only reference. 'qoves' isn't a handle, it's the URL host
    + title prefix. Falls back to URL host / title substring scoring."""
    mod = _import()
    monkeypatch.setattr(mod, "LIST_PATH", _seed_state(tmp_path))
    result = mod.find("the qoves page")
    assert result["ok"] is True
    assert result["best"] == "sources/qoves-com-qoves-start-your-glow-up-without-surgery"


def test_paulg_keyword_resolves_to_paulgraham_pages(tmp_path, monkeypatch):
    """'paulg founder mode' must hit the foundermode essay, not the
    getideas one. Title substring match disambiguates."""
    mod = _import()
    monkeypatch.setattr(mod, "LIST_PATH", _seed_state(tmp_path))
    result = mod.find("paulg founder mode")
    assert result["ok"] is True
    assert result["best"] == "sources/paulgraham-foundermode"
    # The other Paul Graham row should still appear as a candidate so
    # the agent can surface ambiguity if needed
    candidate_slugs = [c["slug"] for c in result["candidates"]]
    assert "sources/paulgraham-foundermode" in candidate_slugs


def test_empty_query_or_pure_stopwords_returns_no_match(tmp_path, monkeypatch):
    """Pathological inputs from agent paraphrasing the user's words —
    must not pick a random row."""
    mod = _import()
    monkeypatch.setattr(mod, "LIST_PATH", _seed_state(tmp_path))
    result = mod.find("the article please")
    assert result["ok"] is False
    assert result["reason"] == "no_match"
    assert result["best"] == "" if "best" in result else True  # no best field on no_match
    assert "show bookmarks" in result["user_message"].lower() or "doesn't see" in result["user_message"].lower() or "don't see" in result["user_message"].lower()


def test_no_brain_page_rows_are_skipped(tmp_path, monkeypatch):
    """A queued bookmark whose brain_page slot is empty (still
    in-flight from the async worker) must NOT be a match candidate —
    brain-ask would fail with not_found on an empty slug."""
    mod = _import()
    list_path = tmp_path / "lists" / "to-read.md"
    list_path.parent.mkdir(parents=True, exist_ok=True)
    list_path.write_text(
        "# To read\n\n## Queue\n\n"
        "| # | Title | Posted by | One-liner | Kind | URL | Added | Brain page |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| 1 | pending row | @nishanksingla |  | x_thread | https://x.com/nishanksingla/status/X | 2026-05-14 |  |\n"
    )
    monkeypatch.setattr(mod, "LIST_PATH", list_path)
    result = mod.find("Nishank")
    assert result["ok"] is False
    assert result["reason"] == "no_match"


def test_gbrain_search_fallback_when_lexical_empty(tmp_path, monkeypatch):
    """When nothing matches lexically, brain-find calls gbrain search.
    The fallback path returns score=0.5 and tier='semantic'."""
    mod = _import()
    monkeypatch.setattr(mod, "LIST_PATH", _seed_state(tmp_path))

    def fake_run(cmd, *a, **k):
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[1] == "search":
            return subprocess.CompletedProcess(
                cmd, 0,
                "media/social/x-nishank-20260512  score=0.81\n"
                "sources/paulgraham-foundermode    score=0.42\n",
                "",
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    # Use a query that has zero lexical overlap with any row's
    # handle / posted_by / title / URL host / slug. (Earlier draft
    # used "anti-survivorship" which incidentally matched "survivorship"
    # in Nishank's title — defeated the purpose of testing the
    # semantic fallback.)
    result = mod.find("elephant kazoo orchestration")
    assert result["ok"] is True
    assert result["candidates"][0]["tier"] == "semantic"
    assert result["best"] == "media/social/x-nishank-20260512"


def test_tokenize_query_strips_stopwords_and_punctuation(tmp_path, monkeypatch):
    mod = _import()
    tokens = mod._tokenize_query("What are the top 3 takeaways from Nishank's article?")
    # Stopwords like 'the', 'are', 'from', 'article', 'top', 'three',
    # 'takeaways' must be dropped; 'nishank' must survive
    assert "nishank" in tokens
    assert "the" not in tokens
    assert "top" not in tokens
    assert "takeaways" not in tokens
    assert "article" not in tokens


def test_score_function_explains_match_reason(tmp_path, monkeypatch):
    """The agent might want to surface 'I picked X because the handle
    matched' on ambiguous queries. The reason string must be present
    and non-trivial."""
    mod = _import()
    row = {
        "title": "ambition, survivorship bias",
        "posted_by": "Nishank Singla (@nishanksingla)",
        "url": "https://x.com/nishanksingla/status/X",
        "brain_page": "media/social/x-nishank-20260512",
    }
    score, reason = mod._score(["nishanksingla"], row)
    assert score >= 1.0
    assert "handle==nishanksingla" in reason


def test_confidence_marker_when_top_dominates(tmp_path, monkeypatch):
    mod = _import()
    monkeypatch.setattr(mod, "LIST_PATH", _seed_state(tmp_path))
    # 'Nishank' uniquely matches handle (score 1.0); nothing else scores
    # > 0.4 → confident=True
    result = mod.find("Nishank")
    assert result["confident"] is True


def test_confidence_low_when_ambiguous(tmp_path, monkeypatch):
    """'paulgraham' matches BOTH paulg rows on URL host. Top score
    should not dominate (within 1.5x of #2) → confident=False so the
    agent can ask which one."""
    mod = _import()
    monkeypatch.setattr(mod, "LIST_PATH", _seed_state(tmp_path))
    result = mod.find("paulgraham")
    assert result["ok"] is True
    # Both paulgraham rows present as candidates
    pg_candidates = [c for c in result["candidates"] if "paulgraham" in c["slug"]]
    assert len(pg_candidates) >= 2
    assert result["confident"] is False
