#!/usr/bin/env python3
"""booking_intent.py — LLM-first extractor for any booking request.

Replaces the regex-based parse_intent() in the old book-reservation.py.
Returns a typed BookingIntent with modality + per-modality fields.

Provider chain:
  1. Kimi K2.6 via Moonshot OpenAI-compatible API (primary, per user pref)
  2. llm.call(tier="mini") -> OpenAI gpt-5.4-mini -> Anthropic claude-haiku-4-5
  3. regex fallback (~/bin/book_intent_regex_fallback) — preserves the
     legacy parse_intent behavior so a totally offline LLM-stack still
     returns *something* sensible

Why this lives in its own file: the orchestrator stays small + every
adapter (restaurant, movie, event, generic_url) consumes the same
BookingIntent type. Adding a new adapter never requires touching the
extractor — only the dispatch map in book-anything.py.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

_OPENCLAW_JSON = Path.home() / ".openclaw" / "openclaw.json"
_MOONSHOT_URL = "https://api.moonshot.ai/v1/chat/completions"
_MOONSHOT_MODEL = "kimi-k2.6"
_IST = timezone(timedelta(hours=5, minutes=30))


# ── Typed intent ─────────────────────────────────────────────────────


Modality = Literal["restaurant", "movie", "event", "generic_url", "unclear"]


@dataclass
class BookingIntent:
    """Typed booking intent. Per-modality fields are nullable —
    the consumer (adapter) decides what's required.
    """
    modality: Modality
    confidence: float                          # 0.0 .. 1.0
    ambiguity_notes: list[str] = field(default_factory=list)
    raw_message: str = ""
    # URL pinned by the user (any platform) — when present, the adapter
    # dispatcher prefers the URL-host-matching adapter over modality.
    platform_url: str | None = None

    # Restaurant
    venue: str | None = None
    area: str | None = None                    # e.g. "JP Nagar", "Forum Rex Walk"
    party: int | None = None
    datetime_iso: str | None = None            # ISO 8601 with TZ — preferred
    date: str | None = None                    # YYYY-MM-DD (legacy compat)
    time: str | None = None                    # "9:00 PM" (legacy compat)

    # Movie
    movie_title: str | None = None
    theatre: str | None = None
    showtime: str | None = None
    seat_preference: Literal["best_available", "specific", "any"] | None = None

    # Event / theatre
    event_name: str | None = None
    event_date: str | None = None
    ticket_tier: str | None = None
    ticket_count: int | None = None

    # Payment posture for the adapter — v1 default is to stop before payment
    payment_handling: Literal["stop_before", "user_app_handoff", "in_chat_relay"] = "stop_before"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


# ── Kimi K2.6 caller (primary) ───────────────────────────────────────


def _moonshot_key() -> str:
    """Read Kimi API key from openclaw.json — the same place the agent
    runtime reads it from. No separate vault entry needed."""
    if not _OPENCLAW_JSON.exists():
        raise RuntimeError(f"missing {_OPENCLAW_JSON}")
    cfg = json.loads(_OPENCLAW_JSON.read_text())
    # Walk the known path: providers / moonshot / apiKey
    for path in (
        ["providers", "moonshot", "apiKey"],
        ["models", "providers", "moonshot", "apiKey"],
        ["modelProviders", "moonshot", "apiKey"],
    ):
        cur: Any = cfg
        ok = True
        for k in path:
            if not isinstance(cur, dict) or k not in cur:
                ok = False
                break
            cur = cur[k]
        if ok and isinstance(cur, str) and cur:
            return cur
    # Last-resort: scan the whole config for "moonshot" → "apiKey"
    def walk(node: Any) -> str | None:
        if isinstance(node, dict):
            if "moonshot" in node and isinstance(node["moonshot"], dict):
                ak = node["moonshot"].get("apiKey")
                if isinstance(ak, str) and ak:
                    return ak
            for v in node.values():
                r = walk(v)
                if r:
                    return r
        return None
    key = walk(cfg)
    if not key:
        raise RuntimeError("kimi apiKey not found in openclaw.json")
    return key


def _call_kimi(system: str, user: str, *, timeout: int = 30) -> str:
    key = _moonshot_key()
    payload = {
        "model": _MOONSHOT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        # Kimi K2.6 rejects any temperature other than 1 (HTTP 400). The
        # JSON response_format constraint + the few-shot examples in the
        # system prompt give us enough determinism without lowering temp.
        "temperature": 1,
        "response_format": {"type": "json_object"},
        "max_tokens": 1500,
    }
    req = urllib.request.Request(
        _MOONSHOT_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode())
    return body["choices"][0]["message"]["content"]


# ── Fallback caller via existing ~/bin/llm.py ────────────────────────


def _call_llm_fallback(system: str, user: str) -> str:
    """OpenAI gpt-5.4-mini → Anthropic claude-haiku-4-5 fallback chain.
    Uses the project's standard llm.py helper so we don't duplicate
    HTTP/auth logic."""
    sys.path.insert(0, os.path.expanduser("~/bin"))
    try:
        import llm  # type: ignore
    finally:
        if os.path.expanduser("~/bin") in sys.path:
            sys.path.remove(os.path.expanduser("~/bin"))
    text, _provider = llm.call(system, user, tier="mini", max_tokens=1500)
    return text


# ── System prompt + few-shot ─────────────────────────────────────────


def _system_prompt() -> str:
    today_ist = datetime.now(_IST).date().isoformat()
    today_weekday = datetime.now(_IST).strftime("%A")
    return f"""You are a booking-intent extractor for a personal-assistant Telegram bot.

