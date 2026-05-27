#!/usr/bin/env python3
# Compile the X corpora into identity/X_VOICE.md — the master voice profile
# used by /x-draft. Distills voice fingerprints per handle and locks in the
# default blend recipe (20% the user / 80% admired averaged) plus the
# handle-override behavior.
#
# Run after x-corpus-collect.py, and again whenever new approved drafts have
# accumulated enough to refresh the fingerprint.

import json
import os
import subprocess
import sys
import urllib.request
from datetime import date
from pathlib import Path

os.environ["PATH"] = "${STATE_HOME}/.bun/bin:" + os.environ.get("PATH", "")

BRAIN_DIR = Path.home() / ".brain"
WORKSPACE = Path.home() / ".openclaw" / "workspace"
IDENTITY_DIR = WORKSPACE / "identity"
GBRAIN = "${STATE_HOME}/.bun/bin/gbrain"
TODAY = date.today().isoformat()

OWN_HANDLE = "myself"
ADMIRED = ["paulg", "naval", "sama", "garrytan", "vkhosla"]


def log(msg):
    print(f"[x-style] {msg}", file=sys.stderr)


def vault_get(key):
    return subprocess.run(["arete-cred", "get", key], capture_output=True, text=True, check=True).stdout.strip()


def load_corpus(handle):
    p = BRAIN_DIR / "corpus" / f"x-{handle}.md"
    if not p.exists():
        return ""
    return p.read_text()


SYSTEM_PROMPT = """You are extracting a precise voice fingerprint from a corpus of X (Twitter) posts.

For each handle's corpus, produce a concrete, observable fingerprint that another LLM can use to imitate the voice. Avoid generic adjectives like "thoughtful" or "engaging". Be specific.

For each handle, capture:
- Sentence patterns: typical length (short/medium/mixed), declarative vs question vs imperative, single-clause vs compound
- Recurring openers: how do they start tweets? ("If you...", "The thing about...", "Nobody talks about...", etc — list 3-6 actual patterns observed)
- Vocabulary signatures: words/phrases they reach for. Words/phrases they avoid.
- Closing patterns: how they end (call to action, observation, no closer)
- Rhetorical moves: contrarian opener, list-of-N, single-line aphorism, question-then-answer, story-then-lesson, etc.
- What they DON'T do: emoji density, hashtag usage, @-mentions, hedging language

Output STRICT JSON only. Schema:

{
  "myself": {
    "fingerprint": "multi-line markdown describing the voice as bullet points with concrete examples drawn from the corpus",
    "exemplar_tweet_ids": ["1-3 tweet excerpts that best capture this voice"]
  },
  "paulg": { "fingerprint": "...", "exemplar_tweet_ids": ["..."] },
  "naval": { "fingerprint": "...", "exemplar_tweet_ids": ["..."] },
  "sama": { "fingerprint": "...", "exemplar_tweet_ids": ["..."] },
  "garrytan": { "fingerprint": "...", "exemplar_tweet_ids": ["..."] },
  "vkhosla": { "fingerprint": "...", "exemplar_tweet_ids": ["..."] }
}

Rules:
- Quote actual phrases from the corpus when describing patterns. Generic descriptions are weak; concrete ones compound.
- Don't list a pattern unless you can point to ≥2 tweets exhibiting it.
- The fingerprint should be 8-15 short bullets, not paragraphs."""


def synthesize(corpora, openai_key):
    user_msg_parts = []
    for handle, body in corpora.items():
        user_msg_parts.append(f"=== @{handle} corpus ===\n\n{body[:30000]}\n")
    user_msg = "\n".join(user_msg_parts) + "\n\nGenerate the JSON fingerprints now."

    payload = {
        "model": "gpt-5.4-mini",
        "input": user_msg,
        "instructions": SYSTEM_PROMPT,
        "max_output_tokens": 8000,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        resp = json.loads(r.read())
    text = ""
    for item in resp.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    text = c["text"]
                    break
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


def render(profiles):
    own_fp = profiles.get(OWN_HANDLE, {}).get("fingerprint", "_(not extracted)_")
    lines = [
        f"# X (Twitter) voice profile",
        "",
        f"_Compiled {TODAY} from `corpus/x-*` brain pages by `~/bin/x-style-profile.py`._",
        "",
        "## Default blend (locked)",
        "",
        "- **@myself: 20%** (own voice)",
        f"- **Admired (averaged across {', '.join('@' + h for h in ADMIRED)}): 80%**",
        "",
        "When generating a draft, the LLM should:",
        "1. Anchor in admired-voice patterns (80% of the texture)",
        "2. Layer the user's own signatures on top (20% — vocabulary, openers, closers)",
        "3. Keep it under 280 chars",
        "",
        "## Override behavior",
        "",
        'If the user invokes a handle override (e.g. "more garry style", "like naval would say it",',
        '"in paulg style"), shift the blend:',
        "",
        "- Named handle: 60%",
        "- the user: 20%",
        "- Other admired (averaged): 20%",
        "",
        "If the user invokes \"pure me\" or \"sound like me\", shift to 100% own voice.",
        "",
        "---",
        "",
        f"## @{OWN_HANDLE} fingerprint (own voice)",
        "",
        own_fp,
        "",
    ]
    for handle in ADMIRED:
        fp = profiles.get(handle, {}).get("fingerprint", "_(not extracted)_")
        lines += ["---", "", f"## @{handle} fingerprint", "", fp, ""]
    return "\n".join(lines) + "\n"


def main():
    corpora = {}
    for h in [OWN_HANDLE] + ADMIRED:
        body = load_corpus(h)
        if not body:
            log(f"@{h} corpus missing — skipping")
            continue
        corpora[h] = body

    if OWN_HANDLE not in corpora or len(corpora) < 3:
        sys.exit(f"ABORT: not enough corpora to synthesize (have {list(corpora.keys())})")

    log(f"synthesizing voice profile from {len(corpora)} corpora")
    openai_key = vault_get("OPENAI_API_KEY")
    profiles = synthesize(corpora, openai_key)

    body = render(profiles)
    IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
    out_path = IDENTITY_DIR / "X_VOICE.md"
    out_path.write_text(body)
    log(f"wrote {out_path}")

    print(json.dumps({"ok": True, "handles_profiled": list(profiles.keys()), "path": str(out_path)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {e}")
        print(json.dumps({"ok": False, "reason": "error", "error": str(e)}))
        sys.exit(1)
