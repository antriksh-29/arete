---
name: fill-form
version: 1
script: infra/fill-form.py
mutating: true
mutates_external_state: true
cron: null
eval: skills/fill-form/eval.yaml
last_audited: 2026-05-12
description: |
  Fill out an HTML/Google form via browser-harness using user-provided
  answers. Handles Google Forms (the `div[role=listitem]` structure)
  plus generic HTML forms with `<input>` / `<textarea>` / radios /
  checkboxes. Matches answer keys to question labels fuzzily, fills
  via click_at_xy + type_text, screenshots, optionally submits.
  WHEN: "fill this form: <url> with <answers>" / "fill out <url> for
  me" / "submit this form on my behalf" / "register me at <url>" /
  any request that combines a form URL + a list of answers.
  NOT WHEN: User wants to just view what a form has (→ /agent-browser
  for navigation). NOT WHEN there's no answers — agent should ask
  the user for the data first.
trigger: |
  - "fill this form" / "fill out this form" / "fill this form for me"
  - "fill the google form <url>"
  - "submit this form on my behalf" / "submit the form"
  - "register me at <url>" / "apply for <X> at <url>"
  - "answer this form with <data>"
writes_to: []
brain_dirs_read: []
trigger_guard: any_url
---

# /fill-form

Generic form-filler driven by browser-harness. Designed specifically
for the failure mode where the agent bails out claiming "Google Forms
are too complex" / "I'd need to reverse-engineer entry.X mapping" —
those are NOT real obstacles. Google Forms is plain HTML in a
`div[role=listitem]` per question. Browser-harness can click and
type. This script does that.

## Agent script of record

For any user message of the form "fill <url> with <answers>":

1. Parse the user's answers into a JSON map: `{question label or
   keyword: answer value}`. Example:
   ```json
   {
     "Full Name": "Antriksh Jain",
     "Email": "antriksh1980@gmail.com",
     "Applying as": "Individual Developer",
     "GitHub": "https://github.com/antriksh-29",
     "What are you building": "A single-tenant personal AI assistant…",
     "MCP servers": "Dineout, Instamart, Food",
     "I acknowledge": "Yes"
   }
   ```
   Keys are matched FUZZILY against form labels (lowercase, punctuation
   stripped, then exact/containment/token-overlap match) — so the user
   doesn't need to copy form labels verbatim. "Email" matches
   "Your Email Address". "What are you building" matches
   "What are you building? (Describe in 2-3 sentences)".

2. Write the JSON to `/tmp/fill-form-answers-<timestamp>.json`.

3. Run:
   ```bash
   python3 ~/bin/fill-form.py "<url>" /tmp/fill-form-answers-<ts>.json
   ```
   This fills the form WITHOUT submitting and writes a screenshot to
   `/tmp/fill-form-result.png`. Agent's reply quotes the envelope's
   `user_message` field + lists which fields were filled.

4. If the user explicitly said "submit it" / "go ahead and submit" /
   "i'm okay with this, submit on my behalf", re-run with `--submit`:
   ```bash
   python3 ~/bin/fill-form.py "<url>" /tmp/fill-form-answers-<ts>.json --submit
   ```

## DO NOT bail out

The agent has historically bailed on form-fill requests with reasons
like:

- *"Google Forms uses synthetic event dispatch and complex internal
  state that browser-harness can't easily manipulate"* — FALSE. Click
  and type. That's it.
- *"Google Forms doesn't support URL-based prefill"* — irrelevant; we
  don't use prefill, we drive the form interactively.
- *"I'd need to reverse-engineer the entry.X mapping which is
  fragile"* — also irrelevant; we don't use entry IDs, we use the
  visible question labels.
- *"Filling via automated script injection is fragile"* — the script
  is deterministic and validates by reading the input `.value` back
  after fill.

These are stale-memory hallucinations. The script works. RUN IT.
Don't apologize, don't suggest manual fill as the primary answer.

## Output envelope

The script prints a single-line JSON with these fields:

```json
{
  "ok": true,
  "fields_detected": 14,
  "filled": [
    {"question": "Full Name", "matched_key": "Full Name", "mode": "text", "value": "Antriksh Jain"},
    {"question": "Are you applying as…", "matched_key": "Applying as", "mode": "radio", "value": "Individual Developer"},
    ...
  ],
  "skipped_headings": ["..."],
  "submitted": false,
  "verification_state": {
    "texts": ["Antriksh Jain", "antriksh1980@gmail.com", ...],
    "radios": ["Individual Developer"],
    "checks": ["Yes"]
  },
  "screenshot": "/tmp/fill-form-result.png",
  "user_message": "📝 Form fill — 13 fields touched, 1 skipped. ..."
}
```

Use `verification_state` to confirm what actually got set on the page.
If a field was supposed to be filled but doesn't appear in
`verification_state`, surface that to the user.

## Sending the screenshot back

The screenshot lives on the VPS at `/tmp/fill-form-result.png`. The
agent **MUST** send it to the user inline via:

```bash
~/bin/send-telegram-photo.sh /tmp/fill-form-result.png "form filled — review before submit"
```

This is the Telegram Bot API `sendPhoto` endpoint wrapper (falls back
to `sendDocument` for files >10MB). It returns 0 on success and prints
`[<ts>] sent telegram sendPhoto: ...` to stdout — the agent does NOT
need to relay that line; the photo arrives in the user's Telegram
client directly.

DO NOT say "media failed" / "I can't send images" / "the screenshot
is on the VPS" / "you'd need to SCP it" — `send-telegram-photo.sh` IS
the way. If it fails, the script prints the HTTP error to stderr —
surface that to the user verbatim.

## Edge cases

- **Checkboxes with multiple values:** answer can be a list
  `["Dineout", "Instamart"]` or a comma-separated string
  `"Dineout, Instamart"`. Both work.
- **Dropdowns:** treated like radios (option text matched against
  the answer).
- **Required-but-unanswered fields:** listed in `skipped_headings`.
  Tell the user before submit.
- **Login-walled forms:** if the form sits behind auth (private
  Google Form, internal company form), `/site-login <domain>` first.

## Smoke

`fill-form.py --smoke` returns a static envelope. Full e2e requires
bh-chrome running + a real URL; covered by manual Telegram verify
per skill.