Input: a free-form user message asking the bot to book something — a restaurant table, a movie ticket, an event/theatre ticket, or to fill an arbitrary booking URL.

Output: a single JSON object matching this exact schema. Return ONLY the JSON object, no prose, no markdown fence.

{{
  "modality": "restaurant" | "movie" | "event" | "generic_url" | "unclear",
  "confidence": float between 0.0 and 1.0,
  "ambiguity_notes": [strings — anything you guessed at; one per field],
  "platform_url": string or null (URL the user pasted),
  "venue": string or null (restaurant name — NEVER include the area/landmark in this field; NEVER set to generic words like "a table" or "table"),
  "area": string or null (neighborhood / landmark / mall — e.g. "JP Nagar", "Forum Rex Walk"),
  "party": int or null,
  "datetime_iso": string or null (ISO 8601 with +05:30 IST offset, resolved against today={today_ist} ({today_weekday})),
  "date": string or null (YYYY-MM-DD),
  "time": string or null ("9:00 PM"),
  "movie_title": string or null,
  "theatre": string or null,
  "showtime": string or null,
  "seat_preference": "best_available" | "specific" | "any" | null,
  "event_name": string or null,
  "theatre" is also reused for event venue,
  "ticket_tier": string or null ("Gold", "VIP", etc),
  "ticket_count": int or null,
  "payment_handling": "stop_before" | "user_app_handoff" | "in_chat_relay" (default "stop_before")
}}

