#!/usr/bin/env python3
"""book-anything.py — end-to-end booking orchestrator (any modality).

Owned by skills/book-anything/SKILL.md. The agent invokes this with
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

# Import the LLM-first booking-intent extractor. Lives next to this file
# (or under ~/bin/ at deploy time). The extractor handles natural-language
# parsing across all modalities (restaurant, movie, event, generic URL);
# the rest of THIS file remains the restaurant-oriented orchestrator that
# drives GMR + provider fallbacks. When the extractor returns a non-
# restaurant modality with a platform_url, we short-circuit to fill-form.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import booking_intent  # noqa: E402
except ImportError:
    booking_intent = None  # falls back to legacy regex parse_intent()

STATE_DIR = Path("/tmp")
STATE_PREFIX = "booking-state-"
USER_DEFAULTS = {
    # Sourced from USER.md / brain — kept here so the orchestrator
    # works even when those aren't accessible. Override per-message
    # via the parsed intent.
    # Key names chosen to map UNAMBIGUOUSLY to platform form headings.
    # Avoid "Name" / "Last name" — they fuzz-match "First name" /
    # "Surname" the wrong way and cross-fill the values.
    "First name": "the user",
    "Surname": "Jain",
    "Phone number": "+918310872480",
    "Email": "${USER_EMAIL}",
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
_MONTH_NAMES = ("january","february","march","april","may","june","july",
                "august","september","october","november","december")
_MONTH_RE = "(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DATE_DM_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + _MONTH_RE + r")\b", re.I)
DATE_MD_RE = re.compile(r"\b(" + _MONTH_RE + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)


def _month_idx(token: str) -> int:
    t = token.lower()
    for i, n in enumerate(_MONTH_NAMES):
        if n.startswith(t) or t.startswith(n[:3]):
            return i
    return -1


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
    if not booking_date:
        # "13 May" / "13th May" / "May 13" / "May 13th"
        m = DATE_DM_RE.search(msg)
        d_num, mon_tok = (None, None)
        if m:
            d_num, mon_tok = int(m.group(1)), m.group(2)
        else:
            m = DATE_MD_RE.search(msg)
            if m:
                d_num, mon_tok = int(m.group(2)), m.group(1)
        if d_num and mon_tok:
            midx = _month_idx(mon_tok)
            if midx >= 0:
                today = date.today()
                year = today.year
                # If the date has already passed this year, assume next year.
                candidate = date(year, midx + 1, d_num)
                if candidate < today:
                    candidate = date(year + 1, midx + 1, d_num)
                booking_date = candidate.isoformat()
    if not booking_date:
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


_BH = os.path.expanduser("~/.local/bin/browser-harness")


def _google_search(query: str, max_results: int = 8) -> list[str]:
    """Google search via browser-harness — Google's SERP is JS-rendered,
    plain HTTP scraping gets 0 useful links. browser-harness drives a
    headless Chrome, waits for the SERP to render, and extracts every
    `a[href]` whose href is a non-google external URL.
    Returns up to max_results URLs in DOM order (top-ranked first).
    Falls back to plain HTTP scraping if browser-harness is missing.
    """
    q = urllib.parse.quote(query)
    if os.path.exists(_BH):
        script = (
            'import json, time\n'
            f'new_tab("https://www.google.com/search?q={q}&hl=en")\n'
            'wait_for_load()\n'
            'time.sleep(2)\n'
            'r = cdp("Runtime.evaluate", expression="""'
            '(function(){'
            '  var anchors = Array.from(document.querySelectorAll(\'a[href]\'));'
            '  var urls = anchors.map(function(a){return a.href||\"\";})'
            '    .filter(function(u){'
            '      if (!u.startsWith(\"http\")) return false;'
            '      if (u.indexOf(\"google.com/search\") >= 0) return false;'
            '      if (u.indexOf(\"google.com/url\") >= 0) return true;'
            '      if (u.indexOf(\"google.com/\") >= 0 && u.indexOf(\"/maps/reserve/\") < 0 && u.indexOf(\"/maps/place/\") < 0) return false;'
            '      if (u.indexOf(\"webcache.googleusercontent\") >= 0) return false;'
            '      return true;'
            '    });'
            '  return JSON.stringify(urls.slice(0, 30));'
            '})()'
            '""", returnByValue=True)\n'
            'print("---URLS---")\n'
            'print(r.get("result",{}).get("value","[]"))\n'
        )
        try:
            out = subprocess.run([_BH], input=script, capture_output=True,
                                 text=True, timeout=25)
            tail = out.stdout.split("---URLS---", 1)[-1].strip()
            if tail.startswith("["):
                urls = json.loads(tail.splitlines()[0])
                cleaned = []
                seen = set()
                for u in urls:
                    # Unwrap google.com/url?q= redirects
                    if "google.com/url" in u and "q=" in u:
                        try:
                            qs = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
                            real = qs.get("q", [u])[0]
                            u = real
                        except Exception:
                            pass
                    if u in seen:
                        continue
                    seen.add(u)
                    cleaned.append(u)
                    if len(cleaned) >= max_results:
                        break
                if cleaned:
                    return cleaned
        except Exception as e:
            log(f"browser-harness search failed: {e}")

    # Fallback: plain HTTP — works occasionally on some queries.
    url = f"https://www.google.com/search?q={q}&hl=en"
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", "ignore")
    except Exception as e:
        log(f"google search HTTP fallback failed: {e}")
        return []
    urls = re.findall(r'href="/url\?q=(https?://[^&"]+)', html) + \
           re.findall(r'href="(https?://[^"]+)"', html)
    seen = set()
    out_list = []
    for u in urls:
        if "google.com/search" in u: continue
        if u in seen: continue
        seen.add(u)
        out_list.append(u)
        if len(out_list) >= max_results: break
    return out_list


