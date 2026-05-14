#!/usr/bin/env python3
# brain-graph.py — walk the [[wikilink]] graph from a brain page slug.
# Layer 3 traversal primitive.
#
# When the user asks "what else in my brain connects to Nishank's
# argument?" they want both directions:
#
#   * forward links — entities the source page references in its
#     Connections section ([[concepts/founder-mode]], [[people/...]])
#   * backlinks — OTHER brain pages that reference THIS slug (notes
#     the user wrote about it, other bookmarks that mentioned it)
#
# Pure file walking — no LLM, no gbrain network call. Fast.
#
# Usage:
#   brain-graph.py media/social/x-nishank-20260512
#   brain-graph.py "Nishank's article"        # resolves via brain-find
#   brain-graph.py last
#   brain-graph.py <slug> --depth 2           # follow links recursively
#
# Prints a single-line JSON envelope:
#   {"ok": true, "slug": "<source>",
#    "forward": [{"slug": "concepts/founder-mode", "context": "..."}],
#    "backlinks": [{"slug": "notes/2026-05-14-...", "context": "..."}],
#    "user_message": "🕸️ ..."}

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BRAIN_DIR = Path.home() / ".brain"
LAST_BOOKMARK_PATH = BRAIN_DIR / ".last-bookmark.json"
GBRAIN = "/home/botuser/.bun/bin/gbrain"

os.environ["PATH"] = "/home/botuser/.bun/bin:" + os.environ.get("PATH", "")


def log(msg):
    print(f"[brain-graph] {msg}", file=sys.stderr)


WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
SLUG_RE = re.compile(r"^[a-z][\w.-]*\/[\w./-]+$")


def _resolve_pointer(pointer):
    """Reuse brain-ask's resolver semantics: slug shape | "last" | URL
    via lists/to-read | free-text via brain-find."""
    if pointer.lower() == "last":
        if LAST_BOOKMARK_PATH.exists():
            try:
                return json.loads(LAST_BOOKMARK_PATH.read_text()).get("slug", "")
            except (json.JSONDecodeError, OSError):
                return ""
        return ""
    if SLUG_RE.match(pointer) and " " not in pointer:
        return pointer
    # Delegate free-text to brain-find for consistency with brain-ask
    bf = Path(__file__).resolve().parent / "brain-find.py"
    if not bf.exists():
        bf = Path("/home/botuser/bin/brain-find.py")
    if not bf.exists():
        return ""
    try:
        r = subprocess.run(["python3", str(bf), pointer],
                           capture_output=True, text=True, timeout=45, check=False)
        env = json.loads(r.stdout.strip().splitlines()[-1])
        return env.get("best", "") if env.get("ok") else ""
    except Exception:
        return ""


def _extract_outgoing_links(slug):
    """Forward edges: every [[target]] appearing in the source page's
    body. Skip self-references."""
    path = BRAIN_DIR / f"{slug}.md"
    if not path.exists():
        return []
    body = path.read_text()
    targets = []
    seen = set()
    for m in WIKILINK_RE.finditer(body):
        tgt = m.group(1).split("|")[0].strip()
        if tgt and tgt != slug and tgt not in seen:
            seen.add(tgt)
            # Capture surrounding context (40 chars) so the agent can
            # report "[concept] — mentioned in 'X argues that...'"
            start = max(0, m.start() - 40)
            end = min(len(body), m.end() + 40)
            context = body[start:end].replace("\n", " ").strip()
            targets.append({"slug": tgt, "context": context[:180]})
    return targets


def _scan_backlinks(target_slug, max_files=2000):
    """Backlinks: every brain page whose body contains [[target]].
    O(N) over all .md files in ~/.brain — fine at the current
    couple-hundred-pages scale. If the brain ever grows past 5k pages,
    swap for the gbrain index."""
    if not BRAIN_DIR.is_dir():
        return []
    target_link = f"[[{target_slug}]]"
    target_alias = f"[[{target_slug}|"  # links with display text
    backlinks = []
    seen = set()
    files = list(BRAIN_DIR.rglob("*.md"))
    if len(files) > max_files:
        log(f"warning: brain has {len(files)} pages, scanning may be slow")
    for path in files:
        rel = path.relative_to(BRAIN_DIR).with_suffix("").as_posix()
        if rel == target_slug or rel.startswith("_raw/"):
            continue
        try:
            body = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if target_link in body or target_alias in body:
            if rel in seen:
                continue
            seen.add(rel)
            # Extract the surrounding sentence for context
            idx = body.find(target_link)
            if idx < 0:
                idx = body.find(target_alias)
            start = max(0, idx - 60)
            end = min(len(body), idx + 120)
            context = body[start:end].replace("\n", " ").strip()
            backlinks.append({"slug": rel, "context": context[:200]})
    return backlinks