Rules:
- "venue" is the restaurant brand name only. "Foo, Forum Rex Walk, Bangalore" → venue="Foo", area="Forum Rex Walk", city="Bangalore" (drop city, it's implicit).
- NEVER set venue to "a table", "table", "dinner", or any generic word. Those are sentence noise, not the restaurant.
- "tonight" = today after 6pm. If now() is past 11pm and user says "tonight", set ambiguity_notes and pick today.
- "tomorrow" / "tomorrow night" → date = today + 1 day.
- If date is missing → assume today, add a note.
- If party is missing for a restaurant → leave null, the orchestrator will ask.
- If only a URL is pasted with "book this", set modality="generic_url" and platform_url.
- BookMyShow URLs → modality="movie" or "event" (look at the URL path).
- District event URLs → modality="event".
- District restaurant URLs (district.in/dining/...) → modality="restaurant".
- Confidence < 0.7 means the orchestrator will ask the user to confirm.
- modality="unclear" when there's no actionable signal (e.g. just "book a table").

Examples:

INPUT: please book a table for today - 8:30PM - 2 people - Foo, Forum Rex Walk, Bangalore
OUTPUT: {{"modality":"restaurant","confidence":0.95,"ambiguity_notes":[],"platform_url":null,"venue":"Foo","area":"Forum Rex Walk","party":2,"datetime_iso":"{today_ist}T20:30:00+05:30","date":"{today_ist}","time":"8:30 PM","movie_title":null,"theatre":null,"showtime":null,"seat_preference":null,"event_name":null,"ticket_tier":null,"ticket_count":null,"payment_handling":"stop_before"}}

INPUT: Foo, Forum Rex Walk, Bangalore, today 8:30 PM, 2 people
OUTPUT: {{"modality":"restaurant","confidence":0.95,"ambiguity_notes":[],"platform_url":null,"venue":"Foo","area":"Forum Rex Walk","party":2,"datetime_iso":"{today_ist}T20:30:00+05:30","date":"{today_ist}","time":"8:30 PM","movie_title":null,"theatre":null,"showtime":null,"seat_preference":null,"event_name":null,"ticket_tier":null,"ticket_count":null,"payment_handling":"stop_before"}}

INPUT: book Uru Brewpark for 2 tonight at 10pm
OUTPUT: {{"modality":"restaurant","confidence":0.92,"ambiguity_notes":[],"platform_url":null,"venue":"Uru Brewpark","area":null,"party":2,"datetime_iso":"{today_ist}T22:00:00+05:30","date":"{today_ist}","time":"10:00 PM","movie_title":null,"theatre":null,"showtime":null,"seat_preference":null,"event_name":null,"ticket_tier":null,"ticket_count":null,"payment_handling":"stop_before"}}

INPUT: book 2 tickets for Avengers tonight 9pm at PVR Forum
OUTPUT: {{"modality":"movie","confidence":0.9,"ambiguity_notes":[],"platform_url":null,"venue":null,"area":null,"party":null,"datetime_iso":"{today_ist}T21:00:00+05:30","date":"{today_ist}","time":"9:00 PM","movie_title":"Avengers","theatre":"PVR Forum","showtime":"9:00 PM","seat_preference":"best_available","event_name":null,"ticket_tier":null,"ticket_count":2,"payment_handling":"stop_before"}}

INPUT: https://in.bookmyshow.com/movies/bengaluru/inception/ET00xxxxx book 2 tickets
OUTPUT: {{"modality":"movie","confidence":0.95,"ambiguity_notes":["showtime not specified"],"platform_url":"https://in.bookmyshow.com/movies/bengaluru/inception/ET00xxxxx","venue":null,"area":null,"party":null,"datetime_iso":null,"date":null,"time":null,"movie_title":"Inception","theatre":null,"showtime":null,"seat_preference":"best_available","event_name":null,"ticket_tier":null,"ticket_count":2,"payment_handling":"stop_before"}}

INPUT: https://district.in/events/some-comedy-show book me 1 gold
OUTPUT: {{"modality":"event","confidence":0.9,"ambiguity_notes":[],"platform_url":"https://district.in/events/some-comedy-show","venue":null,"area":null,"party":null,"datetime_iso":null,"date":null,"time":null,"movie_title":null,"theatre":null,"showtime":null,"seat_preference":null,"event_name":"some-comedy-show","ticket_tier":"Gold","ticket_count":1,"payment_handling":"stop_before"}}

INPUT: book a table
OUTPUT: {{"modality":"unclear","confidence":0.2,"ambiguity_notes":["venue missing","time missing","party missing"],"platform_url":null,"venue":null,"area":null,"party":null,"datetime_iso":null,"date":null,"time":null,"movie_title":null,"theatre":null,"showtime":null,"seat_preference":null,"event_name":null,"ticket_tier":null,"ticket_count":null,"payment_handling":"stop_before"}}

INPUT: 8:30pm for 2 people
OUTPUT: {{"modality":"unclear","confidence":0.3,"ambiguity_notes":["venue missing","date missing"],"platform_url":null,"venue":null,"area":null,"party":2,"datetime_iso":null,"date":null,"time":"8:30 PM","movie_title":null,"theatre":null,"showtime":null,"seat_preference":null,"event_name":null,"ticket_tier":null,"ticket_count":null,"payment_handling":"stop_before"}}
"""


# ── Regex fallback (preserves the legacy parse_intent) ────────────────


_STOP = (r"in|for|tonight|tomorrow|today|this|next|on|at|by|from|till|until|around|near|after|before")
_VENUE_AT = re.compile(rf"(?:\b(?:at|@)\s+)([A-Z][A-Za-z0-9'&.\- ,]*?)(?=\s+(?:{_STOP})\b|\s+\d|[?!.]|$)", re.I)
_VENUE_VERB = re.compile(rf"\b(?:book|reserve|reservation\s+at)\s+(?!a\s+table|the\s+table)([A-Z][A-Za-z0-9'&.\- ,]*?)(?=\s+(?:{_STOP})\b|\s+\d|[?!.]|$)", re.I)
_PARTY = re.compile(r"\bfor\s+(\d{1,2})\s*(?:people|guests|of\s+us|persons|adults|pax)?\b", re.I)
_PARTY_OF = re.compile(r"\b(?:party\s+of|table\s+for|table\s+of)\s+(\d{1,2})\b", re.I)
_TIME = re.compile(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm|AM|PM))\b")
_DAY = re.compile(r"\b(tonight|tomorrow|today|this\s+evening|tomorrow\s+evening|tomorrow\s+night)\b", re.I)
_URL = re.compile(r"https?://[^\s\"'<>]+")


def _regex_extract(msg: str) -> BookingIntent:
    venue = None
    m = _VENUE_AT.search(msg) or _VENUE_VERB.search(msg)
    if m:
        venue = m.group(1).strip().rstrip(",.").strip()
        if venue.lower() in ("a table", "table", "dinner", "lunch"):
            venue = None
    party = None
    for pat in (_PARTY, _PARTY_OF):
        mm = pat.search(msg)
        if mm:
            party = int(mm.group(1))
            break
    bt = None
    mm = _TIME.search(msg)
    if mm:
        bt = mm.group(1).strip().upper().replace(" ", "")
    day = None
    mm = _DAY.search(msg)
    if mm:
        day = mm.group(1).lower().split()[0]
    today = datetime.now(_IST).date()
    bd: str | None = None
    if day in (None, "tonight", "today"):
        bd = today.isoformat()
    elif day == "tomorrow":
        bd = (today + timedelta(days=1)).isoformat()
    url = None
    mm = _URL.search(msg)
    if mm:
        url = mm.group(0)
    modality: Modality = "unclear"
    if venue and (party or bt):
        modality = "restaurant"
    elif url:
        modality = "generic_url"
    notes = []
    if not venue:
        notes.append("venue missing")
    if not party:
        notes.append("party missing")
    if not bt:
        notes.append("time missing")
    return BookingIntent(
        modality=modality,
        confidence=0.4 if modality != "unclear" else 0.15,
        ambiguity_notes=notes,
        raw_message=msg,
        platform_url=url,
        venue=venue,
        party=party,
        date=bd,
        time=bt,
    )


# ── Public entry point ───────────────────────────────────────────────


def _validate(d: dict, raw: str) -> BookingIntent | None:
    try:
        # Drop unknown keys, normalize None
        keep = {f.name for f in BookingIntent.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        clean: dict[str, Any] = {k: v for k, v in d.items() if k in keep}
        clean.setdefault("raw_message", raw)
        clean.setdefault("ambiguity_notes", [])
        # Coerce types
        if "party" in clean and clean["party"] is not None:
            try:
                clean["party"] = int(clean["party"])
            except (ValueError, TypeError):
                clean["party"] = None
        if "ticket_count" in clean and clean["ticket_count"] is not None:
            try:
                clean["ticket_count"] = int(clean["ticket_count"])
            except (ValueError, TypeError):
                clean["ticket_count"] = None
        if "confidence" in clean:
            try:
                clean["confidence"] = float(clean["confidence"])
            except (ValueError, TypeError):
                clean["confidence"] = 0.5
        return BookingIntent(**clean)
    except Exception as e:
        print(f"[booking_intent] validation failed: {e}", file=sys.stderr)
        return None


def extract(msg: str) -> tuple[BookingIntent, str]:
    """Extract a BookingIntent from a free-form user message.

    Returns (intent, source) where source is "kimi" / "fallback_llm" / "regex".
    Never raises — always returns something the orchestrator can act on.
    """
    system = _system_prompt()
    user = msg.strip()
    # 1. Kimi K2.6 (primary)
    try:
        raw = _call_kimi(system, user, timeout=25)
        parsed = json.loads(raw)
        intent = _validate(parsed, msg)
        if intent:
            return intent, "kimi"
        print("[booking_intent] kimi returned unparseable JSON", file=sys.stderr)
    except Exception as e:
        print(f"[booking_intent] kimi failed: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
    # 2. Fallback LLM
    try:
        raw = _call_llm_fallback(system, user)
        # The fallback may wrap in ```json fences — strip
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.M)
        parsed = json.loads(cleaned)
        intent = _validate(parsed, msg)
        if intent:
            return intent, "fallback_llm"
        print("[booking_intent] fallback_llm returned unparseable JSON", file=sys.stderr)
    except Exception as e:
        print(f"[booking_intent] fallback_llm failed: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
    # 3. Regex (always works, lowest quality)
    return _regex_extract(msg), "regex"


# ── CLI for smoke / manual extraction ────────────────────────────────


def _main() -> None:
    if len(sys.argv) < 2:
        sys.exit('Usage: booking_intent.py "<message>"')
    if sys.argv[1] == "--smoke":
        cases = [
            "please book a table for today - 8:30PM - 2 people - Foo, Forum Rex Walk, Bangalore",
            "Foo, Forum Rex Walk, Bangalore, today 8:30 PM, 2 people",
            "book Uru Brewpark for 2 tonight at 10pm",
            "book a table",
            "https://in.bookmyshow.com/movies/bengaluru/inception/ET00xxxxx book 2 tickets",
        ]
        for c in cases:
            intent, src = extract(c)
            print(f"\n--- INPUT ({src}): {c}")
            print(intent.to_json())
        return
    intent, src = extract(sys.argv[1])
    print(json.dumps({"source": src, "intent": asdict(intent)}, indent=2))


if __name__ == "__main__":
    _main()
