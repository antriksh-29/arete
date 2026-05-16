#!/bin/bash
# composio-connections.sh — authoritative "what's connected via Composio"
# listing. Replaces the agent's prior pattern of guessing which toolkits
# to query (which under-reported Drive/YouTube/Maps and over-reported
# stale-initiated Slack/GitHub).
#
# Why this wrapper exists: Composio's MCP has no "list-all-connections"
# tool. COMPOSIO_MANAGE_CONNECTIONS with action="list" requires per-toolkit
# names; the dashboard's complete list is only available through the
# REST `/api/v3/connected_accounts` endpoint which needs a developer
# API key (ak_*), different from the ck_* consumer key we use for MCP.
#
# Until/unless we add a dev API key, this script queries a curated set
# of toolkits and filters by status=active by default. Add new toolkits
# to TOOLKITS below when you connect them in the dashboard.
#
# Usage:
#   composio-connections.sh                # active connections only (markdown)
#   composio-connections.sh --all          # include initiated/pending
#   composio-connections.sh --json         # machine-readable
#   composio-connections.sh --smoke        # skill eval probe

set -uo pipefail

if [ "${1:-}" = "--smoke" ]; then
  echo '{"ok":true,"skill":"composio-connections","user_message":"composio-connections smoke ok"}'
  exit 0
fi

INCLUDE_PENDING=0
AS_JSON=0
for a in "$@"; do
  case "$a" in
    --all|--include-pending) INCLUDE_PENDING=1 ;;
    --json) AS_JSON=1 ;;
  esac
done

# Curated toolkit list — ONLY toolkits we know the user has connected.
#
# CRITICAL: Composio's COMPOSIO_MANAGE_CONNECTIONS with action="list" has
# a documented "no side effects" contract, but empirically calling it on
# a toolkit the user has NEVER connected creates a stale "initiated"
# state at Composio's end. Listing 25 speculative toolkits on 2026-05-16
# polluted the account with 17 such phantom-initiated rows that don't
# show in the dashboard but are returned by the MCP forever.
#
# So: only list toolkits the user has actually connected (per the
# Composio dashboard). When the user connects a new app, append it here.
# To detect newly-connected toolkits, use the dashboard directly OR a
# REST API call against /api/v3/connected_accounts (requires a developer
# ak_* key, not the ck_* consumer key we use for MCP).
TOOLKITS=(
  gmail googlecalendar notion googlesheets googledrive youtube linkedin googlemaps
)

# Build the JSON array of toolkit-list requests.
TOOLKITS_JSON=$(printf '{"name":"%s","action":"list"},' "${TOOLKITS[@]}")
TOOLKITS_JSON="[${TOOLKITS_JSON%,}]"

KEY=$(bot-cred get COMPOSIO_API_KEY 2>/dev/null)
if [ -z "$KEY" ]; then
  echo "ABORT: COMPOSIO_API_KEY missing from vault" >&2
  exit 1
fi

RESP=$(curl -s --max-time 60 \
  -X POST -H "x-consumer-api-key: $KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json,text/event-stream" \
  "https://connect.composio.dev/mcp" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"COMPOSIO_MANAGE_CONNECTIONS\",\"arguments\":{\"toolkits\":$TOOLKITS_JSON}}}")

INCLUDE_PENDING="$INCLUDE_PENDING" AS_JSON="$AS_JSON" python3 - "$RESP" <<'PYEOF'
import json, os, sys, re, base64

raw = sys.argv[1]
data_line = next((l for l in raw.splitlines() if l.startswith("data: ")), None)
if not data_line:
    print("error: no data line in MCP response", file=sys.stderr)
    sys.exit(1)
envelope = json.loads(data_line[len("data: "):])
inner = json.loads(envelope["result"]["content"][0]["text"])
results = inner.get("data", {}).get("results", {})

include_pending = os.environ.get("INCLUDE_PENDING") == "1"
as_json = os.environ.get("AS_JSON") == "1"

active, pending = [], []
for tk, info in results.items():
    accts = info.get("accounts", []) or []
    if accts:
        # Surface the primary email if discoverable.
        for a in accts:
            email = None
            ui = a.get("user_info", {})
            for it in ui.get("items", []):
                if it.get("primary"):
                    email = it.get("id"); break
            if not email:
                tok = ui.get("nextSyncToken", "")
                if tok:
                    try:
                        dec = base64.b64decode(tok + "==").decode("utf-8", errors="ignore")
                        m = re.search(r"[\w.-]+@[\w.-]+", dec)
                        if m: email = m.group(0)
                    except Exception:
                        pass
            active.append({
                "toolkit": tk,
                "alias": a.get("alias"),
                "id": a.get("id"),
                "default": bool(a.get("is_default")),
                "email": email,
            })
    elif info.get("status") in ("initiated", "pending"):
        pending.append({"toolkit": tk, "status": info["status"]})

if as_json:
    print(json.dumps({"active": active, "pending": pending,
                      "include_pending": include_pending}, indent=2))
    sys.exit(0)

# By-toolkit grouping for the markdown render.
by_tk = {}
for a in active:
    by_tk.setdefault(a["toolkit"], []).append(a)

print("**Composio app connections:**")
print()
if not by_tk:
    print("_No active connections found in the queried toolkit set._")
else:
    for tk, accts in by_tk.items():
        plural = "accounts" if len(accts) > 1 else "account"
        print(f"✅ **{tk}** — {len(accts)} {plural}")
        for a in accts:
            tag = " (default)" if a["default"] else ""
            email = a["email"] or "—"
            alias = a["alias"] or "(no alias)"
            print(f"  • {alias}{tag}: {email}")
        print()

if include_pending and pending:
    print("⏳ **Initiated / pending (OAuth not completed):**")
    for p in pending:
        print(f"  • {p['toolkit']}")
    print()

if not include_pending and pending:
    print(f"_(+{len(pending)} initiated-but-not-completed entries hidden; "
          f"run with `--all` to see them)_")
PYEOF
