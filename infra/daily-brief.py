#!/usr/bin/env python3
# Deterministic morning briefing orchestrator.
#
# Owns the section order and final composition. Replaces the agent-based
# briefing because the agent kept reordering sections despite explicit
# instruction. Each data source is a separate call; this script just stitches.
#
# Section order (locked):
#   1. 📅 Calendar
#   2. 🌦️ Weather
#   3. 📋 Carryover from yesterday
#   4. 📥 Today's check-ins
#   5. 📈 Markets
#   6. 🏃 Fitness
#
# Empty sections are skipped entirely.
# Section headers are bold via Telegram HTML parse mode.
# Hard cap: 3800 chars total. Truncates emails first, then calendar.
#
# Usage:  daily-brief.py [--dry-run] [--no-send]
#   --dry-run  appends "(DRY RUN)" to header
#   --no-send  print to stdout instead of posting to Telegram

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone, date
from html import escape

os.environ["PATH"] = "/home/botuser/.bun/bin:" + os.environ.get("PATH", "")

sys.path.insert(0, "/home/botuser/bin")
import llm  # type: ignore

IST = timezone(timedelta(hours=5, minutes=30))
TODAY_IST = datetime.now(IST).date()

ACCOUNTS = ["antriksh1980@gmail.com", "antriksh@getarete.co"]
MAX_CHARS = 3800
EMAIL_CAP_DEFAULT = 5
CAL_CAP_DEFAULT = 6


def log(msg):
    print(f"[daily-brief] {msg}", file=sys.stderr)


def vault_get(key):
    return subprocess.run(
        ["bot-cred", "get", key], capture_output=True, text=True, check=True
    ).stdout.strip()


def run(cmd, timeout=60):
    """Run a shell command, return stdout text or empty string on failure."""
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            log(f"cmd failed ({r.returncode}): {cmd}; stderr={r.stderr[:200]}")
            return ""
        return r.stdout.strip()
    except Exception as e:
        log(f"cmd error {cmd}: {e}")
        return ""


# -------------------- ClawVisor gateway --------------------

