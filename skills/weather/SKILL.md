---
name: weather
version: 1
script: infra/weather-snapshot.py
mutating: false
cron: null
eval: skills/weather/eval.yaml
last_audited: 2026-05-08
description: |
  On-demand weather query for Bangalore. The user asks for current
  conditions, forecast, rain probability, AQI, or "should I take an
  umbrella". Same backing script as the morning briefing's weather
  section, called interactively.
  WHEN: "weather", "forecast", "is it going to rain", "what's the temp
  outside", "AQI today", "umbrella?", "is the air bad", or any direct
  weather/air-quality question.
  NOT WHEN: User mentions weather as context for a different question
  ("it's hot, my run got cut short" → activity-tracker for the run, not
  weather). NOT WHEN they ask about a city other than Bangalore (script
  is hard-coded to Bangalore lat/lon for v1).
trigger: |
  - "weather" / "weather today" / "weather right now"
  - "what's the temp outside" / "temperature in Bangalore"
  - "forecast" / "next 24 hours" / "is it going to rain"
  - "AQI" / "air quality" / "is the air bad"
  - "umbrella?" / "should I take an umbrella"
  - "weather snapshot"
writes_to: []
brain_dirs_read: []
---

# /weather

Single source of truth: `infra/weather-snapshot.py` (deployed to
`~/bin/`). Backed by OpenWeatherMap free-tier endpoints (`/weather`,
`/forecast`, `/air_pollution`).

## How to invoke

```bash
~/bin/weather-snapshot.py
```

No arguments. Output is bullet-formatted markdown — current conditions
+ next-24h forecast highlights + AQI. Designed for the morning
briefing; works equally well for on-demand queries.

## Resolver patterns

| User intent | Command |
|---|---|
| any weather / forecast / AQI / rain / umbrella query | `python3 ~/bin/weather-snapshot.py` |

## Telegram reply

Relay stdout verbatim. The script formats for Telegram bullets — no
post-processing needed. Don't add commentary about API costs, rate
limits, or model choice (this is a deterministic API call, no LLM).

## Architectural rules

- **City is hard-coded to Bangalore** (`LAT=12.9716, LON=77.5946`). If
  the user asks about another city, surface the limitation and skip —
  don't fake a number.
- **UV index is unavailable** on the free tier (paid One Call 3.0
  required). Don't invent UV — script omits the line.
- **No caching**. Every call hits OpenWeatherMap fresh. The script is
  cheap (~3 API calls, well under the 1000/day free quota).

## Smoke

The script supports `--smoke` (no API call, no vault read).
