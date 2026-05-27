#!/usr/bin/env python3
# brain-compare.py — Layer 3 synthesis primitive. Load 2+ brain pages
# (with raw sidecars when present), send them to the LLM with a
# "compare and contrast, quote verbatim" prompt, return the synthesis.
#
# Example use: "compare Nishank's view on ambition with Paul Graham's".
# The agent runs brain-find for both references, hands the slugs to
# brain-compare, relays the result.
#
# Usage:
#   brain-compare.py "media/social/x-nishank-..." "sources/paulgraham-foundermode"
#   brain-compare.py "Nishank's article" "paulg founder mode"
#   brain-compare.py --question "what do they each say about persistence" \
#                    slug-a slug-b
#
# Prints a single-line JSON envelope:
#   {"ok": true, "slugs": [...], "answer": "...",
#    "user_message": "...", "sources_used": [{"slug":..., "raw_chars":..., "page_chars":...}]}

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BRAIN_DIR = Path.home() / ".brain"
RAW_DIR = BRAIN_DIR / "_raw"
LAST_BOOKMARK_PATH = BRAIN_DIR / ".last-bookmark.json"
GBRAIN = "${STATE_HOME}/.bun/bin/gbrain"

os.environ["PATH"] = "${STATE_HOME}/.bun/bin:" + os.environ.get("PATH", "")


def log(msg):
    print(f"[brain-compare] {msg}", file=sys.stderr)


SLUG_RE = re.compile(r"^[a-z][\w.-]*\/[\w./-]+$")

SYSTEM_PROMPT = """You are comparing two or more pieces of writing from the user's personal knowledge brain. Each source is provided as a brain page (with its own analysis from a prior LLM pass) plus, when available, the verbatim source text.

Rules:
1. Compare and contrast — what does each source claim, where do they agree, where do they diverge? Don't just summarize each separately and stop.
2. When you quote, quote VERBATIM from the Raw source section. Wrap in straight quotes. Always identify which source the quote came from: ([[<slug>]]).
3. If the user asked a specific question, structure the answer around it. Otherwise default to: shared ground → key divergence → which is stronger and why, based ONLY on the material.
4. Don't invent overlap that isn't there. If the two pieces are on different topics and don't really speak to each other, say so plainly.
5. The user's name is the user. Address him directly when offering judgment ("you might want to…").

Output format: continuous prose with `[[slug]]` citations inline. No preamble, no "Here's a comparison:", no closing summary. Just the substantive synthesis."""


def _resolve_pointer(pointer):
    if pointer.lower() == "last":
        if LAST_BOOKMARK_PATH.exists():
            try:
                return json.loads(LAST_BOOKMARK_PATH.read_text()).get("slug", "")
            except (json.JSONDecodeError, OSError):
                return ""
        return ""
    if SLUG_RE.match(pointer) and " " not in pointer:
        return pointer
    bf = Path(__file__).resolve().parent / "brain-find.py"
    if not bf.exists():
        bf = Path("${STATE_HOME}/bin/brain-find.py")
    if not bf.exists():
        return ""
    try:
        r = subprocess.run(["python3", str(bf), pointer],
                           capture_output=True, text=True, timeout=45, check=False)
        env = json.loads(r.stdout.strip().splitlines()[-1])
        return env.get("best", "") if env.get("ok") else ""
    except Exception:
        return ""


def _load_source(slug):
    """Return (page_text, raw_text, summary_dict). summary tracks
    char counts so the agent can report 'used the raw sidecar for X
    and the brain page for Y'."""
    page_path = BRAIN_DIR / f"{slug}.md"
    page_text = page_path.read_text() if page_path.exists() else ""
    if not page_text:
        r = subprocess.run([GBRAIN, "get", slug], capture_output=True, text=True)
        if r.returncode == 0:
            page_text = r.stdout
    raw_text = ""
    basename = slug.split("/")[-1]
    for candidate in (RAW_DIR / f"{basename}.raw.txt", RAW_DIR / f"{basename}.raw.md"):
        if candidate.exists():
            raw_text = candidate.read_text()
            break
    return page_text, raw_text


def compare(slugs, question=""):
    if len(slugs) < 2:
        return {"ok": False, "reason": "needs_two_sources",
                "user_message": "🔁 Need at least two brain page slugs to compare."}
    sources_used = []
    payload_parts = []
    for slug in slugs:
        page, raw = _load_source(slug)
        if not page and not raw:
            return {"ok": False, "reason": "missing_source",
                    "user_message": f"🔁 Brain page `{slug}` not found — bookmark or ingest it first."}
        sources_used.append({"slug": slug, "page_chars": len(page), "raw_chars": len(raw)})
        block = [f"=== SOURCE: {slug} ==="]
        if page:
            block.append(f"-- Brain page body ({len(page)} chars):\n{page[:20000]}")
        if raw:
            block.append(f"-- Raw source ({len(raw)} chars, verbatim):\n{raw[:20000]}")
        payload_parts.append("\n".join(block))

    user_block = "\n\n".join(payload_parts)
    if question:
        user_block += f"\n\n=== USER QUESTION ===\n{question}"
    else:
        user_block += "\n\n=== TASK ===\nCompare and contrast these sources."

    sys.path.insert(0, "${STATE_HOME}/bin")
    import llm  # type: ignore
    text, provider = llm.call(
        system_prompt=SYSTEM_PROMPT,
        user_msg=user_block,
        tier="mini",
        max_tokens=3000,
    )
    answer = text.strip()
    citation_block = "\n\n📎 sources: " + ", ".join(slugs)
    return {
        "ok": True, "slugs": list(slugs), "answer": answer,
        "user_message": answer + citation_block,
        "sources_used": sources_used, "provider": provider,
    }


def main():
    ap = argparse.ArgumentParser(description="Compare 2+ brain pages with quoting.")
    ap.add_argument("pointers", nargs="*",
                    help="Two or more slugs / free-text references.")
    ap.add_argument("--question",
                    help="Specific lens for the comparison (optional).")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    if args.smoke:
        print(json.dumps({"ok": True, "skill": "brain-compare",
                          "user_message": "brain-compare smoke ok"}))
        return
    if len(args.pointers) < 2:
        sys.exit('Usage: brain-compare.py <slug-or-ref> <slug-or-ref> [...] [--question "..."]')
    slugs = []
    for p in args.pointers:
        s = _resolve_pointer(p)
        if not s:
            print(json.dumps({
                "ok": False, "reason": "no_slug_resolvable", "input": p,
                "user_message": f"🔁 Couldn't resolve `{p}` to a brain page.",
            }))
            return
        slugs.append(s)
    print(json.dumps(compare(slugs, question=args.question or "")))


if __name__ == "__main__":
    main()
