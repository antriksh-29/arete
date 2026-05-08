---
name: x-draft
version: 1
script: infra/x-draft.py
helper_scripts:
  - infra/x-corpus-collect.py
  - infra/x-style-profile.py
  - infra/x-approve.py
mutating: true
cron: null
eval: skills/x-draft/eval.yaml
last_audited: 2026-05-08
description: |
  Tweet composition. The user wants to draft a new tweet, reply, or quote-
  tweet in their voice blended with admired accounts, then approve/edit/
  reject to compound the voice corpus.
  WHEN: "draft a tweet about <X>", "what should I tweet about <X>", "reply
  to <tweet-url>: <thought>", "quote retweet <url>: <thought>", style
  overrides like "more garry style", "in paulg style", "pure antriksh", and
  the post-draft actions "approve", "edit: <new text>", "reject", "try
  again".
  NOT WHEN: User wants to ingest a tweet they were sent (→ ingest-x-thread),
  save a tweet for later (→ reading-list), or general writing help that
  isn't about X (→ default agent).
trigger: |
  - "draft a tweet about <topic>", "what should I tweet about <X>"
  - "reply to <tweet-url>: <raw thought>"
  - "quote retweet <tweet-url>: <raw thought>", "quote tweet <url>: <raw>"
  - Style overrides: "more garry style", "like naval would say it", "in paulg style", "pure antriksh"
  - After a draft: "approve" / "looks good", "edit: <new text>", "reject" / "try again"
writes_to:
  - corpus/x-antriksssh
  - identity/X_VOICE.md
brain_dirs_read:
  - corpus/
---

# /x-draft

Tweet drafting engine. Reads `identity/X_VOICE.md` (compiled voice profile)
and the corpus pages (`corpus/x-antriksssh.md`, `corpus/x-paulg.md`, etc.)
to produce drafts that blend Antriksh's voice (20%) with admired accounts
(80% averaged across @paulg, @naval, @sama, @garrytan, @vkhosla).

## Pipeline (one-time setup, then ongoing)

```bash
# One-time, run again to refresh:
python3 ~/bin/x-corpus-collect.py            # Pulls own + admired corpora
python3 ~/bin/x-style-profile.py             # Compiles X_VOICE.md

# Per draft:
python3 ~/bin/x-draft.py new   "<raw thought>"        [--style <handle>]
python3 ~/bin/x-draft.py reply <tweet-url> "<raw>"    [--style <handle>]
python3 ~/bin/x-draft.py quote <tweet-url> "<raw>"    [--style <handle>]

# After a draft, exactly one of:
python3 ~/bin/x-approve.py approve
python3 ~/bin/x-approve.py edit "<the edited tweet text>"
python3 ~/bin/x-approve.py reject
```

The pending draft persists at `~/.brain-bootstrap-raw/state/x-draft-pending.json`
between the draft call and the approve/edit/reject call.

## Style overrides

The `--style <handle>` flag shifts the blend:

- `--style garrytan` → 60% @garrytan, 20% @antriksssh, 20% averaged across the others
- `--style antriksh` (or `self`, `me`) → 100% own voice, no admired blend

Recognized handles: `antriksssh`, `paulg`, `naval`, `sama`, `garrytan`, `vkhosla`.

User phrases that map to overrides:
- "more garry style" / "in garry tan style" → `--style garrytan`
- "like naval would say it" / "naval style" → `--style naval`
- "more sama" → `--style sama`
- "pure antriksh" / "sound like me" → `--style antriksh`

## 280-char enforcement

The script counts chars and retries once if over. If still over after
retry, returns the long draft with `char_limit_ok: false` so you can
warn the user and they can tighten manually.

## Approval feedback loop (this is what makes the system compound)

| Action | What happens |
|---|---|
| `approve` | Draft appended to `corpus/x-antriksssh.md` under "## Approved drafts". Future drafts retrieve it as exemplar. |
| `edit: <text>` | BOTH the original draft AND the edited version are logged. The edited version goes into the corpus. The diff is the highest-signal training data we have — it tells the model what correction was needed. |
| `reject` | Logged as a negative example. Future profile recompute can use these to extract "avoid these patterns". |

All actions append to `~/.brain-bootstrap-raw/state/x-draft-feedback.jsonl`
for later analysis or training-pass reuse.

## Telegram reply format

Relay the script's `user_message` field verbatim. It's already formatted
for Telegram with reply prompts ("approve · edit: <text> · reject").

DO NOT add: token costs, processing time, character budget remaining,
which exemplars were retrieved. User-facing surface is draft → approval, period.

## Refreshing the voice profile

When ~10+ approved/edited drafts have accumulated since the last profile
compute, re-run:

```bash
python3 ~/bin/x-style-profile.py
```

This re-distills X_VOICE.md so the fingerprint stays current with how the
voice is evolving. Defer auto-cron until the manual cadence proves out.