def _strip_fragment(u: str) -> str:
    """Strip URL fragments (#:~:text=...) added by Google to its SERP."""
    return u.split("#", 1)[0]


def _venue_tokens(venue: str) -> list[str]:
    """Tokens to require in a candidate URL path so we don't book at
    a totally different venue. Drops words shorter than 3 chars."""
    cleaned = re.sub(r"[^a-zA-Z0-9 ]", " ", venue).lower()
    return [t for t in cleaned.split() if len(t) >= 3 and t not in
            ("the", "and", "bar", "cafe", "kitchen", "restaurant")]


def _url_matches_venue(u: str, venue: str) -> bool:
    """True if the URL path contains at least one venue-name token.
    Rejects 'district.in/dining/jaipur/hum-tum-cafe' for venue 'Uru'."""
    tokens = _venue_tokens(venue)
    if not tokens:
        return True
    path = urllib.parse.urlparse(u).path.lower()
    return any(t in path for t in tokens)


_GMR_URL_RE = re.compile(r"https?://(?:www\.)?google\.com/maps/reserve/v/dine/c/[A-Za-z0-9_-]+", re.I)
_DISTRICT_URL_RE = re.compile(r"https?://(?:www\.)?district\.in/dining/[^\s\"'<>]+", re.I)
_ZOMATO_URL_RE = re.compile(r"https?://(?:www\.)?zomato\.com/[^\s\"'<>]+", re.I)
_EAZYDINER_URL_RE = re.compile(r"https?://(?:www\.)?eazydiner\.com/[^\s\"'<>]+", re.I)
_SWIGGY_URL_RE = re.compile(r"https?://(?:www\.)?swiggy\.com/[^\s\"'<>]+", re.I)


def _url_in_message(msg: str, regex: re.Pattern) -> str | None:
    """If the user pasted a platform URL in their message, prefer it
    over heuristic search. Lets the user pin a specific platform
    (e.g. \"use this GMR URL: https://...\")."""
    m = regex.search(msg or "")
    if not m:
        return None
    u = m.group(0)
    if "maps/reserve" in u and "source=" not in u:
        sep = "&" if "?" in u else "?"
        u = u + sep + "source=pa&hl=en-IN"
    return u


