---
name: sherpa-onnx-tts
version: 1
script: null
mutating: true
cron: null
eval: openclaw/skills/sherpa-onnx-tts/eval.yaml
last_audited: 2026-05-16
description: |
  Generate speech from text locally using the sherpa-onnx offline TTS engine.
  No cloud required. Outputs WAV files. Supports multiple voice models (Piper).
  WHEN: "read this text aloud", "generate speech from this paragraph",
  "create a TTS audio file", "speak this message offline".
  NOT WHEN: user wants cloud TTS (e.g. OpenAI TTS API); user wants to
  transcribe speech to text (use openai-whisper instead).
trigger: |
  - "read this text aloud / generate speech"
  - "create a TTS audio file from <text>"
  - "speak this message offline"
  - "use sherpa-onnx to synthesize voice"
  - "convert this paragraph to speech"
writes_to: []
brain_dirs_read: []
mutates_external_state: true
upstream: openclaw/openclaw@v2026.5.12 skills/sherpa-onnx-tts
---

# sherpa-onnx-tts

Local TTS using the sherpa-onnx offline CLI.

## Install

1. Download the runtime for your OS (extracts into `$OPENCLAW_STATE_DIR/tools/sherpa-onnx-tts/runtime`, default `~/.openclaw/tools/sherpa-onnx-tts/runtime`)
2. Download a voice model (extracts into `$OPENCLAW_STATE_DIR/tools/sherpa-onnx-tts/models`, default `~/.openclaw/tools/sherpa-onnx-tts/models`)

Resolve the active state directory first:

```bash
STATE_DIR="${OPENCLAW_STATE_DIR:-$HOME/.openclaw}"
```

Then write those resolved paths into the active OpenClaw config file (`$OPENCLAW_CONFIG_PATH`, default `~/.openclaw/openclaw.json`):

```json5
{
  skills: {
    entries: {
      "sherpa-onnx-tts": {
        env: {
          SHERPA_ONNX_RUNTIME_DIR: "/path/to/your/state-dir/tools/sherpa-onnx-tts/runtime",
          SHERPA_ONNX_MODEL_DIR: "/path/to/your/state-dir/tools/sherpa-onnx-tts/models/vits-piper-en_US-lessac-high",
        },
      },
    },
  },
}
```

The wrapper lives in this skill folder. Run it directly, or add the wrapper to PATH:

```bash
export PATH="{baseDir}/bin:$PATH"
```

## Usage

```bash
{baseDir}/bin/sherpa-onnx-tts -o ./tts.wav "Hello from local TTS."
```

Notes:

- Pick a different model from the sherpa-onnx `tts-models` release if you want another voice.
- If the model dir has multiple `.onnx` files, set `SHERPA_ONNX_MODEL_FILE` or pass `--model-file`.
- You can also pass `--tokens-file` or `--data-dir` to override the defaults.
- Windows: run `node {baseDir}\\bin\\sherpa-onnx-tts -o tts.wav "Hello from local TTS."`
