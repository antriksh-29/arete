# Shared helper for skill smoke wrappers.
# Source this from each 50-skill-<name>.sh. NOT a drop-in itself —
# leading underscore tells gbrain's smoke-test loader to ignore it.

# run_smoke <skill-name> <runner> <script-path>
#   runner is "python3" or "bash"
#   script-path is the deployed path on the VPS (~/bin/<x>)
#
# Pass: exit 0, prints "✓ <skill> smoke ok"
# Fail: exit 1, prints "✗ <skill> smoke FAILED: <stdout-or-error>"
run_smoke() {
  local skill="$1"
  local runner="$2"
  local script="$3"

  if [ ! -x "$script" ] && [ ! -f "$script" ]; then
    echo "✗ $skill smoke FAILED: script not found at $script"
    return 1
  fi

  local out
  out=$("$runner" "$script" --smoke 2>&1)
  local rc=$?

  if [ "$rc" -ne 0 ]; then
    echo "✗ $skill smoke FAILED (exit=$rc): ${out:0:120}"
    return 1
  fi

  if ! echo "$out" | grep -q '"ok":[[:space:]]*true'; then
    echo "✗ $skill smoke FAILED (no ok=true): ${out:0:120}"
    return 1
  fi

  echo "✓ $skill smoke ok"
  return 0
}
