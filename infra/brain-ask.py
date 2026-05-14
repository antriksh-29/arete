#!/usr/bin/env python3
# brain-ask.py — answer a question against a specific brain page.
#
# Why this exists: the agent has been reading bookmark row metadata
# (title, posted_by, one_liner) from chat context and rephrasing it as
# if that were the article content. When the user asks "quote Nishank"
# the agent says "I can't quote — thread text isn't in what I have"
# instead of running `gbrain get` and reading the actual brain page.
# This script makes brain-page lookup a single tool call the agent can
# invoke, so there's no excuse to improvise from summary.
#
# Resolution order for the source content:
#   1. ~/.brain/_raw/<slug-basename>.raw.txt           (Layer 2 sidecar — verbatim rendered page from browser-harness)
#   2. ~/.brain/<slug>.md                               (the brain page itself — agent analysis + Raw thread section)
#   3. gbrain get <slug>                                (fallback if the file is missing on disk)
#
# Usage:
#   brain-ask.py <slug-or-pointer> "<question>"
#     slug-or-pointer  →  "media/social/x-nishank-20260512"
#                          OR "last"  (reads ~/.brain/.last-bookmark.json)
#                          OR a URL we have bookmarked (looked up in lists/to-read)
#
# Prints a single-line JSON envelope on stdout:
#   {"ok": true, "slug": "...", "answer": "...", "user_message": "...",
#    "source": "raw_sidecar"|"brain_page"|"gbrain_get",
#    "raw_chars": N, "page_chars": N}
#
# The "user_message" field is what the agent should relay verbatim to
# Telegram. The "answer" field is the same content without any envelope
# wrapping — useful for downstream skills that want the raw text.

import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import date
from pathlib import Path

BRAIN_DIR = Path.home() / ".brain"
RAW_DIR = BRAIN_DIR / "_raw"
LAST_BOOKMARK_PATH = BRAIN_DIR / ".last-bookmark.json"
GBRAIN = "/home/botuser/.bun/bin/gbrain"
TODAY = date.today().isoformat()

os.environ["PATH"] = "/home/botuser/.bun/bin:" + os.environ.get("PATH", "")


def log(msg):
    print(f"[brain-ask] {msg}", file=sys.stderr)


SYSTEM_PROMPT = """You are answering a question about a piece of writing that the user has bookmarked into their personal knowledge brain. The brain page and (when available) the verbatim source text are below.

Rules:
1. Answer ONLY from the material below. If the answer isn't in the material, say so plainly — do NOT speculate, do NOT pad with generic advice, do NOT pretend you know what wasn't captured.
2. When the user asks for quotes, attribution, or exact phrasing, quote VERBATIM from the Raw source section. Wrap quotes in straight quotes. Do not paraphrase.
3. When you cite, cite the section. Format: (Brain page §The argument) or (Raw source ¶N) where ¶N is the rough paragraph number. Inline, not as footnotes.
4. Keep answers tight. Bulleted lists when the user asks for "top N" or "key points". Continuous prose when they ask "what does X say" or "explain".
5. The user's name is Antriksh. Address him directly when the answer is opinion-shaped ("you might want to…"). Never call him "the user".

Output format: just the answer. No preamble, no "Here are the key points:", no closing summary. Just the substantive content."""


def _load_raw_sidecar(slug):
    """Return the verbatim source text written by the ingestor's
    browser-harness pass, if present."""
    basename = slug.split("/")[-1]
    candidates = [
        RAW_DIR / f"{basename}.raw.txt",
        RAW_DIR / f"{basename}.raw.md",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(), str(path)
    return "", ""


def _load_brain_page(slug):
    """Return the brain page body — file-on-disk first, gbrain get as
    fallback. Returns (text, source_tag) so the caller can report which
    path it came from."""
    page_path = BRAIN_DIR / f"{slug}.md"
    if page_path.exists():
        return page_path.read_text(), "brain_page"
    r = subprocess.run([GBRAIN, "get", slug], capture_output=True, text=True)
    if r.returncode == 0:
        return r.stdout, "gbrain_get"
    return "", ""


def _normalize_url(url):
    """Mirror reading-list.py's normalize_url so URL→slug lookup
    matches what the queue stores. Strips tracking params."""
    p = urllib.parse.urlsplit(url.strip())
    drop = {"s", "t", "si", "utm_source", "utm_medium", "utm_campaign",
            "utm_term", "utm_content", "ref", "ref_src", "feature", "fbclid", "gclid"}
    qs = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if k.lower() not in drop]
    new_q = urllib.parse.urlencode(qs)
    path = p.path.rstrip("/") if p.path != "/" else p.path
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), path, new_q, ""))


URL_RE = re.compile(r"\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*\[\[([^\]]+)\]\]\s*\|\s*$")
V2_QUEUE_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*$"
)


def _resolve_url_to_slug(url):
    """Search lists/to-read.md for a row whose URL matches. Returns
    slug (the wikilink target in the last column) or empty string."""
    list_path = BRAIN_DIR / "lists" / "to-read.md"
    if not list_path.exists():
        return ""
    target = _normalize_url(url)
    for line in list_path.read_text().splitlines():
        m = V2_QUEUE_RE.match(line)
        if not m:
            continue
        row_url = _normalize_url(m.group(5))
        if row_url == target:
            slug = m.group(7).strip()
            return slug.strip("[]").strip()
    return ""