def find_gmr_url(venue: str) -> str | None:
    """Find a Google Maps Reserve URL for the venue. GMR pages aren't
    well-indexed by SERP, so we try 3 routes:
      1. Direct SERP for the maps/reserve URL pattern.
      2. Open Google Maps place page for the venue and look for the
         "Reserve a table" CTA's data-pwsid attribute.
      3. None — caller falls through to other providers.
    """
    # Route 1: direct SERP
    candidates = _google_search(f"{venue} reserve a table google maps") + \
                 _google_search(f"site:google.com/maps/reserve {venue}")
    for u in candidates:
        m = re.search(r"https://www\.google\.com/maps/reserve/v/dine/c/[A-Za-z0-9_-]+", u)
        if m:
            return f"{m.group(0)}?source=pa&hl=en-IN"

    # Route 2: open the venue's Google Maps place page via browser-harness
    # and extract the GMR id from the page state.
    if os.path.exists(_BH):
        q = urllib.parse.quote(f"{venue} google maps")
        script = (
            'import time, json\n'
            f'new_tab("https://www.google.com/search?q={q}&hl=en")\n'
            'wait_for_load()\n'
            'time.sleep(2)\n'
            'r = cdp("Runtime.evaluate", expression="""'
            '(function(){'
            '  var anchors = Array.from(document.querySelectorAll(\'a[href]\'));'
            '  var maps = anchors.map(function(a){return a.href||\"\";}).filter(function(u){'
            '    return u.indexOf(\"google.com/maps/place/\") >= 0 ||'
            '           u.indexOf(\"google.com/maps/reserve/\") >= 0;'
            '  });'
            '  return JSON.stringify(maps.slice(0, 5));'
            '})()'
            '""", returnByValue=True)\n'
            'print("---MAPS---")\n'
            'print(r.get("result",{}).get("value","[]"))\n'
        )
        try:
            out = subprocess.run([_BH], input=script, capture_output=True,
                                 text=True, timeout=20)
            tail = out.stdout.split("---MAPS---", 1)[-1].strip()
            urls = json.loads(tail.splitlines()[0]) if tail.startswith("[") else []
            for u in urls:
                m = re.search(r"/maps/reserve/v/dine/c/[A-Za-z0-9_-]+", u)
                if m:
                    return f"https://www.google.com{m.group(0)}?source=pa&hl=en-IN"
        except Exception as e:
            log(f"gmr maps-page scan failed: {e}")
    return None


def find_district_url(venue: str, date_iso: str | None, party: int | None) -> str | None:
    """Find a District (district.in/dining) booking URL."""
    candidates = _google_search(f"{venue} district book") + \
                 _google_search(f"site:district.in {venue}")
    for u in candidates:
        u = _strip_fragment(u)
        if "district.in/dining" not in u:
            continue
        if not _url_matches_venue(u, venue):
            continue
        if "/book" not in u:
            u = u.rstrip("/") + "/book"
        base = u.split("?")[0]
        params = []
        if date_iso:
            params.append(f"date={date_iso}")
        if party:
            params.append(f"guests={party}")
        return base + ("?" + "&".join(params) if params else "")
    return None


def find_zomato_url(venue: str) -> str | None:
    candidates = _google_search(f"{venue} zomato book a table") + \
                 _google_search(f"site:zomato.com {venue}")
    for u in candidates:
        u = _strip_fragment(u)
        if "zomato.com" not in u or not _url_matches_venue(u, venue):
            continue
        if "/book" in u:
            return u
        if re.search(r"zomato\.com/[a-z-]+/[a-z0-9-]+/?$", u):
            return u.rstrip("/") + "/book"
    return None


def find_eazydiner_url(venue: str) -> str | None:
    candidates = _google_search(f"{venue} eazydiner book") + \
                 _google_search(f"site:eazydiner.com {venue}")
    for u in candidates:
        u = _strip_fragment(u)
        if "eazydiner.com" not in u or not _url_matches_venue(u, venue):
            continue
        if re.search(r"eazydiner\.com/(?:restaurants?/)?[a-z]+/[a-z0-9-]+", u):
            return u
    return None


