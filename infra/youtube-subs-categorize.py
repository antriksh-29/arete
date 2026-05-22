#!/usr/bin/env python3
"""youtube-subs-categorize.py — one-shot fetch + categorize + brain write.

Replaces the slow agent-driven flow (5+ minutes, watchdog-killed) with
a deterministic script invocation (~60-90s, single agent turn).

Flow:
  1. composio-paginate.py YOUTUBE_LIST_USER_SUBSCRIPTIONS → 161 channels
  2. Single Kimi K2.6 call → categorize all 161 into holistic buckets
  3. Write brain page at references/youtube-subscriptions-<date>.md
  4. Stdout: success summary (count + categories) for the agent to relay

The agent's job is ONLY to invoke this and relay the summary. No
pagination loops, no LLM categorization in the agent context window,
no fabrication risk.

Usage:
  youtube-subs-categorize.py [--brain-page SLUG] [--account NAME]

Default brain page: references/youtube-subscriptions-<YYYY-MM-DD>.md
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path.home() / "personal-assistant"))
PAGINATE_SCRIPT = REPO_ROOT / "infra" / "composio-paginate.py"
BRAIN_ROOT = Path.home() / ".brain"


def vault_get(name: str) -> str:
    try:
        r = subprocess.run(
            ["/usr/local/bin/bot-cred", "get", name],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def fetch_subscriptions(account: str | None) -> list[dict]:
    """Call composio-paginate.py and return the items list."""
    cmd = [
        "python3", str(PAGINATE_SCRIPT),
        "YOUTUBE_LIST_USER_SUBSCRIPTIONS",
        "--args", json.dumps({"part": "snippet", "maxResults": 15, "mine": True}),
        "--slim", "snippet.title,snippet.resourceId.channelId,snippet.description,id",
        "--max-pages", "20",
    ]
    if account:
        cmd += ["--account", account]
    print(f"[fetch] running composio-paginate...", file=sys.stderr)
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"paginate failed (exit {r.returncode}): {r.stderr[:500]}")
    payload = json.loads(r.stdout)
    items = payload.get("items", [])
    print(f"[fetch] got {len(items)} items in {time.time()-t0:.1f}s", file=sys.stderr)
    return items


def categorize_with_kimi(channels: list[dict]) -> dict[str, list[dict]]:
    """Single Moonshot call: take all channel titles + descriptions,
    return a dict of {category: [channel objects]}."""
    api_key = os.environ.get("MOONSHOT_API_KEY") or vault_get("MOONSHOT_API_KEY")
    if not api_key:
        raise RuntimeError("MOONSHOT_API_KEY not available")

    # Build compact payload — title + a short description trunc
    compact = []
    for it in channels:
        snip = it.get("snippet", {})
        title = snip.get("title", "(no title)")
        desc = (snip.get("description") or "").strip().split("\n")[0][:200]
        channel_id = snip.get("resourceId", {}).get("channelId", "")
        compact.append({
            "title": title,
            "channel_id": channel_id,
            "desc": desc,
        })

    system_prompt = """You categorize YouTube channels into holistic, mutually-exclusive buckets for a personal-knowledge brain page.

Given a JSON array of channels (each with `title` and short `desc`), return ONLY this JSON shape:

{
  "categories": {
    "<Category Name>": ["<channel title>", "<channel title>", ...],
    "<Another Category>": [...]
  }
}

RULES:
1. Use 6-12 categories that ACTUALLY describe the user's interests as shown by what they subscribe to. Examples of good category names: "Football & Fantasy", "F1 & Motorsport", "Cricket", "Startups & Tech", "Business & Finance", "News & Current Affairs", "Comedy & Entertainment", "Music", "Film & TV", "Education & Learning", "Health & Fitness", "Travel & Food", "Personal Development", "Politics & Society".
2. Every input channel MUST appear in exactly ONE category. If you can't decide, prefer the broader category.
3. Use the CHANNEL TITLE verbatim from the input — do not abbreviate or rephrase.
4. The output JSON object's keys are category names; values are arrays of channel-title strings.
5. Output raw JSON. First char `{`, last char `}`. No prose, no markdown fences.

