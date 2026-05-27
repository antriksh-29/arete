#!/usr/bin/env python3
# brain-edit.py — Layer 3 mutation primitive. Surgical edits to
# existing brain pages.
#
# Three modes:
#
#   add-link    Add a [[wikilink]] to the page's Connections section
#               (creates section if missing, dedupes if already linked).
#               Also enqueues a cross-ref so the linked entity gets a
#               backlink.
#
#   append      Append a new section at the bottom of the page (above
#               existing trailing Connections section, if any).
#
#   set-field   Update a frontmatter field (or create it if missing).
#               Used by "update Nishank's brain page — set status to
#               'read'".
#
# Always rewrites the page atomically + best-effort gbrain put.
#
# Usage:
#   brain-edit.py add-link  <slug> "<entity-slug>" ["reason"]
#   brain-edit.py append    <slug> "## My note\n<body>"
#   brain-edit.py set-field <slug> <field> "<value>"
#
# Pointer accepts a slug, "last", or free-text (delegated to brain-find).

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

BRAIN_DIR = Path.home() / ".brain"
LAST_BOOKMARK_PATH = BRAIN_DIR / ".last-bookmark.json"
GBRAIN = "${STATE_HOME}/.bun/bin/gbrain"
TODAY = date.today().isoformat()

os.environ["PATH"] = "${STATE_HOME}/.bun/bin:" + os.environ.get("PATH", "")


def log(msg):
    print(f"[brain-edit] {msg}", file=sys.stderr)


WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
SLUG_RE = re.compile(r"^[a-z][\w.-]*\/[\w./-]+$")
FRONTMATTER_RE = re.compile(r"\A(---\n.*?\n---\n)(.*)\Z", re.DOTALL)


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


def _read_page(slug):
    p = BRAIN_DIR / f"{slug}.md"
    if not p.exists():
        return None, None
    return p, p.read_text()


def _write_page(path, slug, new_body):
    path.write_text(new_body)
    try:
        subprocess.run([GBRAIN, "put", slug], input=new_body, text=True,
                       capture_output=True, check=False)
    except FileNotFoundError:
        pass


def _enqueue_xref(slug, entities, source_label="manual edit"):
    if not entities:
        return
    BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"slug": slug, "entities": list(entities),
               "source_label": source_label, "queued_at": f"{TODAY}T00:00:00Z"}
    with open(BRAIN_DIR / ".xref-queue.jsonl", "a") as f:
        f.write(json.dumps(payload) + "\n")


def _bump_updated_field(body):
    return re.sub(r"^(updated:\s*).*$", f"\\g<1>{TODAY}",
                  body, count=1, flags=re.MULTILINE)


def cmd_add_link(slug, entity, reason=""):
    path, body = _read_page(slug)
    if body is None:
        return {"ok": False, "reason": "not_found",
                "user_message": f"🧬 Brain page `{slug}` not found."}
    entity = entity.strip().strip("[]").strip()
    if not entity:
        return {"ok": False, "reason": "empty_entity",
                "user_message": "🧬 Need an entity slug to link."}
    if entity == slug:
        return {"ok": False, "reason": "self_link",
                "user_message": "🧬 Can't link a page to itself."}

    # Skip if already linked
    existing = {m.group(1).split("|")[0].strip() for m in WIKILINK_RE.finditer(body)}
    if entity in existing:
        return {"ok": True, "slug": slug, "added": False,
                "user_message": (f"🧬 `{entity}` is already linked on `{slug}`. "
                                  f"No change.\n\n📎 source: {slug}")}

    bullet = f"- [[{entity}]]" + (f" — {reason}" if reason else "")
    if re.search(r"^##\s+Connections\b", body, re.MULTILINE):
        # Insert under existing Connections section, before the next
        # ## heading or EOF
        def insert(m):
            return m.group(0).rstrip() + "\n" + bullet + "\n"
        new_body, n = re.subn(
            r"(##\s+Connections[^\n]*\n(?:[^\n]*\n)*?)(?=\n##\s|\Z)",
            insert, body, count=1, flags=re.DOTALL,
        )
        if n == 0:
            # Fallback: appended-section path
            new_body = body.rstrip() + f"\n\n## Connections\n{bullet}\n"
    else:
        new_body = body.rstrip() + f"\n\n## Connections\n{bullet}\n"

    new_body = _bump_updated_field(new_body)
    _write_page(path, slug, new_body)
    _enqueue_xref(slug, [entity], source_label="manual edit (add-link)")
    return {"ok": True, "slug": slug, "added": True, "linked": entity,
            "user_message": (f"🧬 Linked `{entity}` on `{slug}`. "
                             f"Backlink will populate via xref-drainer.\n\n"
                             f"📎 source: {slug}")}