SLUG_RE = re.compile(r"^[a-z][\w.-]*\/[\w./-]+$")


def _resolve_via_brain_find(query):
    """Shell out to brain-find.py for free-text resolution. Returns
    (slug, candidates_list, confident_bool) — slug is empty when no
    match. We use a subprocess instead of importing so brain-find's
    failures (gbrain not on PATH, brain index missing) don't poison
    this script."""
    here = Path(__file__).resolve().parent
    bf = here / "brain-find.py"
    if not bf.exists():
        bf = Path("/home/botuser/bin/brain-find.py")
    if not bf.exists():
        return "", [], False
    try:
        r = subprocess.run(
            ["python3", str(bf), query],
            capture_output=True, text=True, timeout=45, check=False,
        )
    except Exception as e:
        log(f"brain-find invocation failed: {e}")
        return "", [], False
    if r.returncode != 0:
        return "", [], False
    try:
        env = json.loads(r.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return "", [], False
    if not env.get("ok"):
        return "", env.get("candidates", []), False
    return env.get("best", ""), env.get("candidates", []), bool(env.get("confident"))


def _resolve_pointer(pointer):
    """Map the CLI argument to a brain page slug. Accepts:
      - a literal slug ("media/social/x-nishank-20260512")
      - "last" — read ~/.brain/.last-bookmark.json
      - a URL — look it up in lists/to-read.md
      - free-text — delegated to brain-find.py (since 2026-05-14
        when the v100 follow-up probe showed `last` was a bad default
        for keyword references like "Nishank's article")
    Returns the slug or empty string. The free-text path returns ""
    on no-match — the caller decides how to surface that.
    """
    if pointer.startswith("http://") or pointer.startswith("https://"):
        return _resolve_url_to_slug(pointer)
    if pointer.lower() == "last":
        if LAST_BOOKMARK_PATH.exists():
            try:
                data = json.loads(LAST_BOOKMARK_PATH.read_text())
                return data.get("slug", "")
            except (json.JSONDecodeError, OSError):
                return ""
        return ""
    # Literal slug shape — pass through
    if SLUG_RE.match(pointer) and " " not in pointer:
        return pointer
    # Anything else → free-text via brain-find
    slug, _candidates, _confident = _resolve_via_brain_find(pointer)
    return slug


def ask(slug, question):
    raw_text, raw_source = _load_raw_sidecar(slug)
    page_text, page_source = _load_brain_page(slug)
    if not raw_text and not page_text:
        return {
            "ok": False, "slug": slug, "reason": "not_found",
            "user_message": f"🧠 No brain page at `{slug}`. Bookmark or ingest the URL first, then ask again.",
        }

    # Assemble the LLM payload. Even when both are present, send both —
    # the brain page carries the LLM's prior analysis + entity links,
    # the raw sidecar carries verbatim source quotes.
    parts = [f"BRAIN PAGE SLUG: {slug}\n"]
    if page_text:
        parts.append(f"BRAIN PAGE BODY ({len(page_text)} chars):\n---\n{page_text[:30000]}\n---")
    if raw_text:
        parts.append(f"RAW SOURCE ({len(raw_text)} chars, verbatim rendered page):\n---\n{raw_text[:30000]}\n---")
    parts.append(f"USER QUESTION: {question}")
    user_msg = "\n\n".join(parts)

    # Import llm lazily so unit tests can stub before import.
    sys.path.insert(0, "/home/botuser/bin")
    import llm  # type: ignore
    text, provider = llm.call(
        system_prompt=SYSTEM_PROMPT,
        user_msg=user_msg,
        tier="mini",
        max_tokens=2500,
    )
    answer = text.strip()
    log(f"answered slug={slug} provider={provider} raw={bool(raw_text)} page={bool(page_text)}")

    # The agent relays user_message verbatim to Telegram; keep it lean,
    # no envelope chrome. Add a single citation line at the bottom for
    # the brain page slug, so the user can navigate to the source.
    #
    # Plain-text format on purpose: earlier we tried `— from [[<slug>]]`
    # and the agent stripped it on relay (treated the wikilink as
    # internal-only). The 📎 emoji + bare slug survives because it
    # looks like a normal user-facing reference.
    user_message = answer + f"\n\n📎 source: {slug}"
    return {
        "ok": True,
        "slug": slug,
        "answer": answer,
        "user_message": user_message,
        "source": raw_source or page_source or "gbrain_get",
        "raw_chars": len(raw_text),
        "page_chars": len(page_text),
        "provider": provider,
    }


def main():
    if len(sys.argv) < 3:
        sys.exit('Usage: brain-ask.py <slug|"last"|url> "<question>"')
    pointer = sys.argv[1]
    question = sys.argv[2]
    if question == "--smoke":
        print(json.dumps({"ok": True, "skill": "brain-ask", "user_message": "brain-ask smoke ok"}))
        return

    slug = _resolve_pointer(pointer)
    if not slug:
        print(json.dumps({
            "ok": False, "reason": "no_slug_resolvable",
            "user_message": f"🧠 Couldn't resolve `{pointer}` to a brain page. Pass an explicit slug like `media/social/x-...` or run `add to reading list <url>` first.",
        }))
        return

    result = ask(slug, question)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
