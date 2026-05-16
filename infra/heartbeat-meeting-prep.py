#!/usr/bin/env python3
"""Meeting-prep heartbeat — runs every 15 min during work hours (8am-10pm IST).

For each calendar event starting in the next 25-40 min that we haven't
already sent prep for, triggers an agent turn that writes prep notes
(attendee context, recent threads, open items) and delivers to Telegram.

Idempotency: $STATE_DIR/meeting-prep-sent-<DATE>.txt holds event IDs
we've notified on. Rotated daily.

Migrated from the bash original 2026-05-16 — replaces ClawVisor calendar
gateway with Composio's GOOGLECALENDAR_EVENTS_LIST via composio_client.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "/home/botuser/bin")
import composio_client  # type: ignore

IST = timezone(timedelta(hours=5, minutes=30))
HOME = Path.home()
STATE_DIR = Path(os.environ.get("STATE_DIR", HOME / ".brain-bootstrap-raw/state"))
LOG_DIR = Path(os.environ.get("LOG_DIR", HOME / ".brain-bootstrap-raw/logs"))
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

TODAY_IST = datetime.now(IST).date().isoformat()
SENT_FILE = STATE_DIR / f"meeting-prep-sent-{TODAY_IST}.txt"
SENT_FILE.touch()
LOG_FILE = LOG_DIR / f"heartbeat-{TODAY_IST}.log"

# Composio account routing (alias OR composio account ID → email).
CALENDAR_ACCOUNTS = [
    {"composio": "personal",                    "email": "antriksh@getarete.co"},
    {"composio": "googlecalendar_casse-wikeno", "email": "antriksh1980@gmail.com"},
]

# Telegram target (user's direct chat id).
TELEGRAM_RECIPIENT = "telegram:775188536"


def log(line: str) -> None:
    LOG_FILE.write_text(LOG_FILE.read_text() + f"[{datetime.now().isoformat()}] {line}\n"
                        if LOG_FILE.exists() else f"[{datetime.now().isoformat()}] {line}\n")


def already_sent(event_id: str) -> bool:
    return event_id in {l.strip() for l in SENT_FILE.read_text().splitlines() if l.strip()}


def mark_sent(event_id: str) -> None:
    with SENT_FILE.open("a") as f:
        f.write(event_id + "\n")


def fetch_upcoming_window(acct: dict) -> list[dict]:
    """List events starting in the next hour for this calendar account."""
    now_utc = datetime.now(timezone.utc)
    t_min = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    t_max = (now_utc + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        return composio_client.calendar_list_events(
            account=acct["composio"],
            time_min=t_min,
            time_max=t_max,
            max_results=10,
            calendar_id="primary",
            time_zone="Asia/Kolkata",
        )
    except Exception as e:
        log(f"composio fetch {acct['email']}: {e}")
        return []


def event_minutes_until(ev: dict) -> int | None:
    start = (ev.get("start") or {}).get("dateTime")
    if not start:
        return None
    try:
        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    except Exception:
        return None
    return int((dt - datetime.now(timezone.utc)).total_seconds() / 60)


def send_prep(ev: dict, account_email: str, minutes_until: int) -> None:
    summary = ev.get("summary", "(untitled)")
    event_id = ev.get("id", "")
    prompt = (
        f"A meeting starts in {minutes_until} minutes: \"{summary}\" "
        f"(calendar account: {account_email}, event id: {event_id}). "
        f"Pull the event details and attendees via the composio MCP (GOOGLECALENDAR_EVENTS_GET on this id, account=\"{account_email}\"). "
        f"For the non-self attendees, query the brain (gbrain__query) for context — recent threads, "
        f"open follow-ups, what we last talked about. Compose a short prep message (under 8 lines).\n\n"
        f"MANDATORY brain write-back (per identity/BRAIN_PROPAGATION.md): for EVERY new insight you "
        f"derive about an attendee from live data (e.g. recent Gmail thread you found, schedule "
        f"conflict you noticed), call gbrain__add_timeline_entry on that person's brain page BEFORE "
        f"sending the prep. This keeps the brain fresh so future preps and briefings build on it "
        f"instead of rediscovering.\n\n"
        f"Land the user-facing prep message on Telegram. Do brain writes silently."
    )
    log(f"sending prep for: {summary} (in {minutes_until}min, {account_email}, id={event_id})")
    cmd = [
        "openclaw", "agent",
        "--agent", "main",
        "--channel", "telegram",
        "--reply-to", TELEGRAM_RECIPIENT,
        "--deliver",
        "--message", prompt,
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        log(f"openclaw agent timed out for event {event_id}")


def main() -> int:
    for acct in CALENDAR_ACCOUNTS:
        events = fetch_upcoming_window(acct)
        for ev in events:
            mins = event_minutes_until(ev)
            if mins is None:
                continue
            # Send prep when event is 25-40 min away.
            if not (25 <= mins <= 40):
                continue
            event_id = ev.get("id", "")
            if not event_id or already_sent(event_id):
                continue
            send_prep(ev, acct["email"], mins)
            mark_sent(event_id)
    # Daily rotation: keep last 7 sent-files + 14 logs.
    for pattern, keep in [("meeting-prep-sent-*.txt", 7), ("heartbeat-*.log", 14)]:
        files = sorted((STATE_DIR if "sent" in pattern else LOG_DIR).glob(pattern),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            try:
                old.unlink()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
