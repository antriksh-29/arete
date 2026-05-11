#!/usr/bin/env python3
# Reading list / bookmark manager. Single source of truth: lists/to-read
# brain page. Every saved URL is ingested deep — we always write a
# dedicated brain page so the list view can show a real one-line
# description, and the brain stays searchable.
#
# Row schema (v2):
#   Queue:        | # | Title | Posted by | One-liner | Kind | URL | Added | Brain page |
#   Recently read | # | Title | Posted by | One-liner | Kind | URL | Added | Read | Brain page |
#
# Title / Posted by / One-liner are stored as plain text. No markdown
# links anywhere except `[[brain-page-slug]]` in the last column.
#
# Subcommands:
#   add <url>           Ingest + add row to queue (deep, always).
#   show                4-line per-entry render for Telegram.
#   read <n|url|slug>   Move row from Queue → Recently read.
#   drop <n|url|slug>   Remove row entirely.
#   migrate             Walk all rows; for any row missing brain page /
#                       posted_by / one_liner, run the right ingestor
#                       and update the row. Idempotent.
#
# Always rebuilds the brain page after any mutation and commits to git.

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
LIST_PATH = BRAIN_DIR / "lists" / "to-read.md"
LIST_SLUG = "lists/to-read"
GBRAIN = "/home/botuser/.bun/bin/gbrain"
TODAY = date.today().isoformat()

# gbrain's shebang is `#!/usr/bin/env bun`. When this script is invoked
# by openclaw or systemd, bun isn't on PATH and every gbrain call
# silently returns exit 127. Prepend bun's bin dir.
os.environ["PATH"] = "/home/botuser/.bun/bin:" + os.environ.get("PATH", "")

INGEST_PDF = "/home/botuser/bin/ingest-pdf.py"
INGEST_X = "/home/botuser/bin/ingest-x-thread.py"
INGEST_YOUTUBE = "/home/botuser/bin/ingest-youtube.py"
INGEST_HTML = "/home/botuser/bin/ingest-html.py"


def log(msg):
    print(f"[reading-list] {msg}", file=sys.stderr)


def vault_get(key):
    r = subprocess.run(["bot-cred", "get", key], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


TRACKING_PARAMS = {
    "s", "t", "si",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_src", "ref_url", "feature", "fbclid", "gclid",
}


def normalize_url(url):
    p = urllib.parse.urlsplit(url.strip())
    qs = urllib.parse.parse_qsl(p.query, keep_blank_values=False)
    qs = [(k, v) for k, v in qs if k.lower() not in TRACKING_PARAMS]
    new_q = urllib.parse.urlencode(qs)
    path = p.path.rstrip("/") if p.path != "/" else p.path
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), path, new_q, ""))


def url_kind(url):
    u = url.lower()
    if u.endswith(".pdf"):
        return "pdf"
    if "twitter.com/" in u or "x.com/" in u:
        return "x_thread" if "/status/" in u else "x_other"
    if "youtube.com/" in u or "youtu.be/" in u:
        return "youtube"
    return "html"


def url_host(url):
    return urllib.parse.urlsplit(url).netloc.replace("www.", "")


# ----- Cell sanitization -----
# Title / posted_by / one_liner must be plain text, single line, no
# markdown link syntax, no t.co shortlinks, no pipe characters (which
# would break the table), and not absurdly long.

PIPE_RE = re.compile(r"\|")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
TCO_RE = re.compile(r"https?://t\.co/\S+")
ANY_URL_RE = re.compile(r"https?://\S+")
WS_RE = re.compile(r"\s+")


def clean_cell(s, max_len=240, strip_urls=False):
    if s is None:
        return ""
    s = str(s)
    # Unwrap [text](url) -> text
    s = MD_LINK_RE.sub(r"\1", s)
    # Drop t.co shortlinks unconditionally — they're never real titles
    s = TCO_RE.sub("", s)
    if strip_urls:
        s = ANY_URL_RE.sub("", s)
    # Collapse whitespace, strip pipes
    s = PIPE_RE.sub("/", s)
    s = WS_RE.sub(" ", s).strip()
    if len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return s


