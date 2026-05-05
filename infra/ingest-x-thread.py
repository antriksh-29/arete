#!/usr/bin/env python3
# X (Twitter) thread ingestion. Reconstructs the full bundle (seed tweet,
# canonical thread by same author, quoted/referenced tweets, notable
# replies, linked URLs) and writes a brain page at
# media/social/x-<handle>-<YYYYMMDD>.md.
#
# Per identity/CONTENT_INGESTION.md: full text not summary, agent's own
# analysis layered on top, every entity gets back-link + timeline entry.
#
# Usage:
#   ingest-x-thread.py <tweet-url>
# Reads OPENAI_API_KEY and X_API_BEARER_TOKEN from bot-cred vault.
# Prints a short JSON envelope to stdout for the agent to relay.

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
X_BASE = "https://api.x.com/2"
TODAY = date.today().isoformat()

# gbrain's shebang is `#!/usr/bin/env bun`. When openclaw invokes this script,
# bun isn't on PATH and every gbrain call silently exits 127. Force PATH.
os.environ["PATH"] = "/home/botuser/.bun/bin:" + os.environ.get("PATH", "")


def log(msg):
    print(f"[ingest-x-thread] {msg}", file=sys.stderr)


def vault_get(key):
    return subprocess.run(
        ["bot-cred", "get", key], capture_output=True, text=True, check=True
    ).stdout.strip()


def x_get(path, params, token):
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{X_BASE}{path}?{qs}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def parse_tweet_id(url):
    m = re.search(r"/status/(\d+)", url)
    if not m:
        sys.exit(f"ABORT: couldn't extract tweet ID from URL: {url}")
    return m.group(1)


def fetch_seed(tweet_id, token):
    return x_get(
        f"/tweets/{tweet_id}",
        {
            "expansions": "author_id,referenced_tweets.id,referenced_tweets.id.author_id,attachments.media_keys",
            "tweet.fields": "created_at,conversation_id,entities,public_metrics,referenced_tweets",
            "user.fields": "name,username,description",
        },
        token,
    )


def fetch_conversation(conversation_id, token):
    return x_get(
        "/tweets/search/recent",
        {
            "query": f"conversation_id:{conversation_id}",
            "max_results": "100",
            "expansions": "author_id,referenced_tweets.id",
            "tweet.fields": "created_at,conversation_id,entities,public_metrics,in_reply_to_user_id,referenced_tweets",
            "user.fields": "name,username",
        },
        token,
    )


def slugify(s):
    s = re.sub(r"[^\w\s-]", "", s.lower()).strip()
    return re.sub(r"[-\s]+", "-", s)[:80]


def fetch_url_gloss(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read(200_000).decode("utf-8", errors="replace")
        m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
        title = m.group(1).strip() if m else url
        m = re.search(
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)',
            html,
            re.IGNORECASE,
        )
        desc = m.group(1).strip() if m else ""
        return f"{title} — {desc[:200]}" if desc else title
    except Exception as e:
        log(f"linked-URL fetch failed for {url}: {e}")
        return url


def assemble_bundle(seed_resp, convo_resp, token):
    seed = seed_resp.get("data", {})
    seed_includes = seed_resp.get("includes", {})
    seed_users = {u["id"]: u for u in seed_includes.get("users", [])}
    seed_tweets = {t["id"]: t for t in seed_includes.get("tweets", [])}

    seed_author = seed_users.get(seed.get("author_id"), {})
    handle = seed_author.get("username", "unknown")
    name = seed_author.get("name", handle)

    convo_data = convo_resp.get("data", []) or []
    convo_includes = convo_resp.get("includes", {})
    convo_users = {u["id"]: u for u in convo_includes.get("users", [])}

    canonical = [seed]
    for t in convo_data:
        if t["id"] == seed["id"]:
            continue
        if t.get("author_id") == seed.get("author_id"):
            irt_user = (
                t.get("in_reply_to_user_id") or seed.get("author_id")
            )
            if irt_user == seed.get("author_id"):
                canonical.append(t)
    canonical.sort(key=lambda x: x.get("created_at", ""))

    notable_replies = []
    for t in convo_data:
        if t.get("author_id") == seed.get("author_id"):
            continue
        likes = (t.get("public_metrics") or {}).get("like_count", 0)
        if likes >= 5 or len(t.get("text", "")) > 280:
            notable_replies.append(t)
    notable_replies.sort(
        key=lambda x: (x.get("public_metrics") or {}).get("like_count", 0),
        reverse=True,
    )
    notable_replies = notable_replies[:5]

    referenced = []
    for ref in seed.get("referenced_tweets", []) or []:
        ref_t = seed_tweets.get(ref["id"])
        if ref_t:
            ref_author = seed_users.get(ref_t.get("author_id"), {})
            referenced.append(
                {
                    "type": ref["type"],
                    "text": ref_t.get("text", ""),
                    "author_handle": ref_author.get("username", ""),
                }
            )

    linked_urls = []
    for t in canonical:
        for url_ent in (t.get("entities") or {}).get("urls", []) or []:
            expanded = url_ent.get("expanded_url")
            if (
                expanded
                and "twitter.com" not in expanded
                and "x.com" not in expanded
                and "t.co" not in expanded
                and expanded not in [u["url"] for u in linked_urls]
            ):
                linked_urls.append({"url": expanded, "gloss": fetch_url_gloss(expanded)})

    return {
        "handle": handle,
        "name": name,
        "bio": seed_author.get("description", ""),
        "seed_id": seed["id"],
        "seed_created_at": seed.get("created_at", ""),
        "canonical_thread": [
            {"text": t.get("text", ""), "created_at": t.get("created_at", "")}
            for t in canonical
        ],
        "referenced_tweets": referenced,
        "notable_replies": [
            {
                "author": convo_users.get(t.get("author_id"), {}).get("username", "?"),
                "text": t.get("text", ""),
                "likes": (t.get("public_metrics") or {}).get("like_count", 0),
            }
            for t in notable_replies
        ],
        "linked_urls": linked_urls,
    }


