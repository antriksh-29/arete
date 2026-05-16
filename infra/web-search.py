#!/usr/bin/env python3
"""web-search.py — browser-harness backed web search.

Replaces the previous OpenAI web_search_preview implementation. Drives
the always-on headless Chrome via browser-harness (CDP at
127.0.0.1:9222) against DuckDuckGo's HTML-only endpoint, which renders
without JS so scraping is reliable.

Usage:
  web-search.py "<query>"          → markdown to stdout
  web-search.py --smoke            → JSON smoke envelope
  web-search.py --json "<query>"   → JSON envelope with results list

Output format (markdown):
  **1. <title>**
  <snippet>
  → <url>

Exit 0 on success. Exit 1 on failure (with a single-line stderr).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse


BH_BIN = shutil.which("browser-harness") or os.path.expanduser("~/.local/bin/browser-harness")
CDP_URL = "http://127.0.0.1:9222"
ENDPOINT = "https://html.duckduckgo.com/html/?q={q}"
MAX_RESULTS = 5
TIMEOUT_S = 60


def log(msg: str) -> None:
    print(f"[web-search] {msg}", file=sys.stderr)


def smoke() -> int:
    print(json.dumps({"ok": True, "skill": "web-search", "user_message": "web-search smoke ok"}))
    return 0


def drive_browser(query: str) -> str:
    """Navigate to DuckDuckGo HTML and return the rendered HTML body."""
    if not os.path.isfile(BH_BIN):
        log(f"browser-harness CLI not found at {BH_BIN}")
        sys.exit(1)
    url = ENDPOINT.format(q=urllib.parse.quote(query))
    script = (
        "import time, json\n"
        f"new_tab({url!r})\n"
        "wait_for_load()\n"
        "time.sleep(2)\n"
        "html = cdp('Runtime.evaluate', expression='document.documentElement.outerHTML', returnByValue=True).get('result',{}).get('value','')\n"
        "print('---WS-HTML---')\n"
        "print(html[:200000])\n"
        "print('---WS-END---')\n"
    )
    env = {**os.environ, "BU_CDP_URL": CDP_URL}
    try:
        r = subprocess.run(
            [BH_BIN], input=script, capture_output=True, text=True,
            timeout=TIMEOUT_S, env=env, check=False,
        )
    except subprocess.TimeoutExpired:
        log(f"browser-harness timed out after {TIMEOUT_S}s")
        sys.exit(1)
    if r.returncode != 0:
        log(f"browser-harness exit {r.returncode}: {r.stderr[-300:]}")
        sys.exit(1)
    out = r.stdout
    if "---WS-HTML---" not in out or "---WS-END---" not in out:
        log("browser-harness output missing markers")
        sys.exit(1)
    return out.split("---WS-HTML---", 1)[1].split("---WS-END---", 1)[0].strip()


# DuckDuckGo HTML result: <div class="result"> with <a class="result__a">title</a>
# and <a class="result__snippet">snippet</a>. Hrefs are wrapped through
# /l/?uddg=<encoded-target> redirects.
RESULT_BLOCK_RE = re.compile(
    r'<div[^>]*class="[^"]*\bresult\b[^"]*"[^>]*>(.+?)</div>\s*</div>',
    re.DOTALL,
)
TITLE_RE = re.compile(
    r'<a[^>]*class="[^"]*\bresult__a\b[^"]*"[^>]*href="([^"]+)"[^>]*>(.+?)</a>',
    re.DOTALL,
)
SNIPPET_RE = re.compile(
    r'<a[^>]*class="[^"]*\bresult__snippet\b[^"]*"[^>]*>(.+?)</a>',
    re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_text(html: str) -> str:
    text = TAG_RE.sub("", html)
    text = WS_RE.sub(" ", text).strip()
    # decode common entities
    for esc, ch in (("&amp;", "&"), ("&quot;", '"'), ("&#x27;", "'"),
                    ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")):
        text = text.replace(esc, ch)
    return text


def unwrap_redirect(href: str) -> str:
    """DuckDuckGo wraps result links through /l/?uddg=<encoded>."""
    if href.startswith("//"):
        href = "https:" + href
    if "/l/?" in href or href.startswith("/l/"):
        qs = href.split("?", 1)[-1] if "?" in href else ""
        params = urllib.parse.parse_qs(qs)
        if "uddg" in params:
            return urllib.parse.unquote(params["uddg"][0])
    return href


def parse_results(html: str, limit: int = MAX_RESULTS) -> list[dict]:
    results = []
    for m in RESULT_BLOCK_RE.finditer(html):
        block = m.group(1)
        t = TITLE_RE.search(block)
        if not t:
            continue
        href, title_html = t.group(1), t.group(2)
        snip = SNIPPET_RE.search(block)
        snippet_html = snip.group(1) if snip else ""
        results.append({
            "title": clean_text(title_html),
            "url": unwrap_redirect(href),
            "snippet": clean_text(snippet_html),
        })
        if len(results) >= limit:
            break
    return results


def format_markdown(query: str, results: list[dict]) -> str:
    if not results:
        return f"No web results for: {query!r}"
    lines = [f"Web results for **{query}**:", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"**{i}. {r['title']}**")
        if r["snippet"]:
            lines.append(r["snippet"])
        lines.append(f"→ {r['url']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("query", nargs="*")
    args = ap.parse_args()

    if args.smoke:
        return smoke()

    query = " ".join(args.query).strip()
    if not query:
        print('Usage: web-search.py "<query>"', file=sys.stderr)
        return 1

    html = drive_browser(query)
    results = parse_results(html)

    if args.json:
        print(json.dumps({
            "ok": True,
            "skill": "web-search",
            "query": query,
            "results": results,
            "user_message": format_markdown(query, results),
        }))
    else:
        print(format_markdown(query, results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