def gateway_call(account, service_kind, action, params, reason):
    cv_url = vault_get("CLAWVISOR_URL")
    cv_token = vault_get("CLAWVISOR_AGENT_TOKEN")
    cv_task = vault_get("CLAWVISOR_STANDING_TASK_ID")
    payload = {
        "task_id": cv_task,
        "session_id": str(uuid.uuid4()),
        "service": f"google.{service_kind}:{account}",
        "action": action,
        "params": params,
        "reason": reason,
    }
    req = urllib.request.Request(
        f"{cv_url}/api/gateway/request?wait=true",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {cv_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
    except Exception as e:
        log(f"gateway {action} error: {e}")
        return {}
    if d.get("status") != "executed":
        log(f"gateway {action} not executed: {json.dumps(d)[:200]}")
        return {}
    return d.get("result", {})


# -------------------- 1. Calendar --------------------

def fetch_calendar():
    """Today + tomorrow events from both accounts. Returns list of dicts."""
    now_utc = datetime.now(timezone.utc)
    end_of_tomorrow_ist = datetime(TODAY_IST.year, TODAY_IST.month, TODAY_IST.day, 23, 59, 59, tzinfo=IST) + timedelta(days=1)
    events = []
    for account in ACCOUNTS:
        result = gateway_call(
            account=account,
            service_kind="calendar",
            action="list_events",
            params={
                "from": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to": end_of_tomorrow_ist.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "max_results": 25,
                "single_events": True,
                "order_by": "startTime",
            },
            reason="Morning briefing: list calendar events for today and tomorrow to surface upcoming meetings",
        )
        for ev in result.get("data", []) or []:
            events.append({**ev, "_account": account})
    return events


def fmt_calendar(events, cap):
    if not events:
        return None
    lines = []
    for ev in events[:cap]:
        start = ev.get("start", "")
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(IST)
            day = "Today" if dt.date() == TODAY_IST else dt.strftime("%a %d %b")
            time_str = f"{day} {dt.strftime('%H:%M')}"
        except Exception:
            time_str = start[:16]
        title = (ev.get("summary") or "(untitled)").strip()
        # Filter out self-attendees, keep names only
        attendees = []
        for a in ev.get("attendees", []) or []:
            email = (a.get("email") or "").lower()
            if email in ACCOUNTS or a.get("self") or a.get("organizer"):
                continue
            name = a.get("displayName") or email.split("@")[0]
            if name:
                attendees.append(name)
        attendee_str = f" with {', '.join(attendees[:3])}" if attendees else ""
        if len(attendees) > 3:
            attendee_str += f" +{len(attendees) - 3}"
        lines.append(f"• {time_str}: {title}{attendee_str}")
    if len(events) > cap:
        lines.append(f"• …and {len(events) - cap} more")
    return lines


# -------------------- 2. Weather --------------------

def fetch_weather():
    out = run(["python3", "/home/botuser/bin/weather-snapshot.py"], timeout=30)
    if not out:
        return None
    # Drop the first line (the heading like "🌦️ Bangalore — Wed 06 May") since
    # we render our own bold heading. Keep only the bullet lines.
    lines = [l for l in out.split("\n") if l.startswith("•")]
    return lines if lines else None


# -------------------- 3. Pending tasks --------------------

def fetch_tasks(cap=5):
    out = run("/home/botuser/bin/daily-tasks-briefing-append.sh", timeout=20)
    if not out:
        return None
    # Output has multiple "## Section" headers. Extract bullet items only.
    items = []
    for line in out.split("\n"):
        line = line.strip()
        if line.startswith("- ") or line.startswith("* "):
            items.append("• " + line[2:].strip())
    if not items:
        return None
    return items[:cap]


# -------------------- 4. Email triage (LLM) --------------------

EMAIL_TRIAGE_SYSTEM = """You are triaging Antriksh's overnight email for his morning briefing. Return ONLY the genuinely action-worthy items.

HIGH importance — surface these:
- Direct response/action needed from Antriksh (replies awaited, decisions to make)
- Bills DUE soon (utility, broadband, mobile, insurance premium, credit card statement with a due date)
- Payment REMINDERS for upcoming dues (e.g. "your bill is due in 3 days", "EMI due tomorrow")
- Government / tax / regulatory notices (IT department, GST, MCA, RBI)
- OOO replies or status changes from people he was waiting on
- Counterparty actions on ongoing work (proposals approved, kickoffs scheduled, deliveries promised by date)
- Service disruptions affecting him (API quota exhausted, account suspended, payment failed, subscription expiring)

LOW / SKIP — do NOT surface these:
- Past-transaction confirmations (UPI debit/credit alerts confirming a transaction that already happened — these are receipts, not actions)
- Marketing newsletters, promo offers, sales pitches, "we have new features" announcements
- Order/delivery confirmations unless something is delayed/stuck
- LinkedIn / Twitter / Instagram / Reddit / Substack notifications
- Calendar invite confirmations
- Routine "your statement is ready" without a due date in the snippet
- Auto-generated reports without an action item
- Corporate-action / shareholder notices: e-voting, AGM, postal ballot,
  dividend vouchers, book closure, record date, rights/bonus/buyback —
  these never require same-day action from a retail investor and pollute
  the briefing. Always SKIP regardless of sender

Heuristic for transaction alerts: a UPI/card debit notification by itself is a receipt and goes to LOW. A monthly STATEMENT with a due date goes to HIGH only on the day before the due date or after.

Output STRICT JSON only. No preamble, no markdown fences. Schema:

{
  "high_importance": [
    {"sender": "<short sender label, e.g. 'HDFC Bank' or 'Kaushik (ArmorIQ)'>", "summary": "<one-line summary ≤90 chars, includes any specific date/amount that makes it actionable>"}
  ]
}

Order by urgency: bills due / service disruptions first, then counterparty actions, then OOO / state-changes. If nothing qualifies, return {"high_importance": []}."""


# Pre-filter: transaction-confirmation emails that are pure receipts (no
# action required). LLM was unreliable at consistently demoting these even
# with explicit instruction, so we drop them deterministically.
TXN_NOISE_RE = re.compile(
    r"(insta\s?alerts?|txn\s?alert|transaction\s?alert|debit\s?alert|credit\s?alert"
    r"|upi\s?(?:txn|debit|credit|alert))",
    re.IGNORECASE,
)
TXN_NOISE_SUBJECT_RE = re.compile(
    r"(debited|credited|upi\s+txn|spent\s+₹|received\s+₹|debit\s+from|credit\s+to)",
    re.IGNORECASE,
)

# Corporate-action / shareholder-notice noise: e-voting, AGM notices, postal
# ballots, dividend vouchers from depositories and registrars. These don't
# require action from a typical retail investor and pollute the briefing.
CORP_ACTION_NOISE_RE = re.compile(
    r"(evoting|e-voting|cdslindia|nsdlindia|kfintech|linkintime|cameoindia"
    r"|computerage|integrated\s?registry|bigshare|skylineRTA|alankit"
    r"|venturecaststor|maashitla)",
    re.IGNORECASE,
)
CORP_ACTION_SUBJECT_RE = re.compile(
    r"(e-?voting|postal\s+ballot|agm\s+notice|annual\s+general\s+meeting"
    r"|dividend\s+(?:voucher|warrant|notice)|corporate\s+action"
    r"|shareholder\s+notice|book\s+closure|record\s+date|rights\s+issue"
    r"|bonus\s+issue|buy\s?back\s+offer)",
    re.IGNORECASE,
)


def is_txn_noise(from_addr, subject):
    """Past-transaction confirmation emails: sender or subject screams 'this is a receipt'."""
    if TXN_NOISE_RE.search(from_addr) and TXN_NOISE_SUBJECT_RE.search(subject):
        return True
    return False


def is_corp_action_noise(from_addr, subject):
    """Corporate-action / shareholder-notice emails: never actionable by user.

    Either sender is a depository/RTA (CDSL/NSDL/Kfintech/etc) OR subject
    matches a known corporate-action pattern.
    """
    if CORP_ACTION_NOISE_RE.search(from_addr):
        return True
    if CORP_ACTION_SUBJECT_RE.search(subject):
        return True
    return False


def fetch_emails():
    """Pull last 24h from both accounts, triage via LLM, return formatted list."""
    raw_emails = []
    for account in ACCOUNTS:
        result = gateway_call(
            account=account,
            service_kind="gmail",
            action="list_messages",
            params={"max_results": 50, "query": "newer_than:1d"},
            reason="Morning briefing: read last 24h email subjects + senders + snippets to surface high-importance items including bills, deadlines, and counterparty actions",
        )
        msgs = (result.get("data") or {}).get("messages", []) or []
        if not msgs:
            d = result.get("data") or []
            if isinstance(d, list):
                msgs = d
        for m in msgs:
            from_addr = (m.get("from") or "").strip()
            subject = (m.get("subject") or "").strip()
            if is_txn_noise(from_addr, subject):
                continue
            if is_corp_action_noise(from_addr, subject):
                continue
            raw_emails.append({
                "account": account,
                "from": from_addr,
                "subject": subject,
                "snippet": (m.get("snippet") or "").strip()[:280],
                "is_unread": bool(m.get("is_unread")),
            })
    if not raw_emails:
        return None
    log(f"pulled {len(raw_emails)} emails from {len(ACCOUNTS)} accounts (after txn-noise filter)")

    # Pass everything to LLM for triage. Cap input size.
    user_msg_lines = ["Emails from last 24h:\n"]
    for i, e in enumerate(raw_emails[:80], 1):
        user_msg_lines.append(
            f"{i}. From: {e['from'][:80]}\n"
            f"   Subject: {e['subject'][:120]}\n"
            f"   Snippet: {e['snippet'][:240]}\n"
        )
    user_msg = "\n".join(user_msg_lines) + "\nReturn the JSON now."

    try:
        text, provider = llm.call(
            system_prompt=EMAIL_TRIAGE_SYSTEM,
            user_msg=user_msg,
            tier="mini",
            max_tokens=1500,
        )
    except Exception as e:
        log(f"email triage LLM failed: {e}")
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        d = json.loads(text)
    except json.JSONDecodeError as e:
        log(f"triage JSON parse failed: {e}; text head: {text[:300]}")
        return None
    items = d.get("high_importance", []) or []
    return items


def fmt_emails(items, cap):
    if not items:
        return None
    return [f"• <b>{escape(it.get('sender','?'))}</b>: {escape(it.get('summary','').strip())}" for it in items[:cap]]


# -------------------- 5. Markets --------------------

def fetch_markets():
    out = run(["python3", "/home/botuser/bin/market-snapshot.py"], timeout=30)
    if not out:
        return None
    lines = [l for l in out.split("\n") if l.startswith("•")]
    return lines if lines else None


# -------------------- 6. Fitness --------------------

def fetch_fitness():
    out = run(["python3", "/home/botuser/bin/fitness-snapshot.py"], timeout=15)
    if not out:
        return None
    # First line is the headline, rest are bullets. We'll keep all of it but
    # render our own bold header — so split out the headline.
    lines = out.split("\n")
    if not lines:
        return None
    # First line looks like "🏃 Fitness — 84 / 200 sessions (42.0%)".
    # Strip the emoji and the "Fitness — " prefix so the compose step can
    # render its own bold "Fitness — <stats>" without duplication.
    headline = lines[0]
    for prefix in ("🏃 Fitness — ", "🏃 ", "Fitness — "):
        if headline.startswith(prefix):
            headline = headline[len(prefix):]
    bullets = [l for l in lines[1:] if l.startswith("•")]
    return {"headline": headline.strip(), "bullets": bullets}


# -------------------- Composition --------------------

def compose(events, weather_lines, task_lines, email_items, market_lines, fitness, dry_run=False):
    """Build the briefing text with HTML bold headers. Returns (text, length)."""
    today_str = TODAY_IST.strftime("%a %d %b")
    suffix = " (DRY RUN)" if dry_run else ""
    parts = [f"🌅 <b>Morning briefing — {today_str}</b>{suffix}"]

    def add_section(emoji, title, lines):
        if not lines:
            return
        parts.append("")
        parts.append(f"{emoji} <b>{title}</b>")
        parts.extend(lines)

    cal_cap = CAL_CAP_DEFAULT
    email_cap = EMAIL_CAP_DEFAULT

    while True:
        out_parts = list(parts)
        cal_lines = fmt_calendar(events, cal_cap) if events else None
        email_lines = fmt_emails(email_items, email_cap) if email_items else None

        sections = list(out_parts)
        # Order: Calendar → Weather → Tasks → Emails → Markets → Fitness
        if cal_lines:
            sections += ["", "📅 <b>Calendar</b>"] + cal_lines
        if weather_lines:
            sections += ["", f"🌦️ <b>Weather — Bangalore</b>"] + weather_lines
        if task_lines:
            sections += ["", "📋 <b>Carryover from yesterday</b>"] + task_lines
        if email_lines:
            sections += ["", "📥 <b>Today's check-ins</b>"] + email_lines
        if market_lines:
            sections += ["", "📈 <b>Markets — last close</b>"] + market_lines
        if fitness:
            sections += ["", f"🏃 <b>Fitness — {escape(fitness['headline'])}</b>"] + fitness["bullets"]

        text = "\n".join(sections)
        if len(text) <= MAX_CHARS:
            return text
        # Truncate: emails first, then calendar
        if email_cap > 3:
            email_cap -= 1
            continue
        if cal_cap > 4:
            cal_cap -= 1
            continue
        return text[: MAX_CHARS - 50] + "\n\n…(truncated)"


# -------------------- Send to Telegram --------------------

def send_telegram_html(text):
    token = vault_get("TELEGRAM_BOT_TOKEN")
    chat_id = vault_get("TELEGRAM_USER_ID")
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        resp = json.loads(r.read())
    if not resp.get("ok"):
        raise RuntimeError(f"Telegram rejected: {json.dumps(resp)[:300]}")
    return resp


def main():
    dry_run = "--dry-run" in sys.argv
    no_send = "--no-send" in sys.argv

    log("fetching all sections in parallel-ish (sequential, but each is fast)…")
    events = fetch_calendar()
    weather_lines = fetch_weather()
    task_lines = fetch_tasks()
    email_items = fetch_emails()
    market_lines = fetch_markets()
    fitness = fetch_fitness()

    log(f"events={len(events) if events else 0} weather={'ok' if weather_lines else 'none'} "
        f"tasks={len(task_lines) if task_lines else 0} emails={len(email_items) if email_items else 0} "
        f"market={'ok' if market_lines else 'none'} fitness={'ok' if fitness else 'none'}")

    text = compose(events, weather_lines, task_lines, email_items, market_lines, fitness, dry_run=dry_run)
    log(f"composed {len(text)} chars")

    if no_send:
        print(text)
        return

    resp = send_telegram_html(text)
    log(f"telegram message_id={resp['result'].get('message_id')}")
    print(json.dumps({"ok": True, "chars": len(text), "message_id": resp["result"].get("message_id")}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {e}")
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)