SYSTEM_PROMPT = """You are analyzing an X (Twitter) thread for Antriksh's personal knowledge brain.

The bundle below is a complete reconstruction: original tweet, the author's full thread, quoted/referenced tweets, notable replies, and linked articles with one-line glosses.

Antriksh values: connections to what he already knows, sharp takes that re-frame the thread, the actual argument structure (not a tweet-by-tweet recap). He does NOT want a summary.

Produce a brain page in this exact structure (markdown). Use [[slug]] syntax to reference any person, company, or concept Antriksh might already track (people/<first-last>, companies/<name>, concepts/<topic>).

# {handle}: <topic in 6-10 words>
**Author:** {name} (@{handle}) | **Date:** {date} | **Link:** {url}

## Why this matters (Antriksh's frame)
2-4 sentences. Connect to what Antriksh already cares about (AI agents, founder-mode, building, personal brain). Use [[slug]] where natural.

## The argument
Reconstruct the thread as continuous prose — what the author is actually claiming, in the order they build it. Don't list tweet-by-tweet. Synthesize the full arc into 1-3 paragraphs.

## Quoted / referenced tweets
For each quoted/referenced tweet, 1-2 sentences on what it adds. If none, say "None."

## Linked context
For each linked article, 1-2 sentences on how it supports the argument. If none, say "None."

## Notable replies
Only the substantive ones — pushback, additions, corrections. Skip "great thread!" etc. If none qualify, say "None."

## Connections
Bullet list of [[slug]] links to existing brain entities mentioned or implied, with a one-line reason each.

---
## Raw thread
Verbatim thread text, one tweet per bullet, in chronological order. This is the evidence base — preserve exact phrasing.

No preamble. No "this thread discusses…" filler. Land the structure above and stop. Do NOT wrap the output in markdown code fences."""


def analyze(bundle, source_url):
    sys.path.insert(0, "/home/botuser/bin")
    import llm  # type: ignore
    user_msg = (
        f"Author: {bundle['name']} (@{bundle['handle']})\n"
        f"Bio: {bundle['bio']}\n"
        f"Date: {bundle['seed_created_at']}\n"
        f"Source: {source_url}\n\n"
        f"Bundle JSON below the line. Use only what's in this bundle.\n\n"
        f"---\n{json.dumps(bundle, indent=2)[:80000]}\n---\n\n"
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
    entities = set()
    for m in WIKILINK_RE.finditer(page_text):
        target = m.group(1).split("|")[0].strip()
        if target and target != slug:
            entities.add(target)
    linked = []
    stubs_created = 0
    for ent in sorted(entities):
        try:
            if ensure_stub(ent, slug, "X thread"):
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
                    f"Mentioned in [[{slug}]] [Source: X thread ingestion {TODAY}]",
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
        sys.exit("Usage: ingest-x-thread.py <tweet-url>")
    url = sys.argv[1]

    token = vault_get("X_API_BEARER_TOKEN")
    if not token:
        sys.exit("ABORT: X_API_BEARER_TOKEN not in vault")

    tweet_id = parse_tweet_id(url)
    log(f"seed tweet id: {tweet_id}")

    seed_resp = fetch_seed(tweet_id, token)
    if "data" not in seed_resp:
        if "errors" in seed_resp:
            err = seed_resp["errors"][0].get("title", "unknown")
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": "tweet_unavailable",
                        "user_message": f"🔒 That tweet isn't accessible ({err}). Paste the text if you have it.",
                    }
                )
            )
            return
        sys.exit(f"ABORT: unexpected seed response: {json.dumps(seed_resp)[:300]}")

    convo_id = seed_resp["data"].get("conversation_id", tweet_id)
    convo_resp = fetch_conversation(convo_id, token)

    bundle = assemble_bundle(seed_resp, convo_resp, token)
    log(
        f"@{bundle['handle']}: {len(bundle['canonical_thread'])} thread tweets, "
        f"{len(bundle['referenced_tweets'])} referenced, "
        f"{len(bundle['notable_replies'])} notable replies, "
        f"{len(bundle['linked_urls'])} linked URLs"
    )

    date_str = bundle["seed_created_at"][:10].replace("-", "") if bundle["seed_created_at"] else TODAY.replace("-", "")
    slug = f"media/social/x-{slugify(bundle['handle'])}-{date_str}"

    page = analyze(bundle, url)
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
                "handle": bundle["handle"],
                "name": bundle["name"],
                "thread_length": len(bundle["canonical_thread"]),
                "referenced_count": len(bundle["referenced_tweets"]),
                "notable_replies": len(bundle["notable_replies"]),
                "slug": slug,
                "linked_entities": linked,
                "insight_one_liner": insight[:240],
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        log(f"HTTP {e.code}: {body}")
        if e.code == 429:
            print(json.dumps({"ok": False, "reason": "rate_limit", "user_message": "🐦 X API rate limit hit — try again in a few minutes."}))
        elif e.code in (401, 403):
            print(json.dumps({"ok": False, "reason": "auth", "user_message": "🔑 X API auth failed — check the bearer token."}))
        else:
            print(json.dumps({"ok": False, "reason": f"http_{e.code}", "user_message": f"⚠️ X API returned {e.code} — see logs."}))
        sys.exit(1)
    except Exception as e:
        log(f"FAILED: {e}")
        print(json.dumps({"ok": False, "reason": "error", "error": str(e)}))
        sys.exit(1)