def _summarize_by_type(items):
    """Bucket by slug prefix so the Telegram render groups things:
    people/ separately from concepts/ separately from notes/ etc."""
    buckets = {}
    for it in items:
        prefix = it["slug"].split("/", 1)[0] if "/" in it["slug"] else "_"
        buckets.setdefault(prefix, []).append(it)
    return buckets


def graph(slug, depth=1):
    """Build the local graph view around `slug`. depth=1 is direct
    neighbors (forward + back). depth=2 includes neighbors-of-neighbors
    on the forward side only (backlinks-of-backlinks balloons fast)."""
    if not slug:
        return {"ok": False, "reason": "no_slug",
                "user_message": "🕸️ Need a brain page slug or a pointer like 'last' / 'Nishank's article'."}
    forward = _extract_outgoing_links(slug)
    backlinks = _scan_backlinks(slug)
    forward_depth2 = []
    if depth >= 2:
        for fwd in forward:
            for nb in _extract_outgoing_links(fwd["slug"]):
                if nb["slug"] != slug:
                    forward_depth2.append({**nb, "via": fwd["slug"]})

    if not forward and not backlinks:
        return {
            "ok": True, "slug": slug, "forward": [], "backlinks": [],
            "user_message": (
                f"🕸️ No graph edges from {slug} yet. Brain page exists but "
                f"hasn't been cross-referenced, or it's a leaf concept stub. "
                f"Run `migrate bookmarks` to refresh, or add a note that "
                f"links to it."
            ),
        }

    fwd_buckets = _summarize_by_type(forward)
    bk_buckets = _summarize_by_type(backlinks)
    lines = [f"🕸️ Graph around {slug}:", ""]
    if forward:
        lines.append(f"Outgoing links ({len(forward)}):")
        for prefix in sorted(fwd_buckets):
            for it in fwd_buckets[prefix]:
                lines.append(f"  → [[{it['slug']}]]")
        lines.append("")
    if backlinks:
        lines.append(f"Pages that reference this one ({len(backlinks)}):")
        for prefix in sorted(bk_buckets):
            for it in bk_buckets[prefix]:
                lines.append(f"  ← [[{it['slug']}]]")
        lines.append("")
    if forward_depth2:
        # Show only a few depth-2 hops, otherwise the reply turns into
        # a wall of text
        uniq = []
        seen = set()
        for it in forward_depth2:
            if it["slug"] not in seen:
                uniq.append(it); seen.add(it["slug"])
            if len(uniq) >= 6:
                break
        lines.append(f"Two hops away ({len(forward_depth2)} total, top {len(uniq)}):")
        for it in uniq:
            lines.append(f"  → [[{it['slug']}]] (via {it['via']})")
        lines.append("")
    lines.append(f"📎 source: {slug}")
    return {
        "ok": True, "slug": slug,
        "forward": forward, "backlinks": backlinks,
        "forward_depth2": forward_depth2,
        "user_message": "\n".join(lines),
    }


def main():
    ap = argparse.ArgumentParser(description="Walk the brain wikilink graph from a slug.")
    ap.add_argument("pointer", nargs="?",
                    help='Slug, "last", URL, or free-text reference.')
    ap.add_argument("--depth", type=int, default=1, choices=[1, 2])
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    if args.smoke:
        print(json.dumps({"ok": True, "skill": "brain-graph",
                          "user_message": "brain-graph smoke ok"}))
        return
    if not args.pointer:
        sys.exit('Usage: brain-graph.py <slug|"last"|"keywords"> [--depth 1|2]')
    slug = _resolve_pointer(args.pointer)
    if not slug:
        print(json.dumps({
            "ok": False, "reason": "no_slug_resolvable",
            "user_message": (
                f"🕸️ Couldn't resolve `{args.pointer}` to a brain page. "
                f"Pass an explicit slug or use a phrase like 'Nishank's article'."
            ),
        }))
        return
    print(json.dumps(graph(slug, depth=args.depth)))


if __name__ == "__main__":
    main()
