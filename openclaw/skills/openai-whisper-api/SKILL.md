---
name: openai-whisper-api
version: 1
script: null
mutating: true
cron: null
eval: openclaw/skills/openai-whisper-api/eval.yaml
last_audited: 2026-05-16
description: |
  Transcribe audio via the OpenAI Audio Transcriptions API (Whisper). Requires
  OPENAI_API_KEY. Supports language hints, speaker prompts, JSON output, and
  OpenAI-compatible proxy/local gateway via OPENAI_BASE_URL.
  WHEN: "transcribe this file via OpenAI API", "use Whisper API on this audio",
  "transcribe with language hint English".
  NOT WHEN: user wants local/offline transcription without API key (use
  openai-whisper CLI skill instead).
trigger: |
  - "transcribe this audio using the OpenAI API"
  - "use Whisper API on <file>"
  - "transcribe with language hint / speaker prompt"
  - "transcribe and output JSON"
  - "send this recording to OpenAI for transcription"
writes_to: []
brain_dirs_read: []
mutates_external_state: true
upstream: openclaw/openclaw@v2026.5.12 skills/openai-whisper-api
---

# OpenAI Whisper API (curl)

Transcribe an audio file via OpenAI's `/v1/audio/transcriptions` endpoint. Set `OPENAI_BASE_URL` to use an OpenAI-compatible proxy or local gateway.

## Quick start

```bash
{baseDir}/scripts/transcribe.sh /path/to/audio.m4a
```

Defaults:

- Model: `whisper-1`
- Output: `<input>.txt`

## Useful flags

```bash
{baseDir}/scripts/transcribe.sh /path/to/audio.ogg --model whisper-1 --out /tmp/transcript.txt
{baseDir}/scripts/transcribe.sh /path/to/audio.m4a --language en
{baseDir}/scripts/transcribe.sh /path/to/audio.m4a --prompt "Speaker names: Peter, Daniel"
{baseDir}/scripts/transcribe.sh /path/to/audio.m4a --json --out /tmp/transcript.json
```

## API key

Set `OPENAI_API_KEY`, or configure it in the active OpenClaw config file (`$OPENCLAW_CONFIG_PATH`, default `~/.openclaw/openclaw.json`). Optionally set `OPENAI_BASE_URL` (for example `http://127.0.0.1:51805/v1`) to use an OpenAI-compatible proxy or local gateway:

```json5
{
  skills: {
    "openai-whisper-api": {
      apiKey: "OPENAI_KEY_HERE",
    },
  },
}
```
