#!/usr/bin/env python3
"""book-reservation.py — end-to-end restaurant/venue booking orchestrator.

Owned by skills/book-reservation/SKILL.md. The agent invokes this with
the user's full message; this script:

1. Parses venue + time + party + date from the message.
2. Discovers a booking URL — Google Maps Reserve first, then
   District / Zomato / EazyDiner / Swiggy / restaurant's own site.
3. Drives the booking widget via ~/bin/fill-form.py — reuses the
   existing form-fill skill as the executor, this script is just
   the orchestrator.
4. Auto-delivers progress screenshots via send-telegram-photo.sh.
5. Stops before the final confirm click and surfaces the screenshot
   for user review.
6. If the platform reaches an OTP screen, saves state and exits with
   state=otp_pending. The agent waits for the user's OTP, then calls
   `book-reservation.py --resume "<otp>"` which loads the most recent
   state file and continues the fill.

Usage:
    book-reservation.py "<user message>"
    book-reservation.py --resume "<otp digits>"
    book-reservation.py --smoke
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

STATE_DIR = Path("/tmp")
STATE_PREFIX = "booking-state-"
USER_DEFAULTS = {
    # Sourced from USER.md / brain — kept here so the orchestrator
    # works even when those aren't accessible. Override per-message
    # via the parsed intent.
    "Name": "Antriksh Jain",
    "First name": "Antriksh",
    "Phone": "+918310872480",
    "Phone number": "+918310872480",
    "Email": "antriksh1980@gmail.com",
}
FILL_FORM = os.path.expanduser("~/bin/fill-form.py")
SEND_PHOTO = os.path.expanduser("~/bin/send-telegram-photo.sh")


# ── Parsing the user's natural-language intent ──────────────────────

_STOP_WORDS = (
    "in", "for", "tonight", "tomorrow", "today", "this", "next", "on", "at",
    "by", "from", "till", "until", "around", "near", "after", "before",
)
_STOP_RE = r"(?:" + r"|".join(_STOP_WORDS) + r")"
# Capture the shortest capitalized phrase (with optional connectors like
# &, ', -) after "at"/"@" or "book"/"reserve <X>" — stop at any
# lowercase preposition, digit, or punctuation. Non-greedy `+?` ensures
# we don't swallow trailing prepositional phrases.
VENUE_HINT_RE = re.compile(
    r"(?:\b(?:at|@)\s+)([A-Z][A-Za-z0-9'&.\- ]*?)(?=\s+" + _STOP_RE + r"\b|\s+\d|[,?!.]|$)",
    re.IGNORECASE,
)
VENUE_VERB_RE = re.compile(
    r"\b(?:book|reserve|reservation\s+at)\s+([A-Z][A-Za-z0-9'&.\- ]*?)(?=\s+" + _STOP_RE + r"\b|\s+\d|[,?!.]|$)",
    re.IGNORECASE,
)
PARTY_RE = re.compile(r"\bfor\s+(\d{1,2})\s*(?:people|guests|of\s+us|persons|adults|pax)?\b", re.I)
PARTY_OF_RE = re.compile(r"\b(?:party\s+of|table\s+for|table\s+of)\s+(\d{1,2})\b", re.I)
TIME_RE = re.compile(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm|AM|PM))\b")
TIME_24H_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
DAY_RE = re.compile(r"\b(tonight|tomorrow|today|this\s+evening|tomorrow\s+evening|tomorrow\s+night)\b", re.I)
DATE_ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def parse_intent(msg: str) -> dict:
    """Extract venue, party, time, date from a free-text booking message.
    Missing fields stay None; the caller asks for them or uses defaults.
    """
    venue = None
    m = VENUE_HINT_RE.search(msg)
    if m:
        venue = m.group(1).strip().rstrip(",. ")
    if not venue:
        m2 = VENUE_VERB_RE.search(msg)
        if m2:
            venue = m2.group(1).strip().rstrip(",. ")
    # Single-word "book Uru tonight" still needs to land Uru — handle the
    # case where the capture above stopped before the venue (the
    # verb-form regex demands at least one capitalised char).
    if not venue:
        m3 = re.search(r"\b(?:book|reserve)\s+([A-Z][A-Za-z0-9'&.\-]*)\b", msg)
        if m3:
            venue = m3.group(1).strip()

    party = None
    for r in (PARTY_RE, PARTY_OF_RE):
        m = r.search(msg)
        if m:
            party = int(m.group(1))
            break

    booking_time = None
    m = TIME_RE.search(msg)
    if m:
        booking_time = m.group(1).strip().upper().replace(" ", "")
    if not booking_time:
        m = TIME_24H_RE.search(msg)
        if m:
            booking_time = f"{m.group(1)}:{m.group(2)}"

    day = None
    m = DAY_RE.search(msg)
    if m:
        day = m.group(1).lower().split()[0]
    booking_date = None
    m = DATE_ISO_RE.search(msg)
    if m:
        booking_date = m.group(1)
    else:
        today = date.today()
        if day in ("tonight", "today", None):
            booking_date = today.isoformat()
        elif day == "tomorrow":
            booking_date = (today + timedelta(days=1)).isoformat()

    return {
        "venue": venue,
        "party": party,
        "time": booking_time,
        "day": day,
        "date": booking_date,
        "raw_message": msg,
    }


# ── URL discovery for each platform ─────────────────────────────────


def _google_search(query: str, max_results: int = 5) -> list[str]:
    """Light Google search — pulls href= links out of the SERP HTML.
    Free, no API key. ~1-2s. Returns up to max_results URLs."""
    q = urllib.parse.quote(query)
    url = f"https://www.google.com/search?q={q}&hl=en"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", "ignore")
    except Exception as e:
        log(f"google search failed: {e}")
        return []
    # SERP href links look like <a href="/url?q=https://..."> — extract.
    urls = re.findall(r'href="/url\?q=(https?://[^&"]+)', html)
    seen = set()
    out = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= max_results:
            break
    return out


def find_gmr_url(venue: str) -> str | None:
    """Find a Google Maps Reserve URL for the venue. Returns None if
    no GMR-enabled provider found."""
    # The reserve URLs look like google.com/maps/reserve/v/dine/c/<id>
    candidates = _google_search(f"{venue} reserve a table site:google.com/maps") + \
                 _google_search(f"{venue} reserve a table google maps")
    for u in candidates:
        m = re.search(r"https://www\.google\.com/maps/reserve/v/dine/c/[A-Za-z0-9_-]+", u)
        if m:
            base = m.group(0)
            # Force English locale + paid-source hint so the widget
            # surfaces correctly.
            return f"{base}?source=pa&hl=en-IN"
    return None


def find_district_url(venue: str, date_iso: str | None, party: int | None) -> str | None:
    """Find a District (district.in/dining) booking URL."""
    candidates = _google_search(f"{venue} district zomato book")
    for u in candidates:
        if "district.in/dining" in u and "/book" in u:
            base = u.split("?")[0]
            params = []
            if date_iso:
                params.append(f"date={date_iso}")
            if party:
                params.append(f"guests={party}")
            return base + ("?" + "&".join(params) if params else "")
        if "district.in/dining" in u and "/book" not in u:
            return u.rstrip("/") + "/book"
    return None


def find_zomato_url(venue: str) -> str | None:
    candidates = _google_search(f"{venue} zomato book a table")
    for u in candidates:
        if "zomato.com" in u and "/book" in u:
            return u
        if "zomato.com" in u and "/restaurants/" not in u and "/restaurant/" not in u:
            continue
        if "zomato.com" in u:
            return u.rstrip("/") + "/book"
    return None


def find_eazydiner_url(venue: str) -> str | None:
    candidates = _google_search(f"{venue} eazydiner book")
    for u in candidates:
        if "eazydiner.com/restaurants/" in u or "eazydiner.com/restaurant/" in u:
            return u
    return None


def find_swiggy_url(venue: str) -> str | None:
    candidates = _google_search(f"{venue} swiggy dineout book")
    for u in candidates:
        if "swiggy.com/dineout" in u:
            return u
    return None


# ── Driving a platform via fill-form.py ──────────────────────────────


def _answers_for(intent: dict) -> dict:
    """Build the answers JSON used by fill-form."""
    return {
        **USER_DEFAULTS,
        "Time": intent.get("time") or "",
        "Date": intent.get("date") or "",
        "Party size": str(intent.get("party") or ""),
        "Guests": str(intent.get("party") or ""),
        "provider_preference": "eazydiner,swiggy",
    }


def drive(url: str, intent: dict, platform: str, max_steps: int = 14) -> dict:
    """Run fill-form.py on a URL with the booking answers. Returns the
    parsed envelope plus a derived `needs_otp` flag."""
    ts = int(time.time())
    answers_path = f"/tmp/booking-answers-{platform}-{ts}.json"
    Path(answers_path).write_text(json.dumps(_answers_for(intent), indent=2))
    shot_path = f"/tmp/booking-{platform}-{ts}.png"
    cmd = [
        "python3", FILL_FORM, url, answers_path,
        "--max-steps", str(max_steps),
        "--screenshot", shot_path,
    ]
    log(f"driving {platform} → {url}")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=360)
    except subprocess.TimeoutExpired:
        return {"platform": platform, "url": url, "ok": False, "reason": "timeout", "screenshot": shot_path}
    # fill-form emits a single-line JSON envelope as the final line.
    last_line = (out.stdout.strip().splitlines() or [""])[-1]
    try:
        env = json.loads(last_line)
    except json.JSONDecodeError:
        return {"platform": platform, "url": url, "ok": False, "reason": "fill_form_no_json",
                "stdout_tail": out.stdout[-500:], "stderr_tail": out.stderr[-500:]}
    env["platform"] = platform
    env["url"] = url
    env["needs_otp"] = _detect_otp_screen(env)
    return env


def _detect_otp_screen(env: dict) -> bool:
    """Heuristic: an OTP step is one where the last visible fields
    include a single short-numeric input or a heading containing
    'OTP' / 'verification' / 'code'."""
    last = env.get("step_records", [])[-1] if env.get("step_records") else {}
    verify = last.get("verify_state", {})
    # If a text input is on the page named like OTP/verify/code, that's
    # the signal. fill-form's verify_state has 'texts' = field values
    # already filled — for an EMPTY OTP screen this list is empty, so
    # check the errors and step screenshots' DOM-reported headings.
    for err in env.get("errors", []):
        h = (err.get("heading") or "").lower()
        if any(k in h for k in ("otp", "verification", "verify", "code", "pin")):
            return True
    for f in env.get("filled", []):
        h = (f.get("heading") or "").lower()
        if any(k in h for k in ("otp", "verification", "code", "pin")):
            return True
    return False


# ── Telegram delivery + state persistence ───────────────────────────


def send_photo(path: str, caption: str = "") -> None:
    if not os.path.exists(SEND_PHOTO) or not os.path.exists(path):
        return
    try:
        subprocess.run([SEND_PHOTO, path, caption], capture_output=True, timeout=30)
    except Exception as e:
        log(f"photo send failed: {e}")


def save_state(intent: dict, env: dict) -> str:
    ts = int(time.time())
    path = STATE_DIR / f"{STATE_PREFIX}{ts}.json"
    path.write_text(json.dumps({
        "saved_at": datetime.now().isoformat(),
        "intent": intent,
        "last_envelope": env,
    }, indent=2))
    return str(path)


def load_latest_state() -> dict | None:
    files = sorted(STATE_DIR.glob(f"{STATE_PREFIX}*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text())
    except Exception:
        return None


# ── Top-level orchestration ─────────────────────────────────────────


def log(msg: str) -> None:
    print(f"[book-reservation] {msg}", file=sys.stderr)


def book(message: str) -> dict:
    intent = parse_intent(message)
    if not intent.get("venue"):
        return _envelope(state="needs_clarification", intent=intent,
                         user_message="Booking intent detected, but I couldn't pull the venue name out of your message. Which restaurant?")
    if not intent.get("party"):
        return _envelope(state="needs_clarification", intent=intent,
                         user_message=f"Got '{intent['venue']}' — how many people?")

    platforms = [
        ("gmr",       find_gmr_url,       lambda url: drive(url, intent, "gmr", max_steps=18)),
        ("district",  lambda v: find_district_url(v, intent.get("date"), intent.get("party")),
                                                  lambda url: drive(url, intent, "district", max_steps=14)),
        ("zomato",    find_zomato_url,    lambda url: drive(url, intent, "zomato", max_steps=14)),
        ("eazydiner", find_eazydiner_url, lambda url: drive(url, intent, "eazydiner", max_steps=14)),
        ("swiggy",    find_swiggy_url,    lambda url: drive(url, intent, "swiggy", max_steps=14)),
    ]

    attempts = []
    for name, finder, driver_fn in platforms:
        log(f"trying platform: {name}")
        url = finder(intent["venue"])
        if not url:
            attempts.append({"platform": name, "ok": False, "reason": "url_not_found"})
            continue
        env = driver_fn(url)
        attempts.append(env)
        # Did we actually fill anything?
        filled = env.get("filled", []) or []
        if env.get("needs_otp"):
            state_path = save_state(intent, env)
            return _envelope(state="otp_pending", intent=intent, attempts=attempts,
                             screenshot=env.get("screenshot"), state_file=state_path,
                             platform=name,
                             user_message=(f"📅 {intent['venue']} via {name.upper()}: OTP screen reached. "
                                           f"Paste the code you received and I'll continue."))
        if filled and not env.get("errors"):
            # Reached a confirm-ready state without OTP
            state_path = save_state(intent, env)
            return _envelope(state="confirm_ready", intent=intent, attempts=attempts,
                             screenshot=env.get("screenshot"), state_file=state_path,
                             platform=name,
                             user_message=(f"📅 {intent['venue']} via {name.upper()}: review the screenshot. "
                                           f"Reply 'confirm' to submit, or 'cancel' to abort."))
        # 0 fields filled OR documented error → try next platform
        log(f"{name} produced 0 useful fills, falling through")

    # All platforms failed
    return _envelope(state="all_platforms_failed", intent=intent, attempts=attempts,
                     user_message=(f"❌ Tried GMR + District + Zomato + EazyDiner + Swiggy for {intent['venue']}. "
                                   f"None reached a fillable booking page. Phone fallback: try calling the venue directly."))


def resume(otp: str) -> dict:
    state = load_latest_state()
    if not state:
        return _envelope(state="no_state", user_message="No paused booking found to resume.")
    env = state.get("last_envelope", {})
    url = env.get("url")
    platform = env.get("platform", "?")
    intent = state.get("intent", {})
    if not url:
        return _envelope(state="no_state", user_message="Paused booking found but no URL to resume.")
    # Build a fresh answers JSON with the OTP filled in
    answers = _answers_for(intent)
    answers["OTP"] = otp
    answers["Verification code"] = otp
    answers["Code"] = otp
    answers_path = f"/tmp/booking-resume-{int(time.time())}.json"
    Path(answers_path).write_text(json.dumps(answers, indent=2))
    shot_path = f"/tmp/booking-resume-{platform}-{int(time.time())}.png"
    cmd = ["python3", FILL_FORM, url, answers_path, "--max-steps", "8", "--screenshot", shot_path]
    log(f"resuming {platform} with OTP")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        return _envelope(state="failed", user_message="OTP resume timed out.")
    last = (out.stdout.strip().splitlines() or [""])[-1]
    try:
        env2 = json.loads(last)
    except json.JSONDecodeError:
        return _envelope(state="failed", user_message="OTP resume produced no envelope.")
    env2["platform"] = platform
    return _envelope(state="confirm_ready", intent=intent, attempts=[env2],
                     screenshot=env2.get("screenshot"), platform=platform,
                     user_message=f"📅 Resumed with OTP. Review the screenshot — reply 'confirm' to submit.")


def _envelope(**kwargs) -> dict:
    return {
        "ok": True,
        "skill": "book-reservation",
        **kwargs,
    }


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit('Usage: book-reservation.py "<message>" | --resume "<otp>" | --smoke')
    if sys.argv[1] == "--smoke":
        print(json.dumps({"ok": True, "skill": "book-reservation", "user_message": "book-reservation smoke ok"}))
        return
    if sys.argv[1] == "--resume":
        if len(sys.argv) < 3:
            sys.exit("Usage: book-reservation.py --resume <otp>")
        otp = re.sub(r"\D", "", sys.argv[2])[:8]  # strip non-digits, cap 8
        env = resume(otp)
    else:
        message = sys.argv[1]
        env = book(message)
    print(json.dumps(env))


if __name__ == "__main__":
    main()