def find_swiggy_url(venue: str) -> str | None:
    candidates = _google_search(f"{venue} swiggy dineout book") + \
                 _google_search(f"site:swiggy.com {venue} dineout")
    for u in candidates:
        u = _strip_fragment(u)
        if "swiggy.com" not in u or not _url_matches_venue(u, venue):
            continue
        if "dineout" in u:
            return u
    return None


def find_own_site_booking_url(venue: str) -> str | None:
    """Fallback: the restaurant's own website. Used when aggregators
    don't surface a booking URL — many venues link to EatApp / Resy /
    a phone number / a WhatsApp link on their homepage."""
    candidates = _google_search(f'"{venue}" official website')
    for u in candidates:
        u = _strip_fragment(u)
        if any(host in u for host in ("zomato.com", "swiggy.com", "eazydiner.com",
                                       "district.in", "google.com", "youtube.com",
                                       "instagram.com", "facebook.com", "wikipedia.org")):
            continue
        # Skip /about, /products, etc.
        if re.search(r"/(about|products|blog|news|press|careers|jobs)\b", u):
            continue
        if not _url_matches_venue(u, venue):
            continue
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
    env["target_time_unavailable"], env["available_slots"] = _detect_time_mismatch(env)
    return env


def _detect_time_mismatch(env: dict) -> tuple[bool, list[str]]:
    """Inspect step records for a `target_time_unavailable` time-slot
    attempt result. fill-form reports this when no slot is within
    ±5 min of the user's target — instead of silently picking the
    closest (which previously booked the wrong day), the orchestrator
    surfaces the available list back to the user."""
    for r in env.get("step_records", []):
        ts = r.get("time_slot_attempt") or {}
        if ts.get("reason") == "target_time_unavailable":
            return True, ts.get("available", [])
    return False, []


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


def _extract_intent(message: str) -> tuple[dict, str, dict | None]:
    """LLM-first intent extraction with regex fallback.

    Returns (legacy_intent_dict, source, full_intent_obj_dict_or_None).
    The legacy dict is what the rest of book-reservation.py expects;
    the full_intent dict carries modality + per-modality fields used
    by the generic_url branch.
    """
    if booking_intent is None:
        return parse_intent(message), "regex", None
    intent_obj, source = booking_intent.extract(message)
    legacy = {
        "venue": intent_obj.venue,
        "party": intent_obj.party,
        "time": intent_obj.time,
        "day": None,
        "date": intent_obj.date,
        "raw_message": message,
    }
    full = {
        "modality": intent_obj.modality,
        "confidence": intent_obj.confidence,
        "ambiguity_notes": list(intent_obj.ambiguity_notes),
        "platform_url": intent_obj.platform_url,
        "area": intent_obj.area,
        "datetime_iso": intent_obj.datetime_iso,
        "movie_title": intent_obj.movie_title,
        "theatre": intent_obj.theatre,
        "showtime": intent_obj.showtime,
        "seat_preference": intent_obj.seat_preference,
        "event_name": intent_obj.event_name,
        "ticket_tier": intent_obj.ticket_tier,
        "ticket_count": intent_obj.ticket_count,
        "payment_handling": intent_obj.payment_handling,
    }
    log(f"intent source={source} modality={full['modality']} confidence={full['confidence']:.2f} "
        f"venue={legacy['venue']!r} party={legacy['party']} time={legacy['time']!r} url={full['platform_url']!r}")
    return legacy, source, full


