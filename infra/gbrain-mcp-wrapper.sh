#!/bin/bash
# Wrapper for gbrain serve as OpenClaw MCP child.
# OpenClaw strips env when spawning MCP children, so gbrain serve loses
# OPENAI_API_KEY (cant embed queries) and bun (gbrains shebang). Inject both.
export PATH="${STATE_HOME}/.bun/bin:$PATH"
export OPENAI_API_KEY="$(/usr/local/bin/arete-cred get OPENAI_API_KEY 2>/dev/null)"
export ANTHROPIC_API_KEY="$(/usr/local/bin/arete-cred get ANTHROPIC_API_KEY 2>/dev/null)"
exec ${STATE_HOME}/.bun/bin/gbrain serve "$@"
