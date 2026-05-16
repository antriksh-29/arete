#!/bin/bash
# heartbeat-meeting-prep.sh — thin shim that defers to the Python
# implementation. The shell script is what the systemd unit invokes
# (kept for backwards compat); all logic lives in heartbeat-meeting-prep.py.
#
# Migrated from a full shell implementation 2026-05-16 — the rewrite swaps
# ClawVisor's calendar gateway for Composio's GOOGLECALENDAR_EVENTS_LIST
# via composio_client.py, and removes the cumbersome curl + heredoc
# shell-escaping plumbing.

set -e
export PATH="/home/botuser/.bun/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

exec /usr/bin/python3 "$(dirname "$0")/heartbeat-meeting-prep.py" "$@"