def _drive_generic_url(message: str, full_intent: dict, legacy_intent: dict) -> dict:
    """Drive a non-restaurant pasted booking URL (movie / event / theatre /
    generic form) directly via fill-form. The intent fields become the
    answers dict; fill-form's generic logic handles whatever form shape
    the page presents. ALWAYS stops before payment per the v1 design.
    """
    url = full_intent.get("platform_url")
    if not url:
        return _envelope(state="needs_clarification", intent=legacy_intent,
                         user_message="No booking URL detected. Paste the URL of the booking page and I'll drive it.")
    # Map the intent fields → answers fill-form will fuzzy-match against
    # whatever the page's form fields are called.
    answers = {**USER_DEFAULTS}
    if legacy_intent.get("party"):
        answers["Party size"] = str(legacy_intent["party"])
        answers["Guests"] = str(legacy_intent["party"])
        answers["Number of people"] = str(legacy_intent["party"])
    if legacy_intent.get("time"):
        answers["Time"] = legacy_intent["time"]
        answers["Showtime"] = legacy_intent["time"]
    if legacy_intent.get("date"):
        answers["Date"] = legacy_intent["date"]
    if full_intent.get("ticket_count"):
        answers["Tickets"] = str(full_intent["ticket_count"])
        answers["Number of tickets"] = str(full_intent["ticket_count"])
        answers["Quantity"] = str(full_intent["ticket_count"])
    if full_intent.get("ticket_tier"):
        answers["Ticket type"] = full_intent["ticket_tier"]
        answers["Tier"] = full_intent["ticket_tier"]
        answers["Category"] = full_intent["ticket_tier"]
    if full_intent.get("seat_preference") == "best_available":
        answers["Seat"] = "Best Available"
        answers["Seats"] = "Best Available"
    modality = full_intent.get("modality") or "generic_url"
    log(f"generic_url drive: modality={modality} url={url}")
    env = drive(url, legacy_intent, f"url_{modality}", max_steps=20)
    if env.get("confirmation_detected"):
        state_path = save_state(legacy_intent, env)
        return _envelope(state="completed", intent=legacy_intent, attempts=[env],
                         screenshot=env.get("screenshot"), state_file=state_path,
                         platform=f"url_{modality}", confirmation_detected=True,
                         user_message=f"✅ Booking confirmed via the URL you shared ({modality}).")
    if env.get("needs_otp"):
        state_path = save_state(legacy_intent, env)
        return _envelope(state="otp_pending", intent=legacy_intent, attempts=[env],
                         screenshot=env.get("screenshot"), state_file=state_path,
                         platform=f"url_{modality}",
                         user_message="OTP screen reached on the booking page. Paste the code you received.")
    filled = env.get("filled", []) or []
    if filled:
        state_path = save_state(legacy_intent, env)
        return _envelope(state="confirm_ready", intent=legacy_intent, attempts=[env],
                         screenshot=env.get("screenshot"), state_file=state_path,
                         platform=f"url_{modality}",
                         user_message=(f"📋 Drove the {modality} booking flow on the URL you shared. "
                                       f"Filled {len(filled)} fields — review the screenshot. "
                                       f"Reply 'confirm' to submit (will stop before payment), "
                                       f"or 'cancel' to abort."))
    return _envelope(state="failed", intent=legacy_intent, attempts=[env],
                     screenshot=env.get("screenshot"),
                     user_message=("Couldn't drive that URL — 0 form fields detected. "
                                   "The page may need login, be JS-only, or have an unsupported widget. "
                                   "Try opening it directly in your browser to complete the booking."))


