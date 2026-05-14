#!/usr/bin/env python3
# note.py — Layer 3 note-taking. Writes a brain page under notes/
# from the user's own words. Compounds with brain-ask: once a note
# is captured, subsequent questions can search across notes + ingested
# bookmarks together.
#
# Why this exists: the brain so far only stored content the user
# bookmarked from elsewhere. Antriksh's own thoughts ("I disagree
# with Nishank because…", "remember that founder-mode applies
# differently at seed vs growth") had nowhere to live except the
# Telegram chat — lossy, search-blind, can't be linked. note.py
# gives those a first-class home that brain-find can route to and
# brain-ask can quote from.
#
# Modes:
#   1. New note from free-text:
#        note.py "I think Nishank's survivorship-bias point applies
#                 more to seed-stage founders than late-stage..."
#      → notes/2026-05-14-survivorship-stage-asymmetry.md
#   2. Note linked to a source (the agent passes the slug after
#      resolving via brain-find):
#        note.py "My reaction: the asymmetry is..." \
#                --source media/social/x-nishank-20260512
#      → linked back to the brain page; bidirectional via gbrain link
#   3. Append to an existing note:
#        note.py "additional thought..." \
#                --append-to notes/2026-05-14-survivorship-stage-asymmetry
#
# Prints a single-line JSON envelope on stdout with ok + slug +
# user_message that the agent relays to Telegram verbatim.

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

BRAIN_DIR = Path.home() / ".brain"
NOTES_DIR = BRAIN_DIR / "notes"
GBRAIN = "/home/botuser/.bun/bin/gbrain"
TODAY = date.today().isoformat()

os.environ["PATH"] = "/home/botuser/.bun/bin:" + os.environ.get("PATH", "")


def log(msg):
    print(f"[note] {msg}", file=sys.stderr)


_SLUG_BAD = re.compile(r"[^\w\s-]")
_SLUG_WS = re.compile(r"[-\s]+")
_FIRST_SENTENCE = re.compile(r"^(.{10,180}?)(?:[.!?](?:\s|$)|\n|$)")


def derive_title(text):
    """Pick a short, human-readable title from the note body. First
    full sentence (up to ~180 chars) if one exists; otherwise the
    first line truncated. Never returns empty — falls back to a date
    stamp when the text is unusable."""
    text = (text or "").strip()
    if not text:
        return f"note {TODAY}"
    m = _FIRST_SENTENCE.search(text)
    if m:
        candidate = m.group(1).strip()
    else:
        candidate = text.splitlines()[0].strip()
    candidate = candidate.rstrip(":,;").strip()
    if len(candidate) > 80:
        candidate = candidate[:77].rstrip() + "…"
    return candidate or f"note {TODAY}"


def slugify(title):
    s = _SLUG_BAD.sub("", title.lower()).strip()
    s = _SLUG_WS.sub("-", s)[:60]
    return s.strip("-") or "note"


def build_slug(title, override=None):
    """Build a notes/<YYYY-MM-DD>-<slug> path. Collisions: append -2,
    -3, … so a second note on the same day with a similar opening
    line doesn't clobber the first."""
    base = override or slugify(title)
    candidate = f"notes/{TODAY}-{base}"
    i = 2
    while (BRAIN_DIR / f"{candidate}.md").exists():
        candidate = f"notes/{TODAY}-{base}-{i}"
        i += 1
    return candidate


WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _list_existing_entities():
    """Cheap entity discovery — walk people/ companies/ concepts/
    directories on disk, return the slug list. Used to auto-suggest
    [[wikilinks]] for entities the note body mentions by name."""
    seen = []
    for d in ("people", "companies", "concepts"):
        root = BRAIN_DIR / d
        if not root.is_dir():
            continue
        for p in root.glob("*.md"):
            seen.append(f"{d}/{p.stem}")
    return seen


def auto_link_entities(body):
    """Scan the note body for mentions of existing entity slugs
    (case-insensitive on the stem), suggest them as [[wikilinks]] in
    a `## Connections` section. Conservative — only emits links for
    entities the user clearly named (substring match on the entity's
    display name derived from slug)."""
    if not body:
        return [], ""
    entities = _list_existing_entities()
    body_l = body.lower()
    matched = []
    for slug in entities:
        stem = slug.split("/", 1)[1]
        # e.g. concepts/founder-mode → "founder mode" and "founder-mode"
        for needle in (stem.replace("-", " "), stem.replace("-", "")):
            if needle and needle.lower() in body_l:
                matched.append(slug)
                break
    matched = sorted(set(matched))
    if not matched:
        return [], ""
    block = ["## Connections"]
    for slug in matched:
        stem = slug.split("/", 1)[1]
        block.append(f"- [[{slug}]] — mentioned in the note text")
    return matched, "\n".join(block)


def explicit_wikilinks(body):
    """Pull [[slug]] entries the user typed explicitly. These get
    gbrain-linked even if they don't exist yet as brain pages —
    parity with how ingest-x-thread handles cross-references."""
    return sorted({m.group(1).split("|")[0].strip()
                   for m in WIKILINK_RE.finditer(body or "")
                   if m.group(1).strip()})


