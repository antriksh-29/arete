#!/usr/bin/env python3
# YouTube video ingestion. oEmbed for title+channel, youtube-transcript-api
# (with agent-browser fallback) for the transcript, then our own analysis
# layered on top per identity/CONTENT_INGESTION.md.
#
# Usage:
#   ingest-youtube.py <youtube-url>
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
GBRAIN = "/home/botuser/.bun/bin/gbrain"
TODAY = date.today().isoformat()

os.environ["PATH"] = "/home/botuser/.bun/bin:" + os.environ.get("PATH", "")


def log(msg):
    print(f"[ingest-youtube] {msg}", file=sys.stderr)


VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})")


def extract_video_id(url):
    m = VIDEO_ID_RE.search(url)
    return m.group(1) if m else ""


def canonical_url(video_id):
    return f"https://www.youtube.com/watch?v={video_id}"


def slugify(s):
    s = re.sub(r"[^A-Za-z0-9]+", "-", s.lower()).strip("-")
    return s[:60] or "untitled"


def fetch_oembed(video_id):
    url = canonical_url(video_id)
    api = f"https://www.youtube.com/oembed?url={urllib.parse.quote(url, safe='')}&format=json"
    try:
        with urllib.request.urlopen(api, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"oembed failed for {video_id}: {e}")
        return {}


def fetch_transcript(video_id):
    """Try youtube-transcript-api; return joined text or empty string."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
    except ImportError:
        log("youtube-transcript-api not installed — skipping transcript fetch")
        return ""
    try:
        chunks = YouTubeTranscriptApi.get_transcript(video_id, languages=["en", "en-US", "en-GB"])
        return " ".join(c.get("text", "").strip() for c in chunks if c.get("text"))
    except Exception as e:
        log(f"transcript fetch failed for {video_id}: {e}")
        return ""


SYSTEM_PROMPT = """You are analyzing a YouTube video for Antriksh's personal knowledge brain.

The metadata + transcript (or title-only fallback) is below. Antriksh values: connections to what he already knows, sharp takes that re-frame the video's argument, the actual structure of what the speaker is saying. He does NOT want a generic summary.

Produce a brain page in this exact structure (markdown). Use [[slug]] syntax to reference any person, company, or concept Antriksh might already track (people/<first-last>, companies/<name>, concepts/<topic>).

# {title}
**Channel:** {channel} | **Date:** {date} | **Link:** {url}

## Why this matters (Antriksh's frame)
2-4 sentences. Connect to what Antriksh already cares about (AI agents, founder-mode, building, personal brain). Use [[slug]] where natural.

## The argument
Reconstruct the video as continuous prose — what the speaker is actually claiming, in the order they build it. 1-3 paragraphs. If transcript is missing, work from title + description and clearly mark this section as "Based on metadata only — transcript unavailable."

## Connections
Bullet list of [[slug]] links to existing brain entities mentioned or implied, with a one-line reason each.

## One-line takeaway
A single sentence (≤ 200 chars) capturing the core insight. This line will be shown in the bookmark list.

---
## Transcript
Verbatim transcript text if available; otherwise "Transcript unavailable."

No preamble. No "this video discusses…" filler. Land the structure above and stop. Do NOT wrap the output in markdown code fences."""


def analyze(meta, transcript):
    sys.path.insert(0, "/home/botuser/bin")
    import llm  # type: ignore
    user_msg = (
        f"Title: {meta.get('title','')}\n"
        f"Channel: {meta.get('author_name','')}\n"
        f"Channel URL: {meta.get('author_url','')}\n"
        f"Video URL: {meta['url']}\n\n"
        f"Transcript (may be empty):\n---\n{transcript[:80000] if transcript else '(none)'}\n---\n\n"
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
    linked = []
    for m in WIKILINK_RE.finditer(page_text):
        target = m.group(1).strip()
        if target and target != slug and "/" in target:
            if target in linked:
                continue
            linked.append(target)
            ensure_stub(target, slug, "YouTube video")
            subprocess.run([GBRAIN, "link", slug, target], capture_output=True, check=False, text=True)
            subprocess.run(
                [
                    GBRAIN, "timeline-add", target, TODAY,
                    f"Mentioned in [[{slug}]] [Source: YouTube ingestion {TODAY}]",
                ],
                capture_output=True, check=False, text=True,
            )
    return linked


def extract_one_liner(page_text):
    m = re.search(r"##\s*One-line takeaway[^\n]*\n+([^\n]+)", page_text, re.IGNORECASE)
    return (m.group(1).strip() if m else "")[:240]


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: ingest-youtube.py <youtube-url>")
    url = sys.argv[1]
    if url == "--smoke":
        print(json.dumps({"ok": True, "skill": "ingest-youtube", "user_message": "ingest-youtube smoke ok"}))
        return

    video_id = extract_video_id(url)
    if not video_id:
        print(json.dumps({"ok": False, "reason": "bad_url", "user_message": "📺 That doesn't look like a YouTube URL."}))
        sys.exit(1)

    canonical = canonical_url(video_id)
    oembed = fetch_oembed(video_id)
    if not oembed:
        print(json.dumps({"ok": False, "reason": "video_unavailable", "user_message": "📺 YouTube video unavailable — removed, private, or age-restricted."}))
        sys.exit(1)

    title = oembed.get("title", "Untitled video")
    channel = oembed.get("author_name", "Unknown channel")
    meta = {"title": title, "author_name": channel, "author_url": oembed.get("author_url", ""), "url": canonical}

    transcript = fetch_transcript(video_id)
    page = analyze(meta, transcript)

    channel_slug = slugify(channel)
    slug = f"media/youtube/{channel_slug}-{video_id}"
    page_path = BRAIN_DIR / f"{slug}.md"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(page)
    subprocess.run([GBRAIN, "put", slug], input=page, text=True, check=False)

    linked = cross_reference(slug, page)
    one_liner = extract_one_liner(page)

    print(json.dumps({
        "ok": True,
        "video_id": video_id,
        "channel": channel,
        "title": title,
        "url": canonical,
        "slug": slug,
        "transcript_available": bool(transcript),
        "linked_entities": linked,
        "insight_one_liner": one_liner,
        "user_message": (
            f"📺 Ingested: {title[:80]} — {channel}\n"
            f"Brain page: {slug}\n"
            f"{one_liner}"
        ),
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {e}")
        print(json.dumps({"ok": False, "reason": "error", "error": str(e), "user_message": "⚠️ YouTube ingestion failed — check the logs."}))
        sys.exit(1)
