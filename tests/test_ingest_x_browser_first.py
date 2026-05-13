"""End-to-end-ish tests for ingest-x-thread.py browser-first flow
(introduced 2026-05-13 after the v99 cookie-import failure).

These tests stub `browser-harness` + the X API + the LLM at the
process / network boundary so the script runs all the way through
`main()` against the failing tobi URL without needing the real VPS,
real bearer token, or real Chrome instance.

Three branches matter:

  1. Browser-harness returns a rich tweet body (cookies present).
     Path: `browser` or `browser+api`. Brain page is the real LLM
     output. Slug is `media/social/x-tobi-YYYYMMDD`.

  2. Browser-harness lands on the X login wall (cookies absent).
     Path: API fallback when token + bundle available; otherwise
     stub brain page.

  3. Both browser-harness AND X API unavailable.
     Path: stub brain page. Still `ok: true`. Slug is computed from
     the URL handle alone. Brain page documents the cookie-import
     remediation.

The script must NEVER emit ok: false for a well-formed public X
URL — that's what triggered the agent's improvised /site-login
reply in image-v99.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "infra" / "ingest-x-thread.py"

TOBI_URL = "https://x.com/tobi/status/2053121182044451016"


def _import_script(monkeypatch=None):
    spec = importlib.util.spec_from_file_location("ingest_x_thread", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ingest_x_thread"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _capture_stdout(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    return buf


def _last_json_envelope(text):
    """Script writes one JSON envelope to stdout (final line)."""
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON envelope in stdout:\n{text}")


# ---------------------------------------------------------------------------
# Branch 1: browser succeeds with rich content
# ---------------------------------------------------------------------------

def test_browser_first_rich_tweet_writes_real_brain_page(tmp_path, monkeypatch):
    mod = _import_script()

    # Isolate brain dir to tmp
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    # gbrain calls become no-ops
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))

    # Browser returns a rich tweet body
    monkeypatch.setattr(mod, "fetch_via_browser_primary", lambda url, timeout_s=90: {
        "ok": True,
        "page_text": (
            "tobi lutke @tobi · Apr 30\n\n"
            "Learning on the Shop floor: every founder should spend a "
            "month inside the operating system of their own product. The "
            "feedback loop you build there is the one your team will "
            "copy when you're not in the room.\n\n"
            "13 replies · 247 reposts · 1.9K likes"
        ),
        "alt_texts": "Diagram of the Shopify ops cadence",
        "title": "tobi lutke (@tobi) on X",
        "final_url": url,
        "login_wall": False,
        "cookies_injected": True,
    })

    # No X API token — exercise pure-browser path
    monkeypatch.setattr(mod, "vault_get", lambda key: "")
    # Cross-reference becomes a no-op (no entities matter here)
    monkeypatch.setattr(mod, "cross_reference", lambda slug, page: [])
    # LLM stub returns a believable brain page text
    fake_llm_page = (
        "# tobi: Learning on the Shop floor\n\n"
        "**Author:** tobi lutke (@tobi) | **Date:** 2026-04-30 | **Link:** " + TOBI_URL + "\n\n"
        "## Why this matters (Antriksh's frame)\n"
        "Connects directly to [[concepts/founder-mode]] — the case for staying "
        "embedded in your product's operating loop rather than abstracting up.\n\n"
        "## The argument\n"
        "Spend a month on the floor; the feedback loop you build there will be "
        "the one your team copies when you are not in the room.\n"
    )
    monkeypatch.setattr(mod, "analyze", lambda bundle, url, prefetched_extra_context="": fake_llm_page)

    monkeypatch.setattr(sys, "argv", ["ingest-x-thread.py", TOBI_URL])
    buf = _capture_stdout(monkeypatch)

    mod.main()
    env = _last_json_envelope(buf.getvalue())

    assert env["ok"] is True
    assert env["handle"] == "tobi"
    assert env["slug"].startswith("media/social/x-tobi-")
    assert env["source"] == "browser"
    assert "founder" in env["insight_one_liner"].lower() or env["insight_one_liner"]
    # Brain page actually written to disk
    page_path = tmp_path / f"{env['slug']}.md"
    assert page_path.exists()
    written = page_path.read_text()
    assert "Learning on the Shop floor" in written
    # No cookie-import boilerplate leaked into the page
    assert "Cookie import" not in written
    assert "/site-login" not in written


# ---------------------------------------------------------------------------
# Branch 2: browser hits login wall, API fallback succeeds
# ---------------------------------------------------------------------------

def test_browser_login_wall_falls_back_to_api(tmp_path, monkeypatch):
    mod = _import_script()
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))

    # Browser comes back, but page body is the X login wall
    monkeypatch.setattr(mod, "fetch_via_browser_primary", lambda url, timeout_s=90: {
        "ok": True,
        "page_text": (
            "Don't miss what's happening\n"
            "People on X are the first to know.\n"
            "Sign up for X\n"
            "Log in to X\n"
        ),
        "alt_texts": "",
        "title": "X. It's what's happening",
        "final_url": url,
        "login_wall": True,
        "cookies_injected": False,
    })

    # API token is present, API path returns a real bundle
    monkeypatch.setattr(mod, "vault_get", lambda key: "FAKE_TOKEN" if key == "X_API_BEARER_TOKEN" else "")
    fake_api_bundle = {
        "handle": "tobi",
        "name": "tobi lutke",
        "bio": "shopify founder",
        "seed_id": "2053121182044451016",
        "seed_created_at": "2026-04-30T12:34:56.000Z",
        "canonical_thread": [{"text": "Learning on the Shop floor", "created_at": "2026-04-30T12:34:56.000Z"}],
        "referenced_tweets": [],
        "notable_replies": [],
        "linked_urls": [],
    }
    monkeypatch.setattr(mod, "try_api_path", lambda url, token: (fake_api_bundle, None))
    monkeypatch.setattr(mod, "cross_reference", lambda slug, page: [])
    monkeypatch.setattr(mod, "analyze", lambda bundle, url, prefetched_extra_context="": "# tobi: API-only fallback\n\n## Why this matters (Antriksh's frame)\nFallback page.\n")

    monkeypatch.setattr(sys, "argv", ["ingest-x-thread.py", TOBI_URL])
    buf = _capture_stdout(monkeypatch)
    mod.main()
    env = _last_json_envelope(buf.getvalue())

    assert env["ok"] is True
    assert env["source"] == "api"
    assert env["slug"] == "media/social/x-tobi-20260430"


# ---------------------------------------------------------------------------
# Branch 3: both paths fail — stub brain page
# ---------------------------------------------------------------------------

def test_browser_and_api_both_unavailable_writes_stub(tmp_path, monkeypatch):
    """The v99 scenario: browser hits login wall AND the X API can't
    return content. The script MUST still emit ok=true with a slug,
    write a stub brain page documenting the cookie-import next step,
    so the reading-list worker has something to point at and the agent
    never has an excuse to improvise."""
    mod = _import_script()
    monkeypatch.setattr(mod, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))

    monkeypatch.setattr(mod, "fetch_via_browser_primary", lambda url, timeout_s=90: {
        "ok": True,
        "page_text": "Don't miss what's happening\nSign up for X\nLog in to X",
        "alt_texts": "",
        "title": "X. It's what's happening",
        "final_url": url,
        "login_wall": True,
        "cookies_injected": False,
    })
    # API path also fails (no token / 401 / whatever)
    monkeypatch.setattr(mod, "vault_get", lambda key: "")
    monkeypatch.setattr(mod, "try_api_path", lambda url, token: (None, "api_unauthorized"))

    monkeypatch.setattr(sys, "argv", ["ingest-x-thread.py", TOBI_URL])
    buf = _capture_stdout(monkeypatch)
    mod.main()
    env = _last_json_envelope(buf.getvalue())

    # The critical assertions — these are the v99 regression
    assert env["ok"] is True, f"stub path must emit ok=true: {env}"
    assert env["handle"] == "tobi"
    assert env["slug"].startswith("media/social/x-tobi-")
    assert env["source"] == "stub"
    assert env["insight_one_liner"]
    assert "cookie" in env["insight_one_liner"].lower()
    # Stub brain page was written
    page_path = tmp_path / f"{env['slug']}.md"
    assert page_path.exists()
    written = page_path.read_text()
    # Has the structure of a real brain page (so it shows up in gbrain)
    assert "# tobi" in written
    assert TOBI_URL in written
    # Contains the cookie-import next step
    assert "login to x.com" in written.lower() or "site-login" in written.lower()
    assert "migrate bookmarks" in written


# ---------------------------------------------------------------------------
# Login-wall detector
# ---------------------------------------------------------------------------

def test_looks_like_login_wall_pattern_matching():
    mod = _import_script()
    # Two distinct X login-wall markers — should match
    assert mod.looks_like_login_wall(
        "Don't miss what's happening\nPeople on X are the first to know.\n"
    )
    # A real tweet body that incidentally has "Sign up for X" in a side
    # prompt — single marker, shouldn't match
    assert not mod.looks_like_login_wall(
        "Learning on the Shop floor: a long thread by Tobi about staying "
        "embedded in your product. Sign up for X to reply."
    )
    # Empty / None / very short
    assert not mod.looks_like_login_wall("")
    assert not mod.looks_like_login_wall(None)


# ---------------------------------------------------------------------------
# handle_from_url
# ---------------------------------------------------------------------------

def test_handle_from_url():
    mod = _import_script()
    assert mod.handle_from_url("https://x.com/tobi/status/2053121182044451016") == "tobi"
    assert mod.handle_from_url("https://twitter.com/Manthanguptaa/status/12345") == "Manthanguptaa"
    assert mod.handle_from_url("https://x.com/tobi/status/123?s=12") == "tobi"
    assert mod.handle_from_url("https://x.com/some/other/path") == ""


# ---------------------------------------------------------------------------
# browser_to_bundle synthesizes a usable API-shaped bundle
# ---------------------------------------------------------------------------

def test_browser_to_bundle_extracts_display_name_from_title():
    mod = _import_script()
    ctx = {
        "page_text": "Learning on the Shop floor...",
        "alt_texts": "diagram",
        "title": "tobi lutke (@tobi) on X",
    }
    bundle = mod.browser_to_bundle(ctx, TOBI_URL, "tobi")
    assert bundle["handle"] == "tobi"
    assert bundle["name"] == "tobi lutke"
    assert bundle["canonical_thread"][0]["text"].startswith("Learning")
    assert bundle["_source"] == "browser-harness"