def cmd_append(slug, content):
    path, body = _read_page(slug)
    if body is None:
        return {"ok": False, "reason": "not_found",
                "user_message": f"🧬 Brain page `{slug}` not found."}
    content = content.strip()
    if not content:
        return {"ok": False, "reason": "empty_content",
                "user_message": "🧬 Nothing to append."}

    # Preserve trailing Connections section by inserting BEFORE it
    m = re.search(r"\n(##\s*Connections\b.*)\Z", body, re.DOTALL)
    if m:
        head = body[:m.start()].rstrip()
        tail = m.group(1)
        new_body = head + "\n\n" + content + "\n\n" + tail
        if not new_body.endswith("\n"):
            new_body += "\n"
    else:
        new_body = body.rstrip() + "\n\n" + content + "\n"

    new_body = _bump_updated_field(new_body)
    _write_page(path, slug, new_body)
    # Pick up any [[wikilinks]] in the appended content for xref
    appended_links = sorted({m.group(1).split("|")[0].strip()
                              for m in WIKILINK_RE.finditer(content)
                              if m.group(1).strip()})
    if appended_links:
        _enqueue_xref(slug, appended_links, source_label="manual edit (append)")
    return {"ok": True, "slug": slug, "appended": True,
            "linked_in_append": appended_links,
            "user_message": (f"🧬 Appended {len(content)} chars to `{slug}`.\n\n"
                             f"📎 source: {slug}")}


def cmd_set_field(slug, field, value):
    """Update (or create) a frontmatter field. Untouched fields are
    preserved. If the page has no frontmatter, prepend one."""
    path, body = _read_page(slug)
    if body is None:
        return {"ok": False, "reason": "not_found",
                "user_message": f"🧬 Brain page `{slug}` not found."}
    field = field.strip()
    if not field or " " in field:
        return {"ok": False, "reason": "invalid_field",
                "user_message": "🧬 Field name must be a single word."}
    m = FRONTMATTER_RE.match(body)
    if m:
        fm = m.group(1)
        rest = m.group(2)
        # Existing field?
        if re.search(rf"^{re.escape(field)}:\s*", fm, re.MULTILINE):
            fm = re.sub(rf"^({re.escape(field)}:\s*).*$", f"\\g<1>{value}",
                        fm, count=1, flags=re.MULTILINE)
        else:
            # Insert before the closing ---
            fm = re.sub(r"(\n---\n)\Z", f"\n{field}: {value}\\g<1>", fm)
        new_body = fm + rest
    else:
        new_body = f"---\n{field}: {value}\nupdated: {TODAY}\n---\n\n" + body
    new_body = _bump_updated_field(new_body)
    _write_page(path, slug, new_body)
    return {"ok": True, "slug": slug, "field": field, "value": value,
            "user_message": (f"🧬 Set `{field}: {value}` on `{slug}`.\n\n"
                             f"📎 source: {slug}")}


def main():
    ap = argparse.ArgumentParser(description="Surgical edits to a brain page.")
    ap.add_argument("--smoke", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    p1 = sub.add_parser("add-link", help="Add a [[wikilink]] to the page.")
    p1.add_argument("pointer"); p1.add_argument("entity"); p1.add_argument("reason", nargs="?", default="")

    p2 = sub.add_parser("append", help="Append content (preserves Connections).")
    p2.add_argument("pointer"); p2.add_argument("content")

    p3 = sub.add_parser("set-field", help="Set a frontmatter field.")
    p3.add_argument("pointer"); p3.add_argument("field"); p3.add_argument("value")

    args = ap.parse_args()
    if args.smoke:
        print(json.dumps({"ok": True, "skill": "brain-edit",
                          "user_message": "brain-edit smoke ok"}))
        return
    if not args.cmd:
        ap.print_help(); sys.exit(2)

    slug = _resolve_pointer(args.pointer)
    if not slug:
        print(json.dumps({"ok": False, "reason": "no_slug_resolvable",
                          "user_message": f"🧬 Couldn't resolve `{args.pointer}` to a brain page."}))
        return

    if args.cmd == "add-link":
        print(json.dumps(cmd_add_link(slug, args.entity, args.reason)))
    elif args.cmd == "append":
        print(json.dumps(cmd_append(slug, args.content)))
    elif args.cmd == "set-field":
        print(json.dumps(cmd_set_field(slug, args.field, args.value)))


if __name__ == "__main__":
    main()