def book(message: str) -> dict:
    legacy_intent, intent_source, full_intent = _extract_intent(message)
    intent = legacy_intent  # name keeps the rest of this function readable
    # Multi-modality short-circuit: if the LLM detected a non-restaurant
    # booking with a pasted URL (movie / event / theatre / generic form),
    # skip the restaurant platform list and drive the URL directly.
    # This is what unlocks "book me 2 tickets at <bookmyshow URL>" and
    # any theatre/event URL the user pastes, in Phase 1, without
    # platform-specific adapters.
    if full_intent is not None:
        modality = full_intent.get("modality")
        pinned_via_llm = full_intent.get("platform_url")
        if modality in ("movie", "event", "generic_url") and pinned_via_llm:
            return _drive_generic_url(message, full_intent, intent)
        if modality == "unclear" and full_intent.get("confidence", 1.0) < 0.5:
            asks = full_intent.get("ambiguity_notes") or []
            need = ", ".join(asks) if asks else "more details"
            return _envelope(state="needs_clarification", intent=intent,
                             user_message=(f"I couldn't pull enough from your message. "
                                           f"Missing: {need}. "
                                           f"Example: 'book Pump House for 2 tomorrow at 8pm', "
                                           f"or paste a booking URL."))
    # If the user pasted a platform URL, pin to that platform and skip
    # heuristic discovery for the others. This is how the user steers
    # away from cover-charge platforms toward a specific provider.
    pinned_url = _url_in_message(message, _GMR_URL_RE)
    pinned_platform = None
    if pinned_url:
        pinned_platform = "gmr"
    elif (u := _url_in_message(message, _DISTRICT_URL_RE)):
        pinned_url, pinned_platform = u, "district"
    elif (u := _url_in_message(message, _ZOMATO_URL_RE)):
        pinned_url, pinned_platform = u, "zomato"
    elif (u := _url_in_message(message, _EAZYDINER_URL_RE)):
        pinned_url, pinned_platform = u, "eazydiner"
    elif (u := _url_in_message(message, _SWIGGY_URL_RE)):
        pinned_url, pinned_platform = u, "swiggy"
    if not intent.get("venue") and pinned_url:
        # User pinned a URL without naming the venue. Use the URL host
        # / path as a synthetic venue so downstream code doesn't bail.
        intent["venue"] = pinned_platform + "_url_supplied"
    if not intent.get("venue"):
        return _envelope(state="needs_clarification", intent=intent,
                         user_message="Booking intent detected, but I couldn't pull the venue name out of your message. Which restaurant?")
    if not intent.get("party"):
        return _envelope(state="needs_clarification", intent=intent,
                         user_message=f"Got '{intent['venue']}' — how many people?")

    platforms = [
        ("gmr",       find_gmr_url,       lambda url: drive(url, intent, "gmr", max_steps=28)),
        ("district",  lambda v: find_district_url(v, intent.get("date"), intent.get("party")),
                                                  lambda url: drive(url, intent, "district", max_steps=14)),
        ("zomato",    find_zomato_url,    lambda url: drive(url, intent, "zomato", max_steps=14)),
        ("eazydiner", find_eazydiner_url, lambda url: drive(url, intent, "eazydiner", max_steps=14)),
        ("swiggy",    find_swiggy_url,    lambda url: drive(url, intent, "swiggy", max_steps=14)),
        ("own_site",  find_own_site_booking_url, lambda url: drive(url, intent, "own_site", max_steps=10)),
    ]
    # If the user pinned a platform URL, reorder so that platform runs
    # first AND skip the others on success (single-platform retry).
    if pinned_platform:
        log(f"pinned platform: {pinned_platform} url={pinned_url}")
        steps_map = {"gmr": 28, "district": 14, "zomato": 14, "eazydiner": 14, "swiggy": 14}
        pinned_max = steps_map.get(pinned_platform, 14)
        platforms = [(pinned_platform, lambda v: pinned_url,
                      lambda url: drive(url, intent, pinned_platform, max_steps=pinned_max))]

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
        # Target time isn't in the slot grid for this platform — surface
        # the available slots to the user BEFORE silently picking the
        # wrong day or a closest-but-wrong slot.
        if env.get("target_time_unavailable"):
            state_path = save_state(intent, env)
            slots = env.get("available_slots") or []
            slots_str = ", ".join(slots[:12]) if slots else "(none)"
            return _envelope(state="needs_time_choice", intent=intent, attempts=attempts,
                             screenshot=env.get("screenshot"), state_file=state_path,
                             platform=name, available_slots=slots,
                             user_message=(
                                 f"📅 {intent['venue']} via {name.upper()}: your requested time "
                                 f"({intent.get('time')}) isn't in the slot grid right now. "
                                 f"Available: {slots_str}.\n\n"
                                 f"Reply with the slot you want (e.g. \"9:30 PM\" or \"10 PM\"), "
                                 f"or \"try another platform\" to fall through."))
        if env.get("needs_otp"):
            state_path = save_state(intent, env)
            return _envelope(state="otp_pending", intent=intent, attempts=attempts,
                             screenshot=env.get("screenshot"), state_file=state_path,
                             platform=name,
                             user_message=(f"📅 {intent['venue']} via {name.upper()}: OTP screen reached. "
                                           f"Paste the code you received and I'll continue."))
        if env.get("confirmation_detected"):
            # Some flows land on the confirmation page during the
            # initial book() drive (rare, but possible — e.g. when the
            # user pinned a URL whose state was already past Book).
            state_path = save_state(intent, env)
            return _envelope(state="completed", intent=intent, attempts=attempts,
                             screenshot=env.get("screenshot"), state_file=state_path,
                             platform=name, confirmation_detected=True,
                             confirmation_marker=env.get("confirmation_marker"),
                             user_message=(
                                 f"✅ Reservation confirmed at {intent['venue']} — "
                                 f"{intent.get('time') or 'requested time'} on "
                                 f"{intent.get('date') or 'requested date'}, party of "
                                 f"{intent.get('party')}. Confirmation page detected via {name.upper()}."))
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