# ----- Ingestor dispatch -----

def fetch_title_fallback(url, kind):
    """Cheap title-only fetch for when the deep ingestor fails. Used as
    a last-resort so a row is never blank."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Alfred reading-list)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read(500_000).decode("utf-8", errors="replace")
        m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE | re.DOTALL)
        if m:
            return clean_cell(m.group(1))
    except Exception as e:
        log(f"fallback title fetch failed for {url}: {e}")
    if kind == "pdf":
        slug = url.rsplit("/", 1)[-1]
        return clean_cell(slug.replace(".pdf", "").replace("-", " ").replace("_", " ").title())
    return clean_cell(url)


def run_ingestor(url, kind):
    """Returns {title, posted_by, one_liner, brain_page} — empty strings
    when the ingestor failed."""
    fields = {"title": "", "posted_by": "", "one_liner": "", "brain_page": ""}

    if kind == "x_thread":
        cmd = ["python3", INGEST_X, url]
    elif kind == "youtube":
        cmd = ["python3", INGEST_YOUTUBE, url]
    elif kind == "pdf":
        cmd = ["python3", INGEST_PDF, url]
    elif kind == "html":
        cmd = ["python3", INGEST_HTML, url]
    else:
        # x_other (profile / hashtag) — no deep ingest available
        log(f"no ingestor for kind={kind}; will use shallow fallback")
        return fields

    log(f"running ingestor for kind={kind}: {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        log(f"ingestor exit {r.returncode}: {r.stderr[-500:]}")
        return fields

    for line in reversed(r.stdout.strip().splitlines()):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not d.get("ok"):
            continue
        fields["brain_page"] = d.get("slug", "")
        fields["one_liner"] = clean_cell(d.get("insight_one_liner", ""), strip_urls=True)
        if kind == "x_thread":
            handle = d.get("handle", "")
            fields["posted_by"] = clean_cell(f"@{handle}" if handle else "")
            # ingest-x's "title" isn't returned; derive from one-liner or
            # fall back to handle + first words of one-liner
            fields["title"] = clean_cell(d.get("insight_one_liner", "")) or fields["posted_by"]
        elif kind == "youtube":
            fields["posted_by"] = clean_cell(d.get("channel", ""))
            fields["title"] = clean_cell(d.get("title", ""))
        elif kind == "pdf":
            fields["posted_by"] = clean_cell(d.get("author", "") or url_host(url))
            fields["title"] = clean_cell(d.get("title", "") or d.get("slug", "").rsplit("/", 1)[-1])
        elif kind == "html":
            fields["posted_by"] = clean_cell(d.get("author", "") or url_host(url))
            fields["title"] = clean_cell(d.get("title", ""))
        break
    return fields


def hydrate_row(url, kind):
    """Run the right ingestor; fall back to a cheap title fetch + URL host
    if it fails so we never store a fully blank row."""
    f = run_ingestor(url, kind)
    if not f["title"]:
        f["title"] = fetch_title_fallback(url, kind)
    if not f["posted_by"]:
        f["posted_by"] = url_host(url)
    return f


# ----- list parsing -----

# v2 schema regexes (8 / 9 fields).
V2_QUEUE_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*$"
)
V2_READ_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*$"
)

# v1 schema regexes (5 / 6 fields, title is [text](url)).
V1_QUEUE_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*\[([^\]]+)\]\(([^)]+)\)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*$"
)
V1_READ_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*\[([^\]]+)\]\(([^)]+)\)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*$"
)
LEGACY_ROW_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*\[([^\]]+)\]\(([^)]+)\)\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*$"
)


def strip_wikilink(s):
    s = s.strip()
    if s.startswith("[[") and s.endswith("]]"):
        return s[2:-2]
    return s


def looks_like_header(cells):
    """A header row contains literal column names — not data."""
    if not cells:
        return False
    joined = " ".join(c.strip().lower() for c in cells)
    return "title" in joined and ("url" in joined or "posted by" in joined or "kind" in joined)


def load_state():
    state = {"queue": [], "read": []}
    r = subprocess.run([GBRAIN, "get", LIST_SLUG], capture_output=True, text=True)
    if r.returncode != 0:
        return state
    text = r.stdout
    has_queue_heading = "## Queue" in text or "## To Read" in text
    section = "queue" if not has_queue_heading else None
    for line in text.splitlines():
        ls = line.strip().lower()
        if ls.startswith("## queue") or ls == "## to read":
            section = "queue"
            continue
        if ls.startswith("## recently read") or ls == "## read":
            section = "read"
            continue
        if ls.startswith("## "):
            section = None
            continue
        if ls.startswith("|---") or ls.startswith("|--") or ls.startswith("| -"):
            continue
        if section is None:
            continue
        # Header detection — skip rows whose first cell is a column name
        if section in ("queue", "read") and "| title " in line.lower():
            continue

        # v2 first (more fields)
        if section == "read":
            m = V2_READ_RE.match(line)
            if m:
                state["read"].append({
                    "title": m.group(1), "posted_by": m.group(2),
                    "one_liner": m.group(3), "kind": m.group(4),
                    "url": m.group(5), "added": m.group(6),
                    "read": m.group(7), "brain_page": strip_wikilink(m.group(8)),
                })
                continue
            m = V1_READ_RE.match(line)
            if m and not m.group(1).startswith("-"):
                state["read"].append({
                    "title": clean_cell(m.group(1)), "posted_by": "", "one_liner": "",
                    "kind": m.group(3), "url": m.group(2),
                    "added": m.group(4), "read": m.group(5),
                    "brain_page": strip_wikilink(m.group(6)),
                })
                continue
        # queue section
        m = V2_QUEUE_RE.match(line)
        if m:
            state[section].append({
                "title": m.group(1), "posted_by": m.group(2),
                "one_liner": m.group(3), "kind": m.group(4),
                "url": m.group(5), "added": m.group(6),
                "brain_page": strip_wikilink(m.group(7)),
            })
            continue
        m = V1_QUEUE_RE.match(line)
        if m and not m.group(1).startswith("-"):
            state[section].append({
                "title": clean_cell(m.group(1)), "posted_by": "", "one_liner": "",
                "kind": m.group(3), "url": m.group(2),
                "added": m.group(4),
                "brain_page": strip_wikilink(m.group(5)),
            })
            continue
        m2 = LEGACY_ROW_RE.match(line)
        if m2:
            state[section].append({
                "title": "", "posted_by": "", "one_liner": "",
                "kind": "", "url": m2.group(2),
                "added": m2.group(3), "brain_page": "",
            })
    return state


# ----- render -----

def _row_cells(r, include_read=False):
    cells = [
        clean_cell(r.get("title", "")),
        clean_cell(r.get("posted_by", "")),
        clean_cell(r.get("one_liner", ""), strip_urls=True),
        clean_cell(r.get("kind", "")),
        r.get("url", "").strip(),
        clean_cell(r.get("added", "")) or TODAY,
    ]
    if include_read:
        cells.append(clean_cell(r.get("read", "")) or TODAY)
    bp = r.get("brain_page", "")
    cells.append(f"[[{bp}]]" if bp else "")
    return cells


def render_page(state):
    lines = [
        "---",
        "type: list",
        "title: To Read",
        f"updated: {TODAY}",
        "---",
        "",
        "# To Read",
        "",
        "Bookmarks queued for reading. Managed by `~/bin/reading-list.py`.",
        "Every row has a dedicated brain page; see the last column.",
        "",
        "## Queue",
        "",
    ]
    if state["queue"]:
        lines += [
            "| # | Title | Posted by | One-liner | Kind | URL | Added | Brain page |",
            "|---|-------|-----------|-----------|------|-----|-------|------------|",
        ]
        for i, r in enumerate(state["queue"], 1):
            cells = _row_cells(r, include_read=False)
            lines.append("| " + " | ".join([str(i)] + cells) + " |")
    else:
        lines.append("_Queue is empty._")
    lines += ["", "## Recently read", ""]
    if state["read"]:
        lines += [
            "| # | Title | Posted by | One-liner | Kind | URL | Added | Read | Brain page |",
            "|---|-------|-----------|-----------|------|-----|-------|------|------------|",
        ]
        for i, r in enumerate(state["read"], 1):
            cells = _row_cells(r, include_read=True)
            lines.append("| " + " | ".join([str(i)] + cells) + " |")
    else:
        lines.append("_Nothing read yet._")
    return "\n".join(lines) + "\n"


def render_for_telegram(state):
    """4-line per entry render. The public render contract — every
    invariant in skills/reading-list/SKILL.md is enforced here."""
    lines = ["📚 Bookmarks"]
    if state["queue"]:
        lines.append("")
        lines.append("To read:")
        for i, r in enumerate(state["queue"], 1):
            title = clean_cell(r.get("title", "")) or "(untitled)"
            posted_by = clean_cell(r.get("posted_by", "")) or url_host(r["url"])
            one_liner = clean_cell(r.get("one_liner", ""), strip_urls=True)
            url = r["url"].strip()
            lines.append(f"{i}. {title}")
            lines.append(f"   Posted by: {posted_by}")
            if one_liner:
                lines.append(f"   {one_liner}")
            lines.append(f"   {url}")
    else:
        lines.append("")
        lines.append("Queue is empty.")
    if state["read"]:
        recent = state["read"][-5:]
        lines += ["", "Recently read:"]
        for r in recent:
            title = clean_cell(r.get("title", "")) or "(untitled)"
            posted_by = clean_cell(r.get("posted_by", "")) or url_host(r["url"])
            one_liner = clean_cell(r.get("one_liner", ""), strip_urls=True)
            url = r["url"].strip()
            lines.append(f"✓ {title}")
            lines.append(f"   Posted by: {posted_by}")
            if one_liner:
                lines.append(f"   {one_liner}")
            lines.append(f"   {url}")
    return "\n".join(lines)


def save_state(state):
    body = render_page(state)
    LIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LIST_PATH.write_text(body)
    subprocess.run([GBRAIN, "put", LIST_SLUG], input=body, text=True, capture_output=True, check=False)
    git_commit()


def git_commit():
    cwd = str(BRAIN_DIR)
    if not (BRAIN_DIR / ".git").exists():
        return
    subprocess.run(["git", "add", "lists/to-read.md"], cwd=cwd, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=alfred@bot", "-c", "user.name=Alfred",
         "commit", "-m", f"reading-list update {TODAY}"],
        cwd=cwd, capture_output=True
    )


# ----- subcommands -----

def cmd_add(url):
    url = normalize_url(url)
    kind = url_kind(url)
    state = load_state()

    for r in state["queue"] + state["read"]:
        if normalize_url(r["url"]) == url:
            print(json.dumps({
                "ok": False, "reason": "duplicate",
                "user_message": f"📚 Already on your bookmarks: {r.get('title','(untitled)')[:80]}",
            }))
            return

    f = hydrate_row(url, kind)
    row = {
        "title": f["title"], "posted_by": f["posted_by"],
        "one_liner": f["one_liner"], "kind": kind, "url": url,
        "added": TODAY, "brain_page": f["brain_page"],
    }
    state["queue"].append(row)
    save_state(state)

    msg_lines = [f"✅ Bookmarked: {row['title'][:80]}",
                 f"   Posted by: {row['posted_by']}"]
    if row["one_liner"]:
        msg_lines.append(f"   {row['one_liner']}")
    msg_lines.append(f"   {url}")
    if row["brain_page"]:
        msg_lines.append(f"\nBrain page: {row['brain_page']}")
    else:
        msg_lines.append("\n(Deep ingestion failed — saved with title only. Run `migrate reading list` later to retry.)")
    print(json.dumps({
        "ok": True, **row,
        "user_message": "\n".join(msg_lines),
    }))


def cmd_show():
    state = load_state()
    msg = render_for_telegram(state)
    print(json.dumps({
        "ok": True,
        "queue_count": len(state["queue"]),
        "read_count": len(state["read"]),
        "user_message": msg,
    }))


def resolve(target, queue):
    if target.isdigit():
        idx = int(target) - 1
        if 0 <= idx < len(queue):
            return idx
        return -1
    for i, r in enumerate(queue):
        if (r["url"] == target
                or normalize_url(r["url"]) == normalize_url(target)
                or (r.get("brain_page") and r["brain_page"] == target)):
            return i
    return -1


def cmd_read(target):
    state = load_state()
    idx = resolve(target, state["queue"])
    if idx < 0:
        print(json.dumps({"ok": False, "reason": "not_found", "user_message": f"📚 Couldn't find #{target} on your bookmarks. Try `/show` to see the numbered list."}))
        return
    item = state["queue"].pop(idx)
    item["read"] = TODAY
    state["read"].append(item)
    save_state(state)
    print(json.dumps({"ok": True, "user_message": f"✅ Marked as read: {item['title'] or item['url']}"}))


def cmd_drop(target):
    state = load_state()
    idx = resolve(target, state["queue"])
    if idx < 0:
        print(json.dumps({"ok": False, "reason": "not_found", "user_message": f"📚 Couldn't find #{target} on your bookmarks."}))
        return
    item = state["queue"].pop(idx)
    save_state(state)
    print(json.dumps({"ok": True, "user_message": f"🗑️  Removed from bookmarks: {item['title'] or item['url']}"}))


def cmd_migrate():
    """Walk all rows; for any row missing a brain page or posted_by /
    one_liner, run the right ingestor and update the row. Idempotent —
    rows already fully populated are left alone."""
    state = load_state()
    updated = 0
    skipped = 0
    failed = 0
    for bucket in ("queue", "read"):
        for r in state[bucket]:
            url = normalize_url(r["url"])
            r["url"] = url
            if not r.get("kind"):
                r["kind"] = url_kind(url)
            needs = (not r.get("brain_page")) or (not r.get("posted_by")) or (not r.get("one_liner"))
            if not needs:
                skipped += 1
                continue
            f = hydrate_row(url, r["kind"])
            had_brain = bool(f["brain_page"])
            r["title"] = clean_cell(r.get("title")) or f["title"]
            r["posted_by"] = f["posted_by"] or r.get("posted_by") or url_host(url)
            r["one_liner"] = f["one_liner"] or r.get("one_liner", "")
            r["brain_page"] = f["brain_page"] or r.get("brain_page", "")
            if had_brain:
                updated += 1
            else:
                failed += 1
    save_state(state)
    print(json.dumps({
        "ok": True, "updated": updated, "skipped": skipped, "failed": failed,
        "user_message": (
            f"📚 Migration done. {updated} rows ingested deep, "
            f"{skipped} already complete, {failed} failed (kept with shallow fields)."
        ),
    }))


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: reading-list.py {add <url>|show|read <n|slug>|drop <n|slug>|migrate}")
    cmd = sys.argv[1]
    if cmd == "--smoke":
        print(json.dumps({"ok": True, "skill": "reading-list", "user_message": "reading-list smoke ok"}))
        return
    if cmd == "add":
        if len(sys.argv) < 3:
            sys.exit("Usage: reading-list.py add <url>")
        cmd_add(sys.argv[2])
    elif cmd == "show":
        cmd_show()
    elif cmd == "read":
        if len(sys.argv) < 3:
            sys.exit("Usage: reading-list.py read <n|slug>")
        cmd_read(sys.argv[2])
    elif cmd == "drop":
        if len(sys.argv) < 3:
            sys.exit("Usage: reading-list.py drop <n|slug>")
        cmd_drop(sys.argv[2])
    elif cmd == "migrate":
        cmd_migrate()
    elif cmd == "backfill":
        # Legacy alias — old SKILL.md mentioned `backfill`. Redirect.
        cmd_migrate()
    else:
        sys.exit(f"Unknown command: {cmd}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {e}")
        print(json.dumps({"ok": False, "reason": "error", "error": str(e), "user_message": "⚠️ Reading-list operation failed — check the logs."}))
        sys.exit(1)
