---
name: openai-whisper
version: 1
script: null
mutating: true
cron: null
eval: openclaw/skills/openai-whisper/eval.yaml
last_audited: 2026-05-16
description: |
  Transcribe audio files locally using the OpenAI Whisper CLI (no API key
  required). Supports multiple output formats (txt, srt, vtt) and translation.
  Models download automatically on first run.
  WHEN: "transcribe this audio file", "convert this recording to text",
  "generate subtitles for this mp3", "translate this audio to English".
  NOT WHEN: user has an OpenAI API key and prefers cloud transcription
  (use openai-whisper-api instead); video files needing audio extraction first.
trigger: |
  - "transcribe this audio file locally"
  - "convert this recording / mp3 / m4a to text"
  - "generate SRT subtitles for <audio>"
  - "translate this audio to English"
  - "run whisper on <file>"
  - "transcribe without sending to the cloud"
writes_to: []
brain_dirs_read: []
mutates_external_state: true
upstream: openclaw/openclaw@v2026.5.12 skills/openai-whisper
---

# Whisper (CLI)

Use `whisper` to transcribe audio locally.

Quick start

- `whisper /path/audio.mp3 --model medium --output_format txt --output_dir .`
- `whisper /path/audio.m4a --task translate --output_format srt`

Notes

- Models download to `~/.cache/whisper` on first run.
- `--model` defaults to `turbo` on this install.
- Use smaller models for speed, larger for accuracy.
