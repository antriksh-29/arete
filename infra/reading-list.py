#!/usr/bin/env python3
# Reading list manager. Single source of truth: lists/to-read brain page.
#
# Subcommands:
#   add <url> [--deep]  Add URL. Shallow: title-only row. Deep: also runs
#                       full ingestion (PDF / X / HTML) and links the page.
#   show                Print numbered list for Telegram (queue + recent reads)
#   read <n|slug>       Move row from "Queue" to "Recently read"
#   drop <n|slug>       Remove row entirely
#   backfill            Hydrate existing rows that are missing title/type
#
# Always rebuilds the brain page after any mutation and commits to git so
# the page is in backups (R2 tarballs ~/.brain/, git captures changes).

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

# gbrain's shebang is `#!/usr/bin/env bun`. When this script is invoked by
# openclaw or systemd, bun isn't on PATH and every gbrain call silently
# returns exit 127 with no output. Prepend bun's bin dir to PATH so all
# downstream subprocess.run([GBRAIN, ...]) calls succeed.
os.environ["PATH"] = "/home/botuser/.bun/bin:" + os.environ.get("PATH", "")

INGEST_PDF = "/home/botuser/bin/ingest-pdf.py"
INGEST_X = "/home/botuser/bin/ingest-x-thread.py"


def log(msg):
    print(f"[reading-list] {msg}", file=sys.stderr)


def vault_get(key):
    r = subprocess.run(["bot-cred", "get", key], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def normalize_url(url):
    p = urllib.parse.urlsplit(url.strip())
    qs = urllib.parse.parse_qsl(p.query, keep_blank_values=False)
    qs = [(k, v) for k, v in qs if k.lower() not in ("s", "t", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "ref_src", "ref_url")]
    new_q = urllib.parse.urlencode(qs)
    path = p.path.rstrip("/") if p.path != "/" else p.path
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), path, new_q, ""))


def url_kind(url):
    if url.lower().endswith(".pdf"):
        return "pdf"
    if "twitter.com/" in url or "x.com/" in url:
        if "/status/" in url:
            return "x_thread"
        return "x_other"
    if "youtube.com/" in url or "youtu.be/" in url:
        return "youtube"
    return "html"


def fetch_title(url, kind):
    if kind in ("x_thread", "x_other"):
        return fetch_x_title(url)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Alfred reading-list)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read(500_000).decode("utf-8", errors="replace")
        m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE | re.DOTALL)
        if m:
            t = re.sub(r"\s+", " ", m.group(1)).strip()
            return t[:200]
    except Exception as e:
        log(f"title fetch failed for {url}: {e}")
    if kind == "pdf":
        slug = url.rsplit("/", 1)[-1]
        return slug.replace(".pdf", "").replace("-", " ").replace("_", " ").title()[:200]
    return url


