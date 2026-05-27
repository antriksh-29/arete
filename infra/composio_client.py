#!/usr/bin/env python3
"""composio_client.py — thin Python client for Composio's MCP endpoint.

Replaces the prior ClawVisor HTTP gateway pattern. Composio exposes 500+
app integrations through a single MCP endpoint at
https://connect.composio.dev/mcp. Auth is one x-consumer-api-key header
sourced from the arete-cred vault (COMPOSIO_API_KEY).

The MCP surface uses a meta-tool pattern: only 7 management tools are
listed directly (COMPOSIO_MULTI_EXECUTE_TOOL, COMPOSIO_SEARCH_TOOLS,
COMPOSIO_GET_TOOL_SCHEMAS, etc.). App tools (GMAIL_FETCH_EMAILS,
GOOGLECALENDAR_EVENTS_LIST, ...) are invoked through
COMPOSIO_MULTI_EXECUTE_TOOL by slug.

Response is SSE-framed (`event: message\\ndata: <json>\\n`). This module
parses the SSE wrapper and unwraps the nested
result.content[0].text → JSON payload that Composio packs inside.

Multi-account: pass `account="<alias>"` (e.g. "work", "personal") or
`account="<connection_id>"` (e.g. "gmail_tekiah-spiel"). Omit for the
default-connected account.

Usage:
    from composio_client import gmail_search, calendar_list_events
    msgs = gmail_search("is:unread newer_than:1d", account="work", max_results=50)
    events = calendar_list_events(account="personal",
                                  time_min="2026-05-16T00:00:00Z",
                                  time_max="2026-05-17T23:59:59Z",
                                  max_results=25)
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any

MCP_URL = "https://connect.composio.dev/mcp"
DEFAULT_TIMEOUT_S = 60


def _vault_get(name: str) -> str:
    try:
        r = subprocess.run(
            ["/usr/local/bin/arete-cred", "get", name],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _log(msg: str) -> None:
    print(f"[composio] {msg}", file=sys.stderr)


def _parse_sse(raw: str) -> dict[str, Any] | None:
    """Pull the first `data: {...}` line out of an SSE payload."""
    for line in raw.splitlines():
        if line.startswith("data: "):
            try:
                return json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                continue
    return None


_API_KEY_CACHE: str | None = None


def _api_key() -> str:
    global _API_KEY_CACHE
    if _API_KEY_CACHE:
        return _API_KEY_CACHE
    k = _vault_get("COMPOSIO_API_KEY")
    if not k:
        raise RuntimeError("COMPOSIO_API_KEY missing from vault")
    _API_KEY_CACHE = k
    return k


def _rpc(method: str, params: dict[str, Any], timeout: int = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """Send a JSON-RPC 2.0 request to the composio MCP endpoint and return
    the parsed `result` field. Raises RuntimeError on transport errors."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    req = urllib.request.Request(
        MCP_URL,
        data=body.encode(),
        headers={
            "x-consumer-api-key": _api_key(),
            "Content-Type": "application/json",
            "Accept": "application/json,text/event-stream",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:400]
        raise RuntimeError(f"composio HTTP {e.code}: {body}") from None
    except Exception as e:
        raise RuntimeError(f"composio transport error: {e}") from None
    payload = _parse_sse(raw)
    if payload is None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError(f"composio response not parseable: {raw[:200]}") from None
    if "error" in payload:
        raise RuntimeError(f"composio rpc error: {payload['error']}")
    return payload.get("result", {})


