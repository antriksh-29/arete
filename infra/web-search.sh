#!/bin/bash
# Web search via OpenAI GPT-5.4-mini + native web_search_preview tool.
# Cheap-tier per eng plan §14.1 model matrix. Mini handles both the search
# and a tight synthesis pass; output is markdown with inline source links.
#
# Usage:  web-search.sh "<query>"
# Optional:  WEB_SEARCH_CONTEXT_SIZE=high  (low|medium|high — default medium)
#            WEB_SEARCH_MAX_TOKENS=2000     (response cap)
#
# Reads OPENAI_API_KEY from the bot-cred vault.
# Output: synthesized markdown to stdout. Exit 0 on success, 1 otherwise.

set -e

# --smoke: cheap health probe for skill eval. Does not call OpenAI.
if [ "${1:-}" = "--smoke" ]; then
  echo '{"ok": true, "skill": "web-search", "user_message": "web-search smoke ok"}'
  exit 0
fi

QUERY="$*"
if [ -z "$QUERY" ]; then
  echo "Usage: web-search.sh \"<query>\"" >&2
  exit 1
fi

OPENAI_API_KEY="$(/usr/local/bin/bot-cred get OPENAI_API_KEY 2>/dev/null)"
if [ -z "$OPENAI_API_KEY" ]; then
  echo "ABORT: OPENAI_API_KEY missing from vault" >&2
  exit 1
fi

CONTEXT_SIZE="${WEB_SEARCH_CONTEXT_SIZE:-medium}"
MAX_TOKENS="${WEB_SEARCH_MAX_TOKENS:-2000}"
MODEL="${WEB_SEARCH_MODEL:-gpt-5.4-mini}"

# Tight synthesis instruction: encourages the model to cite, stay concise,
# and not pad. We're using mini's cheap-tier — wasted tokens here recur for
# every search.
SYSTEM_INSTR='You are a research assistant. Search the web for the user query, then return a tight markdown summary. Rules: lead with the key finding (1-2 sentences), then bullet the most important results (max 5), each bullet ≤ 30 words and ending with the source URL in parentheses. Do not hedge. Do not pad. Do not invent — if a result is uncertain, say so. Output markdown only — no preamble, no apology, no "I searched the web" type filler.'

# OpenAI Responses API call. The web_search_preview tool is built-in.
# Pin response_format to text since we are returning markdown for downstream
# consumption (the agent will paste this into Telegram).
RESP=$(curl -sf https://api.openai.com/v1/responses \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg model "$MODEL" --arg q "$QUERY" --arg sys "$SYSTEM_INSTR" --arg cs "$CONTEXT_SIZE" --argjson mt "$MAX_TOKENS" '{
    model: $model,
    input: $q,
    instructions: $sys,
    tools: [{ type: "web_search_preview", search_context_size: $cs }],
    max_output_tokens: $mt
  }')")

if [ -z "$RESP" ]; then
  echo "ABORT: empty response from OpenAI" >&2
  exit 1
fi

# Extract the assistant text from the responses API output.
# Output shape: { output: [ { type: "message", content: [ { type: "output_text", text: "..." } ] }, ... ] }
echo "$RESP" | jq -r '
  .output[]?
  | select(.type == "message")
  | .content[]?
  | select(.type == "output_text")
  | .text
' 2>/dev/null

# If extraction returned nothing, dump the raw response for debugging.
if [ -z "$(echo "$RESP" | jq -r '.output[]? | select(.type=="message") | .content[]? | select(.type=="output_text") | .text' 2>/dev/null)" ]; then
  echo "[web-search.sh] could not extract text; raw response below:" >&2
  echo "$RESP" | head -c 2000 >&2
  exit 1
fi