def fetch_x_title(url):
    m = re.search(r"/status/(\d+)", url)
    if not m:
        return url
    tid = m.group(1)
    token = vault_get("X_API_BEARER_TOKEN")
    if not token:
        return url
    try:
        req = urllib.request.Request(
            f"https://api.x.com/2/tweets/{tid}?expansions=author_id&user.fields=username,name",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        if "data" not in d:
            return url
        text = d["data"].get("text", "")
        users = {u["id"]: u for u in d.get("includes", {}).get("users", [])}
        author = users.get(d["data"].get("author_id"), {})
        handle = author.get("username", "?")
        snippet = re.sub(r"\s+", " ", text).strip()[:120]
        return f"@{handle}: {snippet}" if snippet else f"@{handle} thread"
    except Exception as e:
        log(f"X title fetch failed for {url}: {e}")
        return url


# ----- list parsing & rendering -----

ROW_QUEUE_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*\[([^\]]+)\]\(([^)]+)\)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*$"
)
ROW_READ_RE = re.compile(
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


def load_state():
    """Load state from the brain page. Returns {'queue': [...], 'read': [...]}."""
    state = {"queue": [], "read": []}
    r = subprocess.run([GBRAIN, "get", LIST_SLUG], capture_output=True, text=True)
    if r.returncode != 0:
        return state
    text = r.stdout
    has_queue_heading = "## Queue" in text or "## To Read" in text
    has_read_heading = "## Recently read" in text or "## Read" in text
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
        if ls.startswith("|---") or ls.startswith("| #") or ls.startswith("| -"):
            continue
        if section is None:
            continue
        if section == "read":
            m = ROW_READ_RE.match(line)
            if m and not m.group(1).startswith("-"):
                state["read"].append({
                    "title": m.group(1),
                    "url": m.group(2),
                    "kind": m.group(3),
                    "added": m.group(4),
                    "read": m.group(5),
                    "brain_page": strip_wikilink(m.group(6)),
                })
                continue
        m = ROW_QUEUE_RE.match(line)
        if m and not m.group(1).startswith("-"):
            state[section].append({
                "title": m.group(1),
                "url": m.group(2),
                "kind": m.group(3),
                "added": m.group(4),
                "brain_page": strip_wikilink(m.group(5)),
            })
            continue
        m2 = LEGACY_ROW_RE.match(line)
        if m2:
            state[section].append({
                "title": "",
                "url": m2.group(2),
                "kind": "",
                "added": m2.group(3),
                "brain_page": "",
            })
    return state


def render(state):
    lines = [
        "---",
        "type: list",
        "title: To Read",
        f"updated: {TODAY}",
        "---",
        "",
        "# To Read",
        "",
        "Articles, threads, links, and PDFs queued for reading.",
        "Managed by `~/bin/reading-list.py`.",
        "",
        "## Queue",
        "",
    ]
    if state["queue"]:
        lines += ["| # | Title | Kind | Added | Brain page |",
                  "|---|-------|------|-------|------------|"]
        for i, r in enumerate(state["queue"], 1):
            bp = f"[[{r['brain_page']}]]" if r.get("brain_page") else ""
            title = r["title"] or "(untitled)"
            lines.append(f"| {i} | [{title}]({r['url']}) | {r.get('kind','')} | {r.get('added',TODAY)} | {bp} |")
    else:
        lines.append("_Queue is empty._")
    lines += ["", "## Recently read", ""]
    if state["read"]:
        lines += ["| # | Title | Kind | Added | Read | Brain page |",
                  "|---|-------|------|-------|------|------------|"]
        for i, r in enumerate(state["read"], 1):
            bp = f"[[{r['brain_page']}]]" if r.get("brain_page") else ""
            title = r["title"] or "(untitled)"
            lines.append(f"| {i} | [{title}]({r['url']}) | {r.get('kind','')} | {r.get('added','')} | {r.get('read', TODAY)} | {bp} |")
    else:
        lines.append("_Nothing read yet._")
    return "\n".join(lines) + "\n"


def save_state(state):
    body = render(state)
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

def cmd_add(url, deep=False):
    url = normalize_url(url)
    kind = url_kind(url)
    state = load_state()

    for r in state["queue"] + state["read"]:
        if normalize_url(r["url"]) == url:
            print(json.dumps({"ok": False, "reason": "duplicate", "user_message": f"📚 Already on your reading list: {r.get('title','(untitled)')[:80]}"}))
            return

    title = fetch_title(url, kind)
    log(f"adding {url} kind={kind} title='{title[:60]}' deep={deep}")

    brain_page = ""
    if deep:
        brain_page = run_deep_ingest(url, kind)

    state["queue"].append({
        "title": title,
        "url": url,
        "kind": kind,
        "added": TODAY,
        "brain_page": brain_page,
    })
    save_state(state)

    msg = f"✅ Added to reading list: {title[:80]}"
    if deep and brain_page:
        msg += f"\nDeep-ingested → {brain_page}"
    elif deep and not brain_page:
        msg += "\n(Deep ingestion failed — saved as shallow row.)"
    print(json.dumps({"ok": True, "title": title, "url": url, "kind": kind, "deep": deep, "brain_page": brain_page, "user_message": msg}))


def run_deep_ingest(url, kind):
    if kind == "pdf":
        cmd = ["python3", INGEST_PDF, url]
    elif kind == "x_thread":
        cmd = ["python3", INGEST_X, url]
    else:
        log(f"deep ingest not supported for kind={kind} — saving shallow")
        return ""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        log(f"deep ingest exit {r.returncode}: {r.stderr[-500:]}")
        return ""
    for line in reversed(r.stdout.strip().splitlines()):
        try:
            d = json.loads(line)
            if d.get("ok") and d.get("slug"):
                return d["slug"]
        except json.JSONDecodeError:
            continue
    return ""


def cmd_show():
    state = load_state()
    lines = ["📚 Reading list", ""]
    if state["queue"]:
        lines.append("To read:")
        for i, r in enumerate(state["queue"], 1):
            tag = f" → {r['brain_page']}" if r.get("brain_page") else ""
            lines.append(f"{i}. {r['title'] or r['url']}{tag}")
            lines.append(f"   {r['url']}")
    else:
        lines.append("Queue is empty.")
    if state["read"]:
        lines += ["", "Recently read:"]
        recent = state["read"][-5:]
        for r in recent:
            lines.append(f"• {r['title'] or r['url']} ({r.get('read','')})")
    print(json.dumps({"ok": True, "queue_count": len(state["queue"]), "read_count": len(state["read"]), "user_message": "\n".join(lines)}))


def resolve(target, queue):
    if target.isdigit():
        idx = int(target) - 1
        if 0 <= idx < len(queue):
            return idx
        return -1
    for i, r in enumerate(queue):
        if r["url"] == target or normalize_url(r["url"]) == normalize_url(target) or (r.get("brain_page") and r["brain_page"] == target):
            return i
    return -1


def cmd_read(target):
    state = load_state()
    idx = resolve(target, state["queue"])
    if idx < 0:
        print(json.dumps({"ok": False, "reason": "not_found", "user_message": f"📚 Couldn't find #{target} on your reading list. Try `/show` to see the numbered list."}))
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
        print(json.dumps({"ok": False, "reason": "not_found", "user_message": f"📚 Couldn't find #{target} on your reading list."}))
        return
    item = state["queue"].pop(idx)
    save_state(state)
    print(json.dumps({"ok": True, "user_message": f"🗑️  Removed from reading list: {item['title'] or item['url']}"}))


def cmd_backfill():
    state = load_state()
    n = 0
    for r in state["queue"] + state["read"]:
        if r.get("title") and r.get("kind"):
            continue
        url = normalize_url(r["url"])
        r["url"] = url
        r["kind"] = url_kind(url)
        if not r.get("title"):
            r["title"] = fetch_title(url, r["kind"])
        if not r.get("added"):
            r["added"] = TODAY
        n += 1
    if n:
        save_state(state)
    print(json.dumps({"ok": True, "backfilled": n, "user_message": f"📚 Backfilled {n} entries."}))


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: reading-list.py {add <url> [--deep]|show|read <n|slug>|drop <n|slug>|backfill}")
    cmd = sys.argv[1]
    if cmd == "add":
        if len(sys.argv) < 3:
            sys.exit("Usage: reading-list.py add <url> [--deep]")
        deep = "--deep" in sys.argv[3:]
        cmd_add(sys.argv[2], deep=deep)
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
    elif cmd == "backfill":
        cmd_backfill()
    else:
        sys.exit(f"Unknown command: {cmd}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {e}")
        print(json.dumps({"ok": False, "reason": "error", "error": str(e), "user_message": "⚠️ Reading list operation failed — check the logs."}))
        sys.exit(1)
