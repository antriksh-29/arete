#!/usr/bin/env python3
# General HTML / web article ingestion. Fetches the page, extracts title +
# meta description + readable body text, runs our analysis on top, and
# writes a brain page at sources/<domain>-<slug>.md.
#
# For JS-heavy sites or login-walled pages the agent should invoke
# agent-browser directly. This script handles the static-HTML majority.
#
# Usage:
#   ingest-html.py <url>
#
# Prints a single-line JSON envelope to stdout.

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

BRAIN_DIR = Path.home() / ".brain"
GBRAIN = "${STATE_HOME}/.bun/bin/gbrain"
TODAY = date.today().isoformat()

os.environ["PATH"] = "${STATE_HOME}/.bun/bin:" + os.environ.get("PATH", "")


def log(msg):
    print(f"[ingest-html] {msg}", file=sys.stderr)


def slugify(s):
    s = re.sub(r"[^A-Za-z0-9]+", "-", s.lower()).strip("-")
    return s[:60] or "untitled"


def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Arete ingest-html)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read(2_000_000)
        enc = r.headers.get_content_charset() or "utf-8"
        return raw.decode(enc, errors="replace"), r.geturl()


def fetch_via_browser(url, timeout_s=90):
    """Render the page in browser-harness's headless Chrome and return
    {title, description, author, body, final_url}. Used when the static
    fetch returns a JS-app shell or a login wall."""
    import shutil
    bh = shutil.which("browser-harness") or os.path.expanduser("~/.local/bin/browser-harness")
    if not os.path.isfile(bh):
        log("browser-harness CLI not found; skipping JS render fallback")
        return None
    script = (
        "import time, json\n"
        f"new_tab({url!r})\n"
        "wait_for_load()\n"
        "time.sleep(3)\n"
        "info = page_info()\n"
        "text = cdp('Runtime.evaluate', expression='document.body.innerText', returnByValue=True).get('result',{}).get('value','')\n"
        "metas = cdp('Runtime.evaluate', expression='JSON.stringify(Array.from(document.querySelectorAll(\\\"meta[property],meta[name]\\\")).map(m=>({k:m.getAttribute(\\\"property\\\")||m.getAttribute(\\\"name\\\"),v:m.getAttribute(\\\"content\\\")||\\\"\\\"})))', returnByValue=True).get('result',{}).get('value','[]')\n"
        "print('---BH-INFO---'); print(json.dumps(info)[:8000])\n"
        "print('---BH-BODY---'); print(text[:60000])\n"
        "print('---BH-META---'); print(metas[:6000])\n"
        "print('---BH-END---')\n"
    )
    env = {**os.environ, "BU_CDP_URL": "http://127.0.0.1:9222"}
    try:
        r = subprocess.run([bh], input=script, capture_output=True, text=True, timeout=timeout_s, env=env, check=False)
    except Exception as e:
        log(f"browser-harness invocation failed: {e}")
        return None
    if r.returncode != 0:
        log(f"browser-harness exit {r.returncode}: {r.stderr[-300:]}")
        return None
    out = r.stdout
    def slab(marker_a, marker_b):
        if marker_a not in out or marker_b not in out:
            return ""
        return out.split(marker_a, 1)[1].split(marker_b, 1)[0].strip()
    body = slab("---BH-BODY---", "---BH-META---")
    metas_raw = slab("---BH-META---", "---BH-END---")
    info_raw = slab("---BH-INFO---", "---BH-BODY---")
    try:
        metas = json.loads(metas_raw) if metas_raw else []
    except Exception:
        metas = []
    meta_map = {(m.get("k") or "").lower(): m.get("v") or "" for m in metas if isinstance(m, dict)}
    try:
        info = json.loads(info_raw) if info_raw else {}
    except Exception:
        info = {}
    return {
        "title": meta_map.get("og:title") or info.get("title", "") or "",
        "description": meta_map.get("og:description") or meta_map.get("description") or meta_map.get("twitter:description") or "",
        "author": meta_map.get("author") or meta_map.get("article:author") or meta_map.get("og:site_name") or "",
        "body": body[:60000],
        "final_url": info.get("url", url),
    }


SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
META_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\']([^"\']+)["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)