If a channel is ambiguous, pick the dominant theme based on its name/description and place it once."""

    user_msg = f"Categorize these {len(compact)} YouTube channels:\n\n```json\n{json.dumps(compact, indent=2)}\n```"

    import urllib.request, urllib.error
    body = json.dumps({
        "model": "moonshot-v1-32k",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 12000,
        "temperature": 0.3,
    }).encode()

    print(f"[categorize] calling Moonshot ({len(compact)} channels, {len(body)}b payload)", file=sys.stderr)
    t0 = time.time()
    req = urllib.request.Request(
        "https://api.moonshot.ai/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Moonshot HTTP {e.code}: {e.read().decode()[:300]}")
    elapsed = time.time() - t0
    content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise RuntimeError(f"Moonshot returned empty content. Full response: {json.dumps(resp)[:500]}")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Moonshot returned non-JSON: {content[:500]}")
    cats = parsed.get("categories", {})
    print(f"[categorize] got {len(cats)} categories in {elapsed:.1f}s", file=sys.stderr)
    # Build {category: [full channel dicts]} by looking up each title back
    title_to_channel = {c["title"]: c for c in compact}
    out = {}
    placed = set()
    for cat_name, titles in cats.items():
        if not isinstance(titles, list):
            continue
        bucket = []
        for t in titles:
            if t in title_to_channel:
                bucket.append(title_to_channel[t])
                placed.add(t)
            else:
                # Title not in our list — LLM hallucination, skip
                pass
        if bucket:
            out[cat_name] = bucket
    # Catch any unplaced channels into "Other"
    missing = [c for c in compact if c["title"] not in placed]
    if missing:
        out.setdefault("Other / Uncategorized", []).extend(missing)
    return out


def write_brain_page(categorized: dict[str, list[dict]], total_input: int, slug: str) -> Path:
    """Write the brain markdown. Returns the path."""
    page_dir = BRAIN_ROOT / "references"
    page_dir.mkdir(parents=True, exist_ok=True)
    page_path = page_dir / f"{slug}.md"

    today = datetime.now().strftime("%Y-%m-%d")
    total_categorized = sum(len(v) for v in categorized.values())

    lines = [
        "---",
        f"title: YouTube Subscriptions",
        f"created: {today}",
        f"updated: {today}",
        f"tags: [reference, youtube, subscriptions]",
        f"source: composio-paginate.py + youtube-subs-categorize.py",
        f"total_channels: {total_categorized}",
        "---",
        "",
        "# YouTube Subscriptions",
        "",
        f"Auto-generated on {today}. Pulled {total_input} subscriptions via Composio's YouTube",
        f"connector, categorized into {len(categorized)} holistic buckets by Moonshot v1-32k.",
        "",
    ]

    # Sort categories by size (largest first) — usually most informative
    sorted_cats = sorted(categorized.items(), key=lambda kv: -len(kv[1]))
    for cat_name, channels in sorted_cats:
        lines.append(f"## {cat_name} ({len(channels)})")
        lines.append("")
        # Sort channels alphabetically within category for readability
        for ch in sorted(channels, key=lambda c: c["title"].lower()):
            cid = ch.get("channel_id", "")
            url = f"https://www.youtube.com/channel/{cid}" if cid else ""
            if url:
                lines.append(f"- [{ch['title']}]({url})")
            else:
                lines.append(f"- {ch['title']}")
        lines.append("")

    lines += [
        "## Notes",
        "",
        f"- Source: YouTube API via Composio's `YOUTUBE_LIST_USER_SUBSCRIPTIONS` tool.",
        f"- Pagination via `infra/composio-paginate.py` (15 items/page to stay under truncation).",
        f"- Categorization via single Moonshot v1-32k call on titles + descriptions.",
        f"- Refresh: `python3 ~/personal-assistant/infra/youtube-subs-categorize.py`",
        "",
    ]
    page_path.write_text("\n".join(lines))
    return page_path


def commit_and_push_brain(page_path: Path, total_channels: int) -> dict:
    """Commit the brain edit and push to GitHub. The brain repo uses
    a per-skill commit pattern (see reading-checkin.sh / water-log.sh).
    Without this step, files land on disk but never reach the GitHub
    mirror — the failure mode observed 2026-05-22.

    Returns {"committed": bool, "pushed": bool, "sha": str, "stderr": str}.
    """
    repo = Path.home() / ".brain"
    if not (repo / ".git").is_dir():
        return {"committed": False, "pushed": False, "sha": "", "stderr": "no .brain/.git dir"}

    rel = page_path.relative_to(repo) if str(page_path).startswith(str(repo)) else page_path

    # Stage only this file (avoid `git add -A` which sweeps in unrelated edits)
    add = subprocess.run(["git", "-C", str(repo), "add", "--", str(rel)],
                         capture_output=True, text=True, timeout=15)
    if add.returncode != 0:
        return {"committed": False, "pushed": False, "sha": "", "stderr": f"add: {add.stderr[:200]}"}

    # If nothing changed (e.g. re-run with identical content), bail cleanly
    diff = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"],
                          capture_output=True, text=True, timeout=10)
    if diff.returncode == 0:
        return {"committed": False, "pushed": False, "sha": "", "stderr": "no changes to commit"}

    msg = f"reference: youtube subscriptions complete ({total_channels} channels)"
    commit = subprocess.run(["git", "-C", str(repo), "commit", "-m", msg],
                            capture_output=True, text=True, timeout=15)
    if commit.returncode != 0:
        return {"committed": False, "pushed": False, "sha": "", "stderr": f"commit: {commit.stderr[:200]}"}

    sha_r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
    sha = sha_r.stdout.strip()

    push = subprocess.run(["git", "-C", str(repo), "push"],
                          capture_output=True, text=True, timeout=30)
    if push.returncode != 0:
        return {"committed": True, "pushed": False, "sha": sha,
                "stderr": f"push: {push.stderr[:200]}"}
    return {"committed": True, "pushed": True, "sha": sha, "stderr": ""}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain-page", default=None, help="Brain page slug (default: youtube-subscriptions-<date>)")
    ap.add_argument("--account", default=None)
    ap.add_argument("--no-push", action="store_true", help="Skip git commit+push (default: push)")
    args = ap.parse_args()

    slug = args.brain_page or f"youtube-subscriptions-{datetime.now().strftime('%Y-%m-%d')}"

    t0 = time.time()
    try:
        channels = fetch_subscriptions(args.account)
        if not channels:
            print("ERROR: no channels returned from paginate", file=sys.stderr)
            return 1
        categorized = categorize_with_kimi(channels)
        page_path = write_brain_page(categorized, len(channels), slug)
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    total_categorized = sum(len(v) for v in categorized.values())

    # Commit + push the brain page so it lands on GitHub (without this,
    # the file sits on disk locally and divergence with the GitHub mirror
    # accumulates — observed 2026-05-22 image-v36 failure).
    push_result = {"committed": False, "pushed": False, "sha": "", "stderr": "skipped"}
    if not args.no_push:
        push_result = commit_and_push_brain(page_path, total_categorized)
        print(f"[git] committed={push_result['committed']} pushed={push_result['pushed']} "
              f"sha={push_result['sha']} {push_result['stderr']}", file=sys.stderr)

    elapsed = time.time() - t0
    summary = {
        "ok": True,
        "total_channels": total_categorized,
        "input_count": len(channels),
        "categories": {k: len(v) for k, v in categorized.items()},
        "brain_page": str(page_path),
        "elapsed_s": round(elapsed, 1),
        "git": push_result,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
