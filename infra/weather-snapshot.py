#!/usr/bin/env python3
# Weather snapshot for Bangalore via OpenWeatherMap free-tier endpoints.
# - /data/2.5/weather       → current temp + conditions
# - /data/2.5/forecast       → next 24h, 3-hour granularity (rain prob + timing)
# - /data/2.5/air_pollution  → AQI (1-5 scale) + PM2.5/PM10
#
# UV index is omitted: requires the paid One Call 3.0 subscription.
# Free-tier UV endpoint /data/2.5/uvi was retired in 2022.
#
# Output: bullet-formatted markdown for the morning briefing.
# Stdout = the section text. Stderr = diagnostic logs.

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

LAT = 12.9716
LON = 77.5946
CITY = "Bangalore"
IST = timezone(timedelta(hours=5, minutes=30))

AQI_LABEL = {1: "Good", 2: "Fair", 3: "Moderate", 4: "Poor", 5: "Very Poor"}


def log(msg):
    print(f"[weather] {msg}", file=sys.stderr)


def vault_get(key):
    return subprocess.run(["bot-cred", "get", key], capture_output=True, text=True, check=True).stdout.strip()


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Alfred/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--smoke":
        # Cold-boot callability check. No network, no vault, no API.
        print(json.dumps({"ok": True, "skill": "weather", "user_message": "weather smoke ok"}))
        return
    key = vault_get("WEATHER_API_KEY")
    base = "https://api.openweathermap.org/data/2.5"

    current = fetch(f"{base}/weather?lat={LAT}&lon={LON}&appid={key}&units=metric")
    forecast = fetch(f"{base}/forecast?lat={LAT}&lon={LON}&appid={key}&units=metric&cnt=10")

    # Current
    temp_now = current["main"]["temp"]
    conditions = current["weather"][0]["description"]

    # Today's high/low from forecast slots within the next 24h
    today = datetime.now(IST).date()
    today_slots = [
        s for s in forecast["list"]
        if datetime.fromtimestamp(s["dt"], IST).date() == today
    ]
    if today_slots:
        # Include current temp in the day's high/low — forecast slots only cover
        # rest-of-day, so we need the current observation to capture peaks
        # already passed (or yet to come if early morning).
        highs = [s["main"]["temp_max"] for s in today_slots] + [current["main"]["temp"], current["main"]["temp_max"]]
        lows = [s["main"]["temp_min"] for s in today_slots] + [current["main"]["temp"], current["main"]["temp_min"]]
        high = max(highs)
        low = min(lows)
    else:
        high, low = current["main"]["temp_max"], current["main"]["temp_min"]

    # Rain windows: any 3-hour slot today with pop ≥ 30%
    rain_windows = []
    for s in today_slots:
        pop = int(s.get("pop", 0) * 100)
        if pop < 30:
            continue
        dt = datetime.fromtimestamp(s["dt"], IST)
        amt = (s.get("rain") or {}).get("3h", 0)
        rain_windows.append({"start": dt, "pop": pop, "mm": amt, "desc": s["weather"][0]["description"]})

    # Compose
    date_str = today.strftime("%a %d %b")
    lines = [f"🌦️ {CITY} — {date_str}"]
    lines.append(f"• Now {temp_now:.0f}°C, {conditions}; high {high:.0f}°C / low {low:.0f}°C today")
    if rain_windows:
        first = rain_windows[0]
        last = rain_windows[-1]
        if first is last:
            lines.append(f"• Rain {first['pop']}% around {first['start'].strftime('%H:%M')} ({first['desc']})")
        else:
            lines.append(f"• Rain expected {first['start'].strftime('%H:%M')}–{last['start'].strftime('%H:%M')}, peak {max(w['pop'] for w in rain_windows)}%")
    else:
        lines.append("• No significant rain expected today")

    print("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {e}")
        print(f"🌦️ {CITY} — weather unavailable ({e})")
        sys.exit(1)
