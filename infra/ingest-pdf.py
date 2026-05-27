#!/usr/bin/env python3
# PDF ingestion — extract text via pdftotext, analyze via GPT-5.4-mini,
# write a brain page at sources/<slug>.md, cross-link entities.
#
# Per identity/CONTENT_INGESTION.md: full text not summary, agent's own
# analysis layered on top, every entity gets back-link + timeline entry.
#
# Usage:
#   ingest-pdf.py <url-or-path>
# Reads OPENAI_API_KEY from arete-cred vault.
# Writes brain page, prints a short JSON envelope to stdout for the agent
# to relay to Telegram. Diagnostic info goes to stderr.

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from datetime import date
from pathlib import Path

BRAIN_DIR = Path.home() / ".brain"
SOURCES_DIR = BRAIN_DIR / "sources"
GBRAIN = "${STATE_HOME}/.bun/bin/gbrain"
TODAY = date.today().isoformat()

# gbrain's shebang is `#!/usr/bin/env bun`. When openclaw invokes this script,
# bun isn't on PATH and every gbrain call silently exits 127. Force PATH.
os.environ["PATH"] = "${STATE_HOME}/.bun/bin:" + os.environ.get("PATH", "")


def log(msg):
    print(f"[ingest-pdf] {msg}", file=sys.stderr)


def vault_get(key):
    return subprocess.run(
        ["arete-cred", "get", key], capture_output=True, text=True, check=True
    ).stdout.strip()


def fetch_pdf(src):
    if src.startswith(("http://", "https://")):
        log(f"downloading {src}")
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        urllib.request.urlretrieve(src, tmp.name)
        return tmp.name, src
    p = Path(src).expanduser()
    if not p.exists():
        sys.exit(f"ABORT: file not found: {src}")
    return str(p), src


def extract_text(pdf_path):
    out = tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w")
    out.close()
    subprocess.run(
        ["pdftotext", "-layout", pdf_path, out.name],
        check=True,
        capture_output=True,
    )
    text = Path(out.name).read_text(errors="replace")
    Path(out.name).unlink(missing_ok=True)
    return text


def detect_metadata(text, src):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    title = next(
        (ln for ln in lines[:10] if 8 <= len(ln) <= 120 and not ln.lower().startswith("page ")),
        Path(src).stem.replace("_", " ").replace("-", " ").title(),
    )
    author = "Unknown"
    for ln in lines[:30]:
        m = re.match(r"^(?:by|author[:\s]+)\s*(.{2,60})$", ln, re.IGNORECASE)
        if m:
            author = m.group(1).strip()
            break
    return title, author


def slugify(s):
    s = re.sub(r"[^\w\s-]", "", s.lower()).strip()
    return re.sub(r"[-\s]+", "-", s)[:80]


SYSTEM_PROMPT = """You are analyzing a document for Antriksh's personal knowledge brain.

Antriksh values: connections to what he already knows, sharp insights that re-frame the topic, specific quotes with their context. He does NOT want a summary.

Produce a brain page in this exact structure (markdown). Reference existing brain entities via [[slug]] syntax wherever the document mentions a person, company, or concept Antriksh might already track. Use plausible slugs (people/<first-last>, companies/<name>, concepts/<topic>) — the indexer resolves them.

# {title}
**Author:** {author} | **Source:** {source}

## Why this matters (Antriksh's frame)
2-4 sentences: central claim, why it's interesting given what Antriksh already cares about (AI agents, founder-mode, personal-assistant building, brain-as-memory). Reference [[slug]] where natural.

## Key arguments (in order)
Bulleted list, each bullet 1-3 sentences. Cover EVERY major argument the document makes, in the order it presents them. Do not skip sections. Use direct quotes sparingly when phrasing matters.

## Quotes worth remembering
- "exact quote" — page X if known, one-line gloss on why this quote earns its place
3-8 quotes max. Skip generic ones.

## Connections
Bullet list of [[slug]] links to existing brain entities mentioned or implied. One short reason per link.

## Open questions / follow-ups
2-4 sharp questions raised by the document worth digging into.

No preamble. No "this document discusses…" filler. No closing summary. Land the structure above and stop. Do NOT wrap the output in markdown code fences."""


def analyze(title, author, source, body):
    sys.path.insert(0, "${STATE_HOME}/bin")
    import llm  # type: ignore
    body_capped = body[:80000]
    user_msg = (
        f"Title: {title}\nAuthor: {author}\nSource: {source}\n\n"
        f"Document text below the line. Use only what's in this text — no outside knowledge or invented detail.\n\n"
        f"---\n{body_capped}\n---\n\nGenerate the brain page now."
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
    entities = set()
    for m in WIKILINK_RE.finditer(page_text):
        target = m.group(1).split("|")[0].strip()
        if target and target != slug:
            entities.add(target)
    linked = []
    stubs_created = 0
    for ent in sorted(entities):
        try:
            if ensure_stub(ent, slug, "PDF"):
                stubs_created += 1
            r1 = subprocess.run(
                [GBRAIN, "link", slug, ent], capture_output=True, check=False, text=True
            )
            r2 = subprocess.run(
                [
                    GBRAIN,
                    "timeline-add",
                    ent,
                    TODAY,
                    f"Mentioned in [[{slug}]] [Source: PDF ingestion {TODAY}]",
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            if r1.returncode == 0 and r2.returncode == 0:
                linked.append(ent)
            else:
                log(f"link/timeline failed for {ent}: link={r1.stdout.strip()[:120]} timeline={r2.stdout.strip()[:120]}")
        except Exception as e:
            log(f"cross-ref error for {ent}: {e}")
    log(f"created {stubs_created} stub pages")
    return linked


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: ingest-pdf.py <url-or-path>")
    src = sys.argv[1]
    if src == "--smoke":
        print(json.dumps({"ok": True, "skill": "ingest-pdf", "user_message": "ingest-pdf smoke ok"}))
        return

    SOURCES_DIR.mkdir(parents=True, exist_ok=True)

    pdf_path, original_source = fetch_pdf(src)
    text = extract_text(pdf_path)
    word_count = len(text.split())
    log(f"extracted {word_count} words")

    if word_count < 200:
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason": "scanned_or_empty",
                    "words": word_count,
                    "user_message": "📄 This PDF looks scanned (no extractable text). OCR isn't wired yet — paste the key passages and I'll ingest from there.",
                }
            )
        )
        return

    title, author = detect_metadata(text, original_source)
    slug = f"sources/{slugify(title)}"
    log(f"title='{title}' author='{author}' slug={slug}")

    page = analyze(title, author, original_source, text)

    page_path = BRAIN_DIR / f"{slug}.md"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(page + "\n")
    log(f"wrote {page_path}")

    subprocess.run([GBRAIN, "put", slug], input=page, text=True, check=False)

    linked = cross_reference(slug, page)
    log(f"linked {len(linked)} entities: {linked}")

    subprocess.run([GBRAIN, "sync"], capture_output=True, check=False)

    insight_match = re.search(
        r"##\s*Why this matters[^\n]*\n+([^\n]+)", page, re.IGNORECASE
    )
    insight = insight_match.group(1).strip() if insight_match else ""

    print(
        json.dumps(
            {
                "ok": True,
                "title": title,
                "author": author,
                "slug": slug,
                "linked_entities": linked,
                "insight_one_liner": insight[:240],
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {e}")
        print(json.dumps({"ok": False, "reason": "error", "error": str(e)}))
        sys.exit(1)