def write_new_note(body, source_slug=None, override_slug=None):
    """Create a fresh notes/<date>-<slug>.md file. Returns the slug."""
    body = (body or "").strip()
    if not body:
        raise ValueError("note body is empty")
    title = derive_title(body)
    slug = build_slug(title, override=override_slug)

    matched_entities, connections_block = auto_link_entities(body)
    typed = explicit_wikilinks(body)
    all_links = sorted(set(matched_entities + typed))
    if source_slug and source_slug not in all_links:
        all_links.append(source_slug)

    parts = [
        "---",
        "type: note",
        f"created: {TODAY}",
        f"updated: {TODAY}",
        "author: antriksh",
    ]
    if source_slug:
        parts.append(f"source: {source_slug}")
    parts += ["---", "", f"# {title}", ""]
    parts.append(body)
    if source_slug:
        parts += ["", "## Source", f"- [[{source_slug}]]"]
    if connections_block:
        parts += ["", connections_block]
    elif typed:
        parts += ["", "## Connections"]
        for ent in typed:
            parts.append(f"- [[{ent}]] — referenced in note text")

    page = "\n".join(parts) + "\n"
    page_path = BRAIN_DIR / f"{slug}.md"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(page)
    log(f"wrote {page_path}")
    # Push to gbrain index — best-effort; non-fatal if gbrain isn't
    # installed (local dev mode).
    try:
        subprocess.run([GBRAIN, "put", slug], input=page, text=True,
                       capture_output=True, check=False)
    except FileNotFoundError:
        pass

    # Enqueue cross-references so xref-drainer creates stubs + links
    # asynchronously instead of blocking the turn. Mirrors the pattern
    # used by ingest-x-thread / ingest-html.
    if all_links:
        try:
            qpath = BRAIN_DIR / ".xref-queue.jsonl"
            qpath.parent.mkdir(parents=True, exist_ok=True)
            with open(qpath, "a") as f:
                f.write(json.dumps({
                    "slug": slug,
                    "entities": all_links,
                    "source_label": "note",
                    "queued_at": f"{TODAY}T00:00:00Z",
                }) + "\n")
        except Exception as e:
            log(f"xref enqueue failed (non-fatal): {e}")

    return slug, title, all_links


def append_to_note(append_to_slug, body):
    """Append text to an existing note's body. Preserves frontmatter
    and Connections; appends under a timestamped sub-heading."""
    body = (body or "").strip()
    if not body:
        raise ValueError("append body is empty")
    page_path = BRAIN_DIR / f"{append_to_slug}.md"
    if not page_path.exists():
        raise FileNotFoundError(f"note not found: {append_to_slug}")
    existing = page_path.read_text()

    # Split off Connections section (if any) so the append goes ABOVE it
    parts_pre = existing
    connections = ""
    m = re.search(r"\n(##\s*Connections.*)\Z", existing, re.DOTALL)
    if m:
        connections = m.group(1)
        parts_pre = existing[:m.start()].rstrip() + "\n"

    addition = f"\n## Addition — {TODAY}\n\n{body}\n"
    new = parts_pre.rstrip() + "\n" + addition
    if connections:
        new = new.rstrip() + "\n\n" + connections.lstrip() + "\n"
    # Refresh `updated:` frontmatter
    new = re.sub(r"^(updated:\s*).*$", f"\\g<1>{TODAY}",
                 new, count=1, flags=re.MULTILINE)
    page_path.write_text(new)
    log(f"appended to {page_path} ({len(body)} chars)")
    try:
        subprocess.run([GBRAIN, "put", append_to_slug], input=new, text=True,
                       capture_output=True, check=False)
    except FileNotFoundError:
        pass
    return append_to_slug


def main():
    ap = argparse.ArgumentParser(description="Write a note to the brain.")
    ap.add_argument("body", nargs="?", help="The note text.")
    ap.add_argument("--source", help="Brain page slug this note relates to "
                    "(e.g. media/social/x-nishank-20260512). Adds a Source "
                    "section + xref to the linked page.")
    ap.add_argument("--append-to", help="Existing note slug to append to "
                    "(instead of creating a new one).")
    ap.add_argument("--slug", help="Override the auto-derived slug stem "
                    "(useful for tests / scripted seeding).")
    ap.add_argument("--smoke", action="store_true", help="Smoke probe.")
    args = ap.parse_args()

    if args.smoke:
        print(json.dumps({"ok": True, "skill": "note",
                          "user_message": "note smoke ok"}))
        return

    if not args.body or not args.body.strip():
        print(json.dumps({
            "ok": False, "reason": "empty_body",
            "user_message": "🗒️  Nothing to save — paste the note text after "
                           "'take a note:' and I'll capture it.",
        }))
        return

    try:
        if args.append_to:
            slug = append_to_note(args.append_to, args.body)
            print(json.dumps({
                "ok": True, "slug": slug, "appended": True,
                "user_message": (
                    f"🗒️  Appended to note: {slug}\n\n"
                    f"📎 source: {slug}"
                ),
            }))
            return
        slug, title, links = write_new_note(
            args.body, source_slug=args.source, override_slug=args.slug,
        )
        msg_lines = [f"🗒️  Note saved: {title}", f"   {slug}"]
        if args.source:
            msg_lines.append(f"   linked back to {args.source}")
        if links:
            msg_lines.append(f"   ({len(links)} cross-link{'s' if len(links) != 1 else ''} queued)")
        msg_lines.append("")
        msg_lines.append(f"📎 source: {slug}")
        print(json.dumps({
            "ok": True, "slug": slug, "title": title,
            "linked_entities": links,
            "user_message": "\n".join(msg_lines),
        }))
    except FileNotFoundError as e:
        print(json.dumps({
            "ok": False, "reason": "append_target_missing",
            "user_message": f"🗒️  Can't find note to append to: {e}",
        }))
    except ValueError as e:
        print(json.dumps({
            "ok": False, "reason": "empty_body",
            "user_message": f"🗒️  {e}",
        }))


if __name__ == "__main__":
    main()
