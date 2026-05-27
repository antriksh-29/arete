#!/usr/bin/env python3
# Image OCR / understanding via gpt-5.4-mini vision.
#
# Use cases:
#   - "What does this say?" / "Read the text in this image"
#   - "Extract the data from this screenshot"
#   - "What's in this picture?"
#   - "Translate the text in this image"
#
# Usage:
#   image-ocr.py <image-path-or-url> [optional question/instruction]
#
# Default behavior (no question): full text extraction + 1-line summary.
# With a question: answer it grounded in the image content.
#
# Reads OPENAI_API_KEY (and ANTHROPIC fallback) via shared infra/llm.py.

import base64
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, "${STATE_HOME}/bin")
import llm  # type: ignore


def log(msg):
    print(f"[image-ocr] {msg}", file=sys.stderr)


def load_image(src):
    """Return (image_url, image_base64) — pass at most one to llm.call."""
    if src.startswith(("http://", "https://")):
        return src, None
    p = Path(src).expanduser()
    if not p.exists():
        sys.exit(f"ABORT: file not found: {src}")
    data = p.read_bytes()
    return None, base64.b64encode(data).decode()


SYSTEM_PROMPT_DEFAULT = """You are extracting and analyzing the content of an image for Antriksh.

If the image contains text (screenshot, document, menu, sign, handwriting), output it in this structure:

## Extracted text
[full text from the image, preserving line breaks where they matter]

## Summary
[one or two sentences on what this image is and what the text says]

If the image is mostly visual (photo, diagram, chart), skip the text section and write:

## What I see
[2-4 sentences describing the visual content. If it's a chart or diagram, describe the data + the takeaway.]

If there's BOTH meaningful visual content AND text (e.g. an annotated screenshot, a menu with photos), include both sections.

Be concrete and specific. Don't invent details that aren't in the image. If something is illegible or unclear, say so."""

SYSTEM_PROMPT_QUESTION = """You are answering a question about the content of an image for Antriksh.

Look at the image carefully and answer the question directly. Quote text from the image when relevant. If the question can't be answered from the image alone (the answer isn't visible), say so explicitly rather than inventing.

Be concrete and tight. No "the image shows..." preamble — just answer."""


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: image-ocr.py <image-path-or-url> [question]")
    src = sys.argv[1]
    if src == "--smoke":
        # Cold-boot callability check used by eval/run-skill-eval.mjs.
        # No image, no LLM, no network — just prove the script runs end
        # to end and emits a well-formed envelope on stdout.
        print(json.dumps({"ok": True, "provider": "smoke", "user_message": "image-ocr smoke ok"}))
        return
    question = " ".join(sys.argv[2:]).strip()

    image_url, image_base64 = load_image(src)
    log(f"src={src} mode={'question' if question else 'default'}")

    if question:
        system = SYSTEM_PROMPT_QUESTION
        user = f"Question: {question}"
    else:
        system = SYSTEM_PROMPT_DEFAULT
        user = "Analyze this image."

    try:
        text, provider = llm.call(
            system_prompt=system,
            user_msg=user,
            tier="vision",
            max_tokens=2000,
            image_url=image_url,
            image_base64=image_base64,
        )
    except Exception as e:
        log(f"FAILED: {e}")
        print(json.dumps({
            "ok": False,
            "reason": "error",
            "error": str(e),
            "user_message": "⚠️ Couldn't process the image. Both gpt-5.4-mini and gpt-5.4 vision attempts failed.",
        }))
        sys.exit(1)

    print(json.dumps({
        "ok": True,
        "provider": provider,
        "result": text,
        "user_message": text,
    }))


if __name__ == "__main__":
    main()