def pick_time(new_time: str) -> dict:
    """User picked a time from the offered slot list. Re-drive the same
    platform with the new time. The platform's GMR URL stays the same;
    only the answers' Time changes."""
    state = load_latest_state()
    if not state:
        return _envelope(state="no_state", user_message="No paused booking found to retry.")
    env = state.get("last_envelope", {})
    url = env.get("url")
    platform = env.get("platform", "?")
    intent = state.get("intent", {})
    if not url:
        return _envelope(state="no_state", user_message="Paused booking found but no URL.")
    intent = dict(intent)
    intent["time"] = new_time
    log(f"retrying {platform} with new time: {new_time}")
    env2 = drive(url, intent, platform, max_steps=18)
    filled = env2.get("filled", []) or []
    if env2.get("target_time_unavailable"):
        slots = env2.get("available_slots") or []
        slots_str = ", ".join(slots[:12]) if slots else "(none)"
        state_path = save_state(intent, env2)
        return _envelope(state="needs_time_choice", intent=intent,
                         screenshot=env2.get("screenshot"), state_file=state_path,
                         platform=platform, available_slots=slots,
                         user_message=(
                             f"📅 {intent.get('venue')} via {platform.upper()}: '{new_time}' "
                             f"still isn't available. Slots: {slots_str}. Reply with one of these "
                             f"or 'try another platform'."))
    if env2.get("needs_otp"):
        state_path = save_state(intent, env2)
        return _envelope(state="otp_pending", intent=intent,
                         screenshot=env2.get("screenshot"), state_file=state_path,
                         platform=platform,
                         user_message=(f"📅 {intent.get('venue')} via {platform.upper()}: OTP screen "
                                       f"reached. Paste the code to continue."))
    if filled and not env2.get("errors"):
        state_path = save_state(intent, env2)
        return _envelope(state="confirm_ready", intent=intent,
                         screenshot=env2.get("screenshot"), state_file=state_path,
                         platform=platform,
                         user_message=(f"📅 {intent.get('venue')} via {platform.upper()} at {new_time}: "
                                       f"review the screenshot. Reply 'confirm' to submit, or 'cancel'."))
    return _envelope(state="failed", attempts=[env2],
                     user_message=(f"Retry on {platform} with time {new_time} didn't fill the form. "
                                   f"Try another platform or another time."))


