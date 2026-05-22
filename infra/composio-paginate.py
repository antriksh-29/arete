#!/usr/bin/env python3
"""composio-paginate.py — fetch a complete paginated Composio response.

The MCP `COMPOSIO_MULTI_EXECUTE_TOOL` truncates large responses to
`data_preview` (first ~3 items) and writes the full payload to a
remote sandbox file the agent can't read back. PR-A0 disabled
`COMPOSIO_REMOTE_WORKBENCH` to save tokens, so the agent has no way
to fetch the sandbox file either. Net effect: any tool result with
>3-5 items gets silently truncated in the agent's context, the
agent only sees a "preview", and any user request that needs the
complete list (subscriptions, calendar history, drive files) gives
an incomplete answer.

This wrapper bypasses MCP entirely:
  • Calls Composio's HTTP MCP endpoint via composio_client.execute(),
    which sets sync_response_to_workbench=False → full data inline
  • Loops on nextPageToken until exhausted (or max-pages hit)
  • Dedupes by a configurable key (default: items[].id)
  • Emits clean JSON to stdout: {"items": [...], "total": N, "pages": P}

The agent invokes this via Bash `exec`, parses stdout, then processes
the data normally (categorize, write brain page, etc.). NO truncation.

Usage:
  composio-paginate.py YOUTUBE_LIST_USER_SUBSCRIPTIONS \\
      --args '{"part": "snippet", "maxResults": 50}' \\
      --max-pages 20

  composio-paginate.py GMAIL_FETCH_EMAILS \\
      --args '{"query": "newer_than:7d", "max_results": 100}' \\
      --account work --max-pages 10

Args:
  TOOL_SLUG          (positional) Composio tool slug.
  --args JSON        Tool arguments as JSON string. Required.
  --account NAME     Optional account alias (e.g. work / personal).
  --max-pages N      Safety cap on pagination loops (default 30).
                     Prevents runaway when nextPageToken is broken.
  --dedupe-key PATH  jq-style path inside each item for dedup key.
                     Default: "id". Set to "" to skip dedup.
  --items-key PATH   Where the paginated array lives in the response.
                     Default: "items". Some tools use "messages",
                     "files", etc.
  --page-token-key K Where the next-page token lives. Default:
                     "nextPageToken". Some APIs use "next_page_token"
                     or "pageToken".
  --page-token-arg K Name of the parameter that takes the token.
                     Default: "pageToken".
  --slim FIELDS      Comma-separated jq-style field paths to keep
                     per item. Drops everything else. Reduces output
                     size dramatically. Example: for YouTube subs,
                     --slim snippet.title,snippet.resourceId.channelId,id
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# Import the existing composio_client (handles auth, RPC, response unwrapping)
sys.path.insert(0, str(Path.home() / "bin"))
sys.path.insert(0, str(Path(__file__).parent))
try:
    from composio_client import execute as composio_execute
except ImportError as e:
    print(f"composio-paginate: cannot import composio_client: {e}", file=sys.stderr)
    sys.exit(2)


def deep_get(obj: dict, path: str) -> Any:
    """Walk obj along a dot-separated path. Returns None if any hop misses."""
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def slim_item(item: dict, fields: list[str]) -> dict:
    """Return a new dict with only the listed dot-paths kept."""
    out = {}
    for path in fields:
        val = deep_get(item, path)
        # Re-nest under the same path structure so the shape is preserved
        parts = path.split(".")
        cur = out
        for i, p in enumerate(parts):
            if i == len(parts) - 1:
                cur[p] = val
            else:
                if p not in cur or not isinstance(cur[p], dict):
                    cur[p] = {}
                cur = cur[p]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Paginate a Composio tool call")
    ap.add_argument("slug", help="Composio tool slug (e.g. YOUTUBE_LIST_USER_SUBSCRIPTIONS)")
    ap.add_argument("--args", required=True, help='JSON-encoded tool args')
    ap.add_argument("--account", default=None, help="Optional account alias")
    ap.add_argument("--max-pages", type=int, default=30)
    ap.add_argument("--dedupe-key", default="id", help='Dedup key path. Empty string disables.')
    ap.add_argument("--items-key", default="items")
    ap.add_argument("--page-token-key", default="nextPageToken")
    ap.add_argument("--page-token-arg", default="pageToken")
    ap.add_argument("--slim", default=None, help="Comma-separated dot-paths to keep per item")
    ap.add_argument("--verbose", action="store_true", help="Per-page progress to stderr")
    args = ap.parse_args()

    try:
        base_args = json.loads(args.args)
    except json.JSONDecodeError as e:
        print(f"composio-paginate: --args is not valid JSON: {e}", file=sys.stderr)
        return 2
    if not isinstance(base_args, dict):
        print("composio-paginate: --args must be a JSON object", file=sys.stderr)
        return 2

    slim_fields = [s.strip() for s in args.slim.split(",")] if args.slim else None

    all_items: list[dict] = []
    seen: set[str] = set()
    seen_tokens: set[str] = set()
    page_token: str | None = None
    pages = 0
    start = time.time()

    while pages < args.max_pages:
        call_args = dict(base_args)
        if page_token:
            call_args[args.page_token_arg] = page_token

        if args.verbose:
            elapsed = time.time() - start
            print(f"[composio-paginate] page {pages+1}: token={page_token[:20] if page_token else 'first'} "
                  f"items_so_far={len(all_items)} elapsed={elapsed:.1f}s", file=sys.stderr)

        try:
            # Call the low-level _rpc directly so we can inspect the
            # full response envelope (including data_preview when
            # Composio truncates). composio_execute returns only `data`
            # which is empty for truncated responses.
            from composio_client import _rpc as _composio_rpc
            tool_item = {"tool_slug": args.slug, "arguments": call_args}
            if args.account:
                tool_item["account"] = args.account
            raw = _composio_rpc("tools/call", {
                "name": "COMPOSIO_MULTI_EXECUTE_TOOL",
                "arguments": {
                    "tools": [tool_item],
                    "thought": f"paginate {args.slug} page {pages+1}",
                    "sync_response_to_workbench": False,
                    "current_step": args.slug,
                },
            }, timeout=90)
            text = raw.get("content", [{}])[0].get("text", "")
            envelope = json.loads(text) if text else {}
            results = envelope.get("data", {}).get("results") or []
            inner = (results[0].get("response") if results else {}) or {}
            if inner.get("successful") is False:
                err_msg = inner.get("error") or inner.get("message") or "tool returned successful=false"
                raise RuntimeError(err_msg)
            # Prefer `data` (full); fall back to `data_preview` (truncated)
            # for the items + nextPageToken so we still make progress
            # even when Composio truncates a single page.
            resp = inner.get("data") if isinstance(inner.get("data"), dict) and inner["data"] else None
            if not resp:
                resp = inner.get("data_preview") if isinstance(inner.get("data_preview"), dict) else {}
        except Exception as e:
            print(json.dumps({
                "items": all_items,
                "total": len(all_items),
                "pages": pages,
                "error": f"page {pages+1} failed: {e}",
                "partial": True,
            }))
            return 1

        items = resp.get(args.items_key, [])
        if not isinstance(items, list):
            items = []
        # Filter out string-typed placeholders like "...28 more items"
        # that data_preview includes as truncation markers.
        items = [it for it in items if isinstance(it, dict)]

        # Dedup
        new_count = 0
        for it in items:
            if args.dedupe_key:
                key = deep_get(it, args.dedupe_key)
                if key is None:
                    # Can't dedup — include unconditionally
                    all_items.append(slim_item(it, slim_fields) if slim_fields else it)
                    new_count += 1
                    continue
                key_s = str(key)
                if key_s in seen:
                    continue
                seen.add(key_s)
            all_items.append(slim_item(it, slim_fields) if slim_fields else it)
            new_count += 1

        # Advance pagination
        next_token = resp.get(args.page_token_key)
        if not next_token:
            # Also check inside data_preview if main response didn't have it
            preview = resp.get("data_preview")
            if isinstance(preview, dict):
                next_token = preview.get(args.page_token_key)
        if not next_token:
            pages += 1
            break
        if next_token in seen_tokens:
            # Repeated token — API stuck, bail out
            pages += 1
            break
        seen_tokens.add(next_token)
        page_token = next_token
        pages += 1

    elapsed = time.time() - start
    print(json.dumps({
        "items": all_items,
        "total": len(all_items),
        "pages": pages,
        "elapsed_s": round(elapsed, 1),
        "tool_slug": args.slug,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
