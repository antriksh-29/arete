"""Unit tests for reading-list rendering.

These guard the output contract documented in
skills/reading-list/SKILL.md. The 2026-05-11 screenshot bug
(three URLs per entry due to t.co shortlinks bleeding from tweet body
into the title field) lives here as test_renderer_strips_tco_from_titles
— if it ever regresses, the suite goes red before the user sees it.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "infra" / "reading-list.py"

spec = importlib.util.spec_from_file_location("reading_list", SCRIPT)
assert spec is not None and spec.loader is not None
reading_list = importlib.util.module_from_spec(spec)
sys.modules["reading_list"] = reading_list
spec.loader.exec_module(reading_list)  # type: ignore[union-attr]

render_for_telegram = reading_list.render_for_telegram
clean_cell = reading_list.clean_cell
normalize_url = reading_list.normalize_url


URL_RE = re.compile(r"https?://\S+")
MD_LINK_RE = re.compile(r"\[[^\]]+\]\([^)]+\)")


def _entry_blocks(rendered: str):
    """Slice render output into entries. Each entry starts with 'N. '
    for queue or '✓ ' for recently read; an entry's continuation
    lines run at column 0 with labels ("Posted by:", "About:") or are
    the bare canonical URL. A blank line ends an entry."""
    blocks = []
    cur = []
    in_entry = False
    for line in rendered.splitlines():
        starter = re.match(r"^\d+\.\s", line) or line.startswith("✓ ")
        if starter:
            if cur:
                blocks.append(cur)
            cur = [line]
            in_entry = True
            continue
        if in_entry:
            if line == "":
                blocks.append(cur)
                cur = []
                in_entry = False
                continue
            cur.append(line)
    if cur:
        blocks.append(cur)
    return blocks


def test_smoke_single_clean_entry():
    state = {
        "queue": [{
            "title": "Founder Mode by Paul Graham",
            "posted_by": "@paulg",
            "one_liner": "Founders should not be told to act like managers.",
            "kind": "x_thread",
            "url": "https://x.com/paulg/status/123",
            "added": "2026-05-11",
            "brain_page": "media/social/x-paulg-20260511",
        }],
        "read": [],
    }
    out = render_for_telegram(state)
    blocks = _entry_blocks(out)
    assert len(blocks) == 1
    entry = blocks[0]
    # Exactly one URL across all four lines
    urls = URL_RE.findall("\n".join(entry))
    assert urls == ["https://x.com/paulg/status/123"]
    # No markdown link syntax anywhere
    assert not MD_LINK_RE.search("\n".join(entry))
    # Posted by present
    assert any("Posted by:" in ln for ln in entry)
    # No t.co
    assert "t.co" not in out


def test_renderer_strips_tco_from_titles():
    """2026-05-11 regression: title field contained tweet body with t.co
    shortlinks, producing 3 URLs per entry. Renderer must scrub them."""
    state = {
        "queue": [{
            "title": "@manthanguptaa: https://t.co/8AGleYV2QQ https://t.co/jLcjg7o3RaPWl great research",
            "posted_by": "@manthanguptaa",
            "one_liner": "Threads on building AI agents end-to-end.",
            "kind": "x_thread",
            "url": "https://x.com/manthanguptaa/status/2049835703971053624",
            "added": "2026-05-11",
            "brain_page": "media/social/x-manthanguptaa-20260511",
        }],
        "read": [],
    }
    out = render_for_telegram(state)
    assert "t.co" not in out, f"t.co leaked into render:\n{out}"
    urls = URL_RE.findall(out)
    assert len(urls) == 1, f"expected 1 URL, got {len(urls)}:\n{out}"
    assert urls[0] == "https://x.com/manthanguptaa/status/2049835703971053624"


def test_renderer_strips_markdown_link_titles():
    """Migration from v1 schema: titles came in as [text](url). The
    renderer must unwrap them, not let the link syntax through."""
    state = {
        "queue": [{
            "title": "[Founder Mode](https://paulgraham.com/foundermode.html)",
            "posted_by": "paulgraham.com",
            "one_liner": "",
            "kind": "html",
            "url": "https://paulgraham.com/foundermode.html",
            "added": "2026-05-08",
            "brain_page": "",
        }],
        "read": [],
    }
    out = render_for_telegram(state)
    assert not MD_LINK_RE.search(out), f"markdown link leaked:\n{out}"
    assert "Founder Mode" in out
    urls = URL_RE.findall(out)
    assert urls == ["https://paulgraham.com/foundermode.html"]


def test_each_entry_has_exactly_one_url_across_mixed_kinds():
    state = {
        "queue": [
            {
                "title": "Manthan's agent thread",
                "posted_by": "@manthanguptaa",
                "one_liner": "What founders building agent products keep getting wrong.",
                "kind": "x_thread",
                "url": "https://x.com/manthanguptaa/status/2049835703971053624",
                "added": "2026-05-11",
                "brain_page": "media/social/x-manthanguptaa-20260511",
            },
            {
                "title": "How transformers actually work",
                "posted_by": "3Blue1Brown",
                "one_liner": "The attention mechanism explained with vector intuition.",
                "kind": "youtube",
                "url": "https://www.youtube.com/watch?v=eURcW5_uS60",
                "added": "2026-05-11",
                "brain_page": "media/youtube/3blue1brown-eURcW5_uS60",
            },
        ],
        "read": [],
    }
    out = render_for_telegram(state)
    blocks = _entry_blocks(out)
    assert len(blocks) == 2
    for entry in blocks:
        joined = "\n".join(entry)
        urls = URL_RE.findall(joined)
        assert len(urls) == 1, f"entry has {len(urls)} URLs:\n{joined}"
        assert "t.co" not in joined
        assert not MD_LINK_RE.search(joined)


def test_posted_by_falls_back_to_host_when_missing():
    state = {
        "queue": [{
            "title": "Some article",
            "posted_by": "",
            "one_liner": "",
            "kind": "html",
            "url": "https://example.blog/post/123",
            "added": "2026-05-11",
            "brain_page": "",
        }],
        "read": [],
    }
    out = render_for_telegram(state)
    assert "Posted by: example.blog" in out


def test_no_leading_whitespace_on_any_line():
    """Render must not rely on indentation — some Telegram clients and
    agent reply pipelines strip leading whitespace and would collapse
    the entry into a single line if we did."""
    state = {
        "queue": [{
            "title": "Founder Mode",
            "posted_by": "@paulg",
            "one_liner": "Founders should not act like managers.",
            "kind": "x_thread",
            "url": "https://x.com/paulg/status/1",
            "added": "2026-05-11",
            "brain_page": "media/social/x-paulg-20260511",
        }],
        "read": [],
    }
    out = render_for_telegram(state)
    for line in out.splitlines():
        assert line == "" or not line.startswith(" "), f"line has leading whitespace: {line!r}"


def test_continuation_lines_have_explicit_labels():
    """Posted-by and one-liner must be label-prefixed so they survive
    any agent text munging."""
    state = {
        "queue": [{
            "title": "Founder Mode",
            "posted_by": "@paulg",
            "one_liner": "Founders should not act like managers.",
            "kind": "x_thread",
            "url": "https://x.com/paulg/status/1",
            "added": "2026-05-11",
            "brain_page": "media/social/x-paulg-20260511",
        }],
        "read": [],
    }
    out = render_for_telegram(state)
    assert "Posted by: @paulg" in out
    assert "About: Founders should not act like managers." in out


def test_one_liner_omitted_line_when_empty():
    state = {
        "queue": [{
            "title": "Untitled",
            "posted_by": "site.com",
            "one_liner": "",
            "kind": "html",
            "url": "https://site.com/x",
            "added": "2026-05-11",
            "brain_page": "",
        }],
        "read": [],
    }
    out = render_for_telegram(state)
    blocks = _entry_blocks(out)
    assert len(blocks) == 1
    # Three lines (title, posted-by, url) when one_liner is empty
    assert len(blocks[0]) == 3
    assert "About:" not in out


def test_recently_read_renders_with_checkmark():
    state = {
        "queue": [],
        "read": [{
            "title": "Founder Mode",
            "posted_by": "paulgraham.com",
            "one_liner": "Founders should not act like managers.",
            "kind": "html",
            "url": "https://paulgraham.com/foundermode.html",
            "added": "2026-05-01",
            "read": "2026-05-10",
            "brain_page": "sources/paulgraham-foundermode",
        }],
    }
    out = render_for_telegram(state)
    assert "Recently read:" in out
    assert "✓ Founder Mode" in out


def test_normalize_url_strips_tracking_params():
    assert normalize_url("https://youtu.be/abc?si=xyz123") == "https://youtu.be/abc"
    assert normalize_url("https://x.com/a/status/1?s=20&t=foo") == "https://x.com/a/status/1"
    assert normalize_url("https://example.com/p?utm_source=x&keep=1") == "https://example.com/p?keep=1"


def test_clean_cell_truncates_and_collapses_whitespace():
    s = "a  b\tc\n\n d" + " e" * 200
    out = clean_cell(s, max_len=50)
    assert "\n" not in out and "\t" not in out
    assert len(out) <= 50
    assert "  " not in out


def test_clean_cell_strips_urls_when_requested():
    s = "Read more at https://example.com/x and tell me"
    assert clean_cell(s, strip_urls=True).count("http") == 0
