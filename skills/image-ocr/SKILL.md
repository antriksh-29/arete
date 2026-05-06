---
name: image-ocr
description: Read text in images, extract data from screenshots, answer questions about images via gpt-5.4-mini vision (fallback gpt-5.4 vision).
trigger: |
  Image attachment on Telegram + any of:
  - "what does this say"
  - "read this", "OCR this"
  - "extract the text", "extract the data"
  - "what's in this picture/image/screenshot"
  - "translate this"
  - Or a question about the image ("what's the price", "what time does it open")
mutating: false
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