def submit() -> dict:
    """User said 'confirm'. Re-open the booking URL, re-fill (idempotent
    via existing answers), and explicitly click the Continue / Submit
    button so the booking actually advances to the next step (auth /
    OTP / final confirmation). Pass --submit to fill-form so it doesn't
    just stop after fill but runs the submit sequence too."""
    state = load_latest_state()
    if not state:
        return _envelope(state="no_state",
                         user_message="No paused booking to confirm. Start a new booking.")
    env = state.get("last_envelope", {})
    url = env.get("url")
    platform = env.get("platform", "?")
    intent = state.get("intent", {})
    if not url:
        return _envelope(state="no_state",
                         user_message="Paused booking found but no URL to confirm.")
    answers = _answers_for(intent)
    answers_path = f"/tmp/booking-submit-{int(time.time())}.json"
    Path(answers_path).write_text(json.dumps(answers, indent=2))
    shot_path = f"/tmp/booking-submit-{platform}-{int(time.time())}.png"
    cmd = ["python3", FILL_FORM, url, answers_path,
           "--max-steps", "32",
           "--screenshot", shot_path,
           "--submit"]
    log(f"submitting {platform}")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=360)
    except subprocess.TimeoutExpired:
        return _envelope(state="failed", user_message="Submit step timed out.")
    last = (out.stdout.strip().splitlines() or [""])[-1]
    try:
        env2 = json.loads(last)
    except json.JSONDecodeError:
        return _envelope(state="failed", user_message="Submit produced no envelope.")
    env2["platform"] = platform
    # Hard-stop: if any time-slot attempt returned target_time_unavailable
    # the platform's slot grid doesn't have the user's time. Refuse to
    # submit (otherwise the booking commits the default-selected slot,
    # which is NOT what the user asked for). Surface alternatives.
    tu, slots = _detect_time_mismatch(env2)
    if tu:
        save_state(intent, env2)
        slots_str = ", ".join(slots[:12]) if slots else "(none)"
        return _envelope(state="needs_time_choice", intent=intent, platform=platform,
                         screenshot=env2.get("screenshot"), available_slots=slots,
                         user_message=(
                             f"⚠️ {intent.get('venue')} on {platform.upper()}: target "
                             f"{intent.get('time')} on {intent.get('date')} is not in the "
                             f"slot grid. Available: {slots_str}. Reply with one of these "
                             f"times to book, or 'try another platform'."))
    # Detect OTP after submit (the most common next stage)
    if _detect_otp_screen(env2):
        save_state(intent, env2)
        return _envelope(state="otp_pending", intent=intent, platform=platform,
                         screenshot=env2.get("screenshot"),
                         user_message=(f"📅 {intent.get('venue')} submitted. "
                                       f"OTP screen — paste the code you received."))
    # Ground truth: a confirmation page was detected by the DOM scan.
    # submitted==True alone is not enough — the click can succeed while
    # the provider rejects the booking later. Only "Reservation
    # confirmed" / equivalent marker text means it actually went through.
    if env2.get("confirmation_detected"):
        return _envelope(state="completed", intent=intent, platform=platform,
                         screenshot=env2.get("screenshot"),
                         confirmation_detected=True,
                         confirmation_marker=env2.get("confirmation_marker"),
                         user_message=(
                             f"✅ Reservation confirmed at {intent.get('venue')} — "
                             f"{intent.get('time')} on {intent.get('date')}, party of "
                             f"{intent.get('party')}. Booked via {platform.upper()}. "
                             f"Confirmation email should arrive shortly."))
    if env2.get("submitted"):
        # Book click landed but no confirmation marker yet. Could be a
        # slow provider round-trip OR a backend rejection. Surface as
        # confirm_ready (not completed) so the bot doesn't claim success
        # falsely.
        return _envelope(state="confirm_ready", intent=intent, platform=platform,
                         screenshot=env2.get("screenshot"),
                         user_message=(
                             f"📅 {intent.get('venue')} — Book clicked but no confirmation "
                             f"page detected yet. Screenshot is the post-click state. "
                             f"If you see 'Reservation confirmed', the booking succeeded. "
                             f"If you see an error, reply 'try another platform'."))
    return _envelope(state="confirm_ready", intent=intent, platform=platform,
                     screenshot=env2.get("screenshot"),
                     user_message=(f"📅 {intent.get('venue')} — submit attempted but no "
                                   f"confirmation detected. Check the screenshot; the next "
                                   f"step may be sign-in or OTP."))


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
    cmd = ["python3", FILL_FORM, url, answers_path, "--max-steps", "16", "--screenshot", shot_path]
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
        "skill": "book-anything",
        **kwargs,
    }


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit('Usage: book-reservation.py "<message>" | --resume "<otp>" | --pick-time "<time>" | --smoke')
    if sys.argv[1] == "--smoke":
        print(json.dumps({"ok": True, "skill": "book-anything", "user_message": "book-anything smoke ok"}))
        return
    if sys.argv[1] == "--pick-time":
        if len(sys.argv) < 3:
            sys.exit("Usage: book-reservation.py --pick-time <time>")
        env = pick_time(sys.argv[2])
        print(json.dumps(env))
        return
    if sys.argv[1] == "--submit":
        env = submit()
        print(json.dumps(env))
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
