---
name: voice-call
version: 1
script: null
mutating: true
cron: null
eval: openclaw/skills/voice-call/eval.yaml
last_audited: 2026-05-16
description: |
  Initiate and manage voice calls via the OpenClaw voice-call plugin. Supports
  Twilio, Telnyx, Plivo, and mock providers. Can start calls, speak messages,
  check call status, and end calls programmatically.
  WHEN: "call +1555... and say X", "start a voice call to this number",
  "speak a message to someone by phone", "check the status of call <id>".
  NOT WHEN: user wants to send a text/SMS; user wants TTS audio file without
  a call (use sherpa-onnx-tts instead).
trigger: |
  - "call <phone number> and say <message>"
  - "start a voice call to <number>"
  - "initiate a phone call with message <text>"
  - "check status of call <id>"
  - "end call <id>"
  - "speak to the user via phone"
writes_to: []
brain_dirs_read: []
mutates_external_state: true
upstream: openclaw/openclaw@v2026.5.12 skills/voice-call
---

# Voice Call

Use the voice-call plugin to start or inspect calls (Twilio, Telnyx, Plivo, or mock).

## CLI

```bash
openclaw voicecall call --to "+15555550123" --message "Hello from OpenClaw"
openclaw voicecall status --call-id <id>
```

## Tool

Use `voice_call` for agent-initiated calls.

Actions:

- `initiate_call` (message, to?, mode?)
- `continue_call` (callId, message)
- `speak_to_user` (callId, message)
- `end_call` (callId)
- `get_status` (callId)

Notes:

- Requires the voice-call plugin to be enabled.
- Plugin config lives under `plugins.entries.voice-call.config`.
- Twilio config: `provider: "twilio"` + `twilio.accountSid/authToken` + `fromNumber`.
- Telnyx config: `provider: "telnyx"` + `telnyx.apiKey/connectionId` + `fromNumber`.
- Plivo config: `provider: "plivo"` + `plivo.authId/authToken` + `fromNumber`.
- Dev fallback: `provider: "mock"` (no network).