def extract(html):
    meta = {}
    for m in META_RE.finditer(html):
        meta[m.group(1).lower()] = m.group(2)
    title_m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE | re.DOTALL)
    title = WS_RE.sub(" ", title_m.group(1)).strip() if title_m else ""
    title = meta.get("og:title") or title
    description = (
        meta.get("og:description")
        or meta.get("description")
        or meta.get("twitter:description")
        or ""
    )
    author = (
        meta.get("author")
        or meta.get("article:author")
        or meta.get("og:site_name")
        or ""
    )
    body_html = SCRIPT_STYLE_RE.sub(" ", html)
    body_text = WS_RE.sub(" ", TAG_RE.sub(" ", body_html)).strip()
    return {"title": title, "description": description, "author": author, "body": body_text[:60000]}


SYSTEM_PROMPT = """You are analyzing a web article for the user's personal knowledge brain.

the user values: connections to what he already knows, sharp takes that re-frame the article, the actual argument structure (not a paragraph-by-paragraph recap). He does NOT want a generic summary.

Produce a brain page in this exact structure (markdown). Use [[slug]] syntax to reference any person, company, or concept the user might already track (people/<first-last>, companies/<name>, concepts/<topic>).

# {title}
**Author / Site:** {author} | **Date:** {date} | **Link:** {url}

## Why this matters (the user's frame)
2-4 sentences. Connect to what the user already cares about (AI agents, founder-mode, building, personal brain). Use [[slug]] where natural.

## The argument
Reconstruct the article as continuous prose — what the author is actually claiming, in the order they build it. 1-3 paragraphs.

## Connections
Bullet list of [[slug]] links to existing brain entities mentioned or implied, with a one-line reason each.

## One-line takeaway
A single sentence (≤ 200 chars) capturing the core insight. This line will be shown in the bookmark list.

---
## Raw excerpt
First ~2000 chars of the article body, verbatim. Evidence base.

No preamble. Land the structure above and stop. Do NOT wrap output in code fences."""


def analyze(meta, url):
    sys.path.insert(0, "${STATE_HOME}/bin")
    import llm  # type: ignore
    user_msg = (
        f"Title: {meta['title']}\n"
        f"Author/site: {meta['author']}\n"
        f"Meta description: {meta['description']}\n"
        f"URL: {url}\n\n"
        f"Body (truncated to 60K chars):\n---\n{meta['body']}\n---\n\n"
        f"Generate the brain page now."
    )
    text, provider = llm.call(
        system_prompt=SYSTEM_PROMPT,
        user_msg=user_msg,
        tier="mini",
        max_tokens=4000,
    )
    log(f"analyze provider={provider}")
    return text


WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def page_exists(slug):
    r = subprocess.run([GBRAIN, "get", slug], capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip().startswith("---")


def ensure_stub(slug, source_slug, source_label):
    if page_exists(slug):
        return False
    type_prefix = slug.split("/", 1)[0] if "/" in slug else "concept"
    type_map = {"people": "person", "companies": "company", "concepts": "concept", "projects": "project"}
    page_type = type_map.get(type_prefix, "concept")
    name = slug.split("/", 1)[-1].replace("-", " ").title()
    stub = (
        f"---\ntype: {page_type}\ncreated: {TODAY}\nupdated: {TODAY}\n---\n\n"
        f"# {name}\n\n"
        f"_Auto-created stub from {source_label} ingestion on {TODAY}._ "
        f"First referenced by [[{source_slug}]]. Fill in details on next mention.\n"
    )
    page_path = BRAIN_DIR / f"{slug}.md"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(stub)
    subprocess.run([GBRAIN, "put", slug], input=stub, text=True, capture_output=True, check=False)
    return True


def cross_reference(slug, page_text):
    """Defer entity stub / link / timeline-add to xref-drainer.py via
    ${BRAIN_PATH}/.xref-queue.jsonl. See ingest-x-thread.py for rationale."""
    seen = []
    for m in WIKILINK_RE.finditer(page_text):
        target = m.group(1).strip()
        if target and target != slug and "/" in target and target not in seen:
            seen.append(target)
    if not seen:
        return []
    queue_path = BRAIN_DIR / ".xref-queue.jsonl"
    BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "slug": slug,
        "entities": seen,
        "source_label": "Web article",
        "queued_at": f"{TODAY}T00:00:00Z",
    }
    try:
        with open(queue_path, "a") as f:
            f.write(json.dumps(payload) + "\n")
    except OSError as e:
        log(f"xref enqueue failed; running inline fallback: {e}")
        for target in seen:
            ensure_stub(target, slug, "Web article")
            subprocess.run([GBRAIN, "link", slug, target], capture_output=True, check=False, text=True)
            subprocess.run(
                [GBRAIN, "timeline-add", target, TODAY,
                 f"Mentioned in [[{slug}]] [Source: web article ingestion {TODAY}]"],
                capture_output=True, check=False, text=True,
            )
    return seen


