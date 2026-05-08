#!/usr/bin/env bash
# Single CI gate. Runs every audit + eval in this repo and exits non-zero
# if any one fails. Designed to be wired into a pre-push hook (Wave 4) or
# called manually before landing changes.
#
# Usage: bash eval/run-all-evals.sh
# Optional flags:
#   --no-llm   skip every step that needs OPENAI_API_KEY (cheap pre-push)
#   --judge    enable LLM-as-judge scoring on output evals (gpt-5.4-nano).
#              Implies --llm. Use for the deeper weekly verification.

set -uo pipefail
cd "$(dirname "$0")/.."

NO_LLM=0
WITH_JUDGE=0
for arg in "$@"; do
  case "$arg" in
    --no-llm) NO_LLM=1 ;;
    --judge) WITH_JUDGE=1 ;;
  esac
done

if [ "$NO_LLM" -eq 1 ] && [ "$WITH_JUDGE" -eq 1 ]; then
  echo "error: --no-llm and --judge are mutually exclusive" >&2
  exit 2
fi

red=$'\033[31m'; green=$'\033[32m'; yellow=$'\033[33m'; rst=$'\033[0m'
fails=0
ran=0

step() {
  local name="$1"; shift
  echo
  echo "═══ $name ═══"
  ran=$((ran+1))
  if "$@"; then
    echo "${green}✓ $name${rst}"
  else
    echo "${red}✗ $name${rst}"
    fails=$((fails+1))
  fi
}

# ── 1. check-resolvable: every custom skill has SKILL.md, script, eval ──
step "check-resolvable" node infra/check-resolvable.mjs

# ── 2. dry-audit: no exact trigger collisions ──
step "dry-audit" node infra/dry-audit.mjs

# ── 3. per-skill evals (smoke + structural; --judge adds LLM scoring) ──
JUDGE_FLAG=""
[ "$WITH_JUDGE" -eq 1 ] && JUDGE_FLAG="--judge"
for d in skills/*/; do
  [ -f "${d}SKILL.md" ] || continue
  [ -f "${d}eval.yaml" ] || { echo "(skip ${d} — no eval.yaml)"; continue; }
  if [ -n "$JUDGE_FLAG" ]; then
    step "skill-eval: $(basename "$d")" node eval/run-skill-eval.mjs "$d" "$JUDGE_FLAG"
  else
    step "skill-eval: $(basename "$d")" node eval/run-skill-eval.mjs "$d"
  fi
done

# ── 4. retrieval eval (gbrain-backed; needs network + key) ──
if [ "$NO_LLM" -eq 0 ] && [ -n "${OPENAI_API_KEY:-}" ]; then
  step "retrieval-eval" node eval/run-retrieval-eval.mjs
else
  echo
  if [ "$NO_LLM" -eq 1 ]; then
    echo "(skip retrieval-eval — --no-llm flag)"
  else
    echo "(skip retrieval-eval — OPENAI_API_KEY not set)"
  fi
fi

echo
if [ "$fails" -gt 0 ]; then
  echo "${red}=== $fails of $ran step(s) FAILED ===${rst}"
  exit 1
fi
echo "${green}=== all $ran step(s) passed ===${rst}"