def execute(tool_slug: str, arguments: dict[str, Any], account: str | None = None,
            thought: str = "scripted call from infra/", timeout: int = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """Run a single Composio app tool via COMPOSIO_MULTI_EXECUTE_TOOL.

    Returns the unwrapped `data` payload from the tool's response, or
    raises RuntimeError if Composio reports an error.
    """
    tool_item: dict[str, Any] = {"tool_slug": tool_slug, "arguments": arguments}
    if account:
        tool_item["account"] = account
    rpc_params = {
        "name": "COMPOSIO_MULTI_EXECUTE_TOOL",
        "arguments": {
            "tools": [tool_item],
            "thought": thought,
            "sync_response_to_workbench": False,
            "current_step": tool_slug,
        },
    }
    result = _rpc("tools/call", rpc_params, timeout=timeout)
    # result.content[0].text is a JSON string Composio wraps around the actual response
    content = result.get("content") or []
    if not content:
        raise RuntimeError(f"composio {tool_slug}: empty content envelope")
    try:
        inner = json.loads(content[0].get("text", "{}"))
    except json.JSONDecodeError:
        raise RuntimeError(f"composio {tool_slug}: non-JSON content: {content[0].get('text','')[:200]}")
    # MULTI_EXECUTE returns results keyed under "data.results" (one entry per
    # tool). Each entry has shape:
    #   {"response": {"successful": bool, "data": {...}, "error": "..."},
    #    "tool_slug": "...", "index": N}
    data = inner.get("data") or inner
    results = data.get("results") if isinstance(data, dict) else None
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            resp = first.get("response") or {}
            if isinstance(resp, dict):
                if resp.get("successful") is False:
                    raise RuntimeError(
                        f"composio {tool_slug}: "
                        f"{resp.get('error') or resp.get('message') or resp}"
                    )
                return resp.get("data") or {}
            # Older shape — data nested directly under first
            if first.get("successful") is False:
                raise RuntimeError(f"composio {tool_slug}: {first.get('error') or first}")
            return first.get("data") or first
    # Single-tool fallback: return data as-is
    return data if isinstance(data, dict) else {"raw": data}


# ── Convenience wrappers ──────────────────────────────────────────────


def gmail_search(query: str, account: str | None = None, max_results: int = 20,
                 verbose: bool = True) -> list[dict[str, Any]]:
    """Fetch Gmail messages matching `query`. Returns a list of message
    dicts. Composio's GMAIL_FETCH_EMAILS shape per message:
      messageId, threadId, subject, sender (full "Name <email>"), to,
      messageTimestamp (ISO UTC), preview.{body,subject},
      messageText (full body, when verbose=True), labelIds[], display_url,
      attachmentList[].
    """
    args = {
        "query": query,
        "user_id": "me",
        "verbose": verbose,
        "max_results": max_results,
    }
    data = execute("GMAIL_FETCH_EMAILS", args, account=account)
    return data.get("messages") or []


def gmail_get_message(message_id: str, account: str | None = None) -> dict[str, Any]:
    args = {"message_id": message_id, "user_id": "me", "format": "full"}
    return execute("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", args, account=account)


def calendar_list_events(account: str | None = None, time_min: str | None = None,
                         time_max: str | None = None, max_results: int = 25,
                         calendar_id: str = "primary",
                         time_zone: str = "Asia/Kolkata") -> list[dict[str, Any]]:
    """List calendar events between time_min and time_max (RFC3339, UTC or
    with offset). Returns a list of event dicts (Composio's
    GOOGLECALENDAR_EVENTS_LIST `items` array). Fields per event: id,
    summary, start{dateTime|date,timeZone}, end{...}, attendees[],
    location, htmlLink, description, hangoutLink, status.

    Composio warns if time_min/time_max are UTC-only but the calendar's
    timezone is regional — we pass timeZone explicitly to anchor.
    """
    args: dict[str, Any] = {
        "calendar_id": calendar_id,
        "single_events": True,
        "order_by": "startTime",
        "max_results": max_results,
        "timeZone": time_zone,
    }
    if time_min:
        args["timeMin"] = time_min
    if time_max:
        args["timeMax"] = time_max
    data = execute("GOOGLECALENDAR_EVENTS_LIST", args, account=account)
    return data.get("items") or []


def calendar_get_event(event_id: str, account: str | None = None,
                       calendar_id: str = "primary") -> dict[str, Any]:
    args = {"calendar_id": calendar_id, "event_id": event_id}
    return execute("GOOGLECALENDAR_EVENTS_GET", args, account=account)


# ── CLI shim for shell callers ────────────────────────────────────────


def _cli() -> int:
    """Small CLI: composio_client.py <tool_slug> <args-json> [--account <alias>]"""
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--smoke":
        print(json.dumps({"ok": True, "skill": "composio_client", "user_message": "composio_client smoke ok"}))
        return 0
    if len(args) < 2:
        print("Usage: composio_client.py <TOOL_SLUG> '<args-json>' [--account <alias>]", file=sys.stderr)
        return 2
    tool_slug = args[0]
    try:
        tool_args = json.loads(args[1])
    except json.JSONDecodeError as e:
        print(f"args JSON parse error: {e}", file=sys.stderr)
        return 2
    account = None
    if "--account" in args:
        i = args.index("--account")
        if i + 1 < len(args):
            account = args[i + 1]
    try:
        out = execute(tool_slug, tool_args, account=account)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