def extract_one_liner(page_text):
    m = re.search(r"##\s*One-line takeaway[^\n]*\n+([^\n]+)", page_text, re.IGNORECASE)
    return (m.group(1).strip() if m else "")[:240]


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: ingest-html.py <url>")
    url = sys.argv[1]
    if url == "--smoke":
        print(json.dumps({"ok": True, "skill": "ingest-html", "user_message": "ingest-html smoke ok"}))
        return

    html = None
    final_url = url
    try:
        html, final_url = fetch_html(url)
    except Exception as e:
        log(f"static fetch failed: {e}; trying browser-harness")
    meta = extract(html) if html else {"title": "", "description": "", "author": "", "body": ""}

    # If the static fetch produced a thin shell (JS-heavy app, login wall,
    # bot detection), retry via browser-harness which renders the page.
    if len(meta.get("body", "") or "") < 600 or not meta.get("title"):
        log(f"static body thin ({len(meta.get('body','') or '')} chars); falling back to browser-harness")
        bm = fetch_via_browser(url)
        if bm and len(bm.get("body", "") or "") >= len(meta.get("body", "") or ""):
            meta = bm
            final_url = bm.get("final_url", final_url)

    if not (meta.get("body") or "").strip() and not meta.get("title"):
        print(json.dumps({"ok": False, "reason": "fetch_failed", "user_message": "🌐 Couldn't fetch the page — both static fetch and browser render returned empty."}))
        sys.exit(1)
    if not meta["title"]:
        meta["title"] = urllib.parse.urlsplit(final_url).path.strip("/").split("/")[-1] or final_url
    if not meta["author"]:
        meta["author"] = urllib.parse.urlsplit(final_url).netloc

    domain = urllib.parse.urlsplit(final_url).netloc.replace("www.", "")
    slug = f"sources/{slugify(domain)}-{slugify(meta['title'])[:40]}"

    page = analyze(meta, final_url)
    page_path = BRAIN_DIR / f"{slug}.md"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(page)
    subprocess.run([GBRAIN, "put", slug], input=page, text=True, check=False)

    # Layer 2: write the verbatim extracted body to a local-only
    # sidecar so brain-ask.py can quote exact phrases. The page itself
    # carries the LLM's analysis; the sidecar carries the source.
    raw_body = meta.get("body") or ""
    if raw_body:
        try:
            raw_dir = BRAIN_DIR / "_raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            basename = slug.split("/")[-1]
            raw_path = raw_dir / f"{basename}.raw.txt"
            raw_path.write_text(
                f"# Raw extracted body for {slug}\n"
                f"Source URL: {final_url}\n"
                f"Captured: {TODAY}\n"
                f"Page title: {meta.get('title','')}\n\n"
                f"## Extracted body (readability)\n{raw_body}\n"
            )
            log(f"wrote raw sidecar {raw_path} ({len(raw_body)} chars)")
        except Exception as e:
            log(f"raw sidecar write failed (non-fatal): {e}")

    linked = cross_reference(slug, page)
    one_liner = extract_one_liner(page)

    print(json.dumps({
        "ok": True,
        "title": meta["title"],
        "author": meta["author"],
        "url": final_url,
        "slug": slug,
        "linked_entities": linked,
        "insight_one_liner": one_liner,
        "user_message": (
            f"🌐 Ingested: {meta['title'][:80]} — {meta['author']}\n"
            f"Brain page: {slug}\n"
            f"{one_liner}"
        ),
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {e}")
        print(json.dumps({"ok": False, "reason": "error", "error": str(e), "user_message": "⚠️ HTML ingestion failed — check the logs."}))
        sys.exit(1)
