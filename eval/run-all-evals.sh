#!/usr/bin/env bash
# Single CI gate. Runs every audit + eval in this repo and exits non-zero
# if any one fails. Designed to be wired into a pre-push hook (Wave 4) or
# called manually before landing changes.
#
# Usage: bash eval/run-all-evals.sh
# Optional: --no-llm   (skip steps that need OPENAI_API_KEY)

set -uo pipefail
cd "$(dirname "$0")/.."

NO_LLM=0
for arg in "$@"; do
  case "$arg" in
    --no-llm) NO_LLM=1 ;;
  esac
done

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

# ── 3. per-skill evals (smoke + trigger routing) ──
for d in skills/*/; do
  [ -f "${d}SKILL.md" ] || continue
  [ -f "${d}eval.yaml" ] || { echo "(skip ${d} — no eval.yaml)"; continue; }
  step "skill-eval: $(basename "$d")" node eval/run-skill-eval.mjs "$d"
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
