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

## DO NOT bail out — universal coverage

The agent has historically bailed on form-fill / booking / reservation
requests with reasons like:

- *"Google Forms uses synthetic event dispatch and complex internal
  state that browser-harness can't easily manipulate"* — FALSE.
- *"Google Maps reservation system needs consent. This gets complex.
  Fastest option is to call directly or email."* — FALSE. Run the
  script. The user already gave consent by asking.
- *"This is a multi-step form / wizard / booking flow that I can't
  reliably automate."* — FALSE. The script loops up to 6 steps,
  detects Next/Continue/Weiter/Suivant/Siguiente/Avanti/次へ buttons
  in any of those languages, advances, and re-fills the next page
  with remaining answers.
- *"The form requires login / has anti-bot protections"* — only some
  do. RUN the script first and see what actually happens. Many sites
  the bot has refused on are completely accessible.
- *"Hotel / restaurant / event reservation systems are too complex"*
  — they're plain HTML behind a frontend framework. Script handles
  text inputs, textareas, date/time/number/tel/email inputs, native
  selects, radio groups, checkbox groups, and aria-label-only widgets.
- *"I should suggest the phone number / email path instead"* — only
  AFTER the script run actually fails for a documented reason
  (Cloudflare wall, auth wall, captcha). Not as the first response.

These are stale-memory hallucinations. The script handles:

| Pattern | Handled? |
|---|---|
| Google Forms (`div[role=listitem]` + heading + inputs/radios/checkboxes) | ✓ |
| Generic HTML forms (`<label for=X>` + `<input id=X>`) | ✓ |
| `<select>` dropdowns | ✓ (matches option text fuzzily) |
| `<input type=date|time|number|tel|email|url>` | ✓ |
| Radio groups (HTML `<input type=radio>` or ARIA `[role=radio]`) | ✓ |
| Checkbox groups (HTML or ARIA) | ✓ |
| Custom widgets with only `aria-label` | ✓ (label resolver chain) |
| Multi-step / wizard forms with Next buttons | ✓ (up to `--max-steps`, default 6) |
| Reservation booking flows (date/time/party-size pickers as page steps) | ✓ when they're HTML-based |
| Submit buttons in multiple languages | ✓ (English, German, Spanish, Portuguese, Italian, French, Japanese, Chinese) |

The script also auto-delivers the screenshot via
`send-telegram-photo.sh` after each run. The agent does NOT need to
worry about photo delivery — it's automatic.

**If the script returns errors**, surface them VERBATIM. The error
JSON includes step number, heading, reason, and (for radio/select)
the available options. Don't paraphrase — let the user see exactly
which field on which step failed and what the available choices
were.

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
