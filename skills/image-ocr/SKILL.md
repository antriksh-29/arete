---
name: image-ocr
version: 1
script: infra/image-ocr.py
mutating: false
cron: null
eval: skills/image-ocr/eval.yaml
last_audited: 2026-05-08
description: |
  Vision-driven extraction skill. The user shares an image and either asks
  what it says, asks to extract structured data from it, or asks a direct
  question grounded in the image content.
  WHEN: Telegram image attachment + intent words like "what does this say",
  "read this", "OCR this", "extract the text", "translate this", or any
  question whose answer is in the image ("what's the price", "what time does
  it open"). Also fires when only an image is sent with no caption — assume
  default extraction mode.
  NOT WHEN: User wants to save the image for later (→ reading-list), absorb
  a screenshotted PDF page (→ ingest-pdf if it's actually a PDF), or analyze
  a tweet screenshot they could just send the URL of (→ ingest-x-thread).
trigger: |
  Image attachment on Telegram + any of:
  - "what does this say"
  - "read this", "OCR this"
  - "extract the text", "extract the data"
  - "what's in this picture/image/screenshot"
  - "translate this"
  - Or a question about the image ("what's the price", "what time does it open")
writes_to: []
brain_dirs_read: []
trigger_guard: image_attached
---

# /image-ocr

Process an image (Telegram attachment or URL) using gpt-5.4-mini vision.
Falls back to gpt-5.4 vision (per `identity/MODEL_ROUTING.md` vision tier).

## How to invoke

When the user shares an image with any trigger phrase OR a question
about its content, the agent should:

1. Get the image onto disk (Telegram getFile API → file_path → download
   to /tmp/), OR pass the URL directly if the message contains one.
2. Run:
   ```bash
   python3 ~/bin/image-ocr.py <path-or-url> [optional user question]
   ```
3. Relay the script's `user_message` field verbatim to Telegram.

## Two modes

**Default mode (no question):** structured extraction.
- If the image has text → "## Extracted text" + "## Summary" sections.
- If the image is mostly visual → "## What I see" section.
- If both → both sections.

**Question mode (user asked something specific):** direct answer grounded
in the image content. The script doesn't pad with "the image shows..." —
just answers what was asked.

## Result envelope (stdout JSON)

Success:
```json
{
  "ok": true,
  "provider": "openai",          // or "openai" if fallback to gpt-5.4
  "result": "...",
  "user_message": "..."
}
```

Failure: relay `user_message` verbatim. Both vision providers attempted
before this returns.

## Architectural notes

- Script uses shared `~/bin/llm.py` with vision tier:
  primary `openai/gpt-5.4-mini`, fallback `openai/gpt-5.4` (Anthropic
  vision via Haiku is supported by the shared module if we ever switch
  the fallback chain — currently both vision tiers stay on OpenAI).
- The agent (running on gpt-5.4-mini in the main loop) MAY also see the
  image natively if openclaw passes it through. But this dedicated
  script gives a deterministic structured-extraction path that's easier
  to reason about and easier to test.
