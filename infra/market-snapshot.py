#!/usr/bin/env python3
# Market snapshot via Yahoo Finance public chart endpoint (no auth).
#
# Tickers:
#   ^NSEI   — NIFTY 50
#   ^GSPC   — S&P 500
#   ^IXIC   — NASDAQ Composite
#   GC=F    — Gold spot futures (USD/oz) — combined with INR=X to compute INR/10g
#   INR=X   — USD/INR FX rate (price = INR per 1 USD)
#
# Output: bullet-formatted markdown for the morning briefing.

import json
import sys
import urllib.error
import urllib.request

# Index tickers shown directly
INDEX_TICKERS = [
    ("^NSEI", "NIFTY 50"),
    ("^GSPC", "S&P 500"),
    ("^IXIC", "NASDAQ"),
]

# 1 troy ounce = 31.1035 grams
OZ_TO_GRAM = 31.1035


def log(msg):
    print(f"[market] {msg}", file=sys.stderr)


def fetch(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Alfred/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        meta = d["chart"]["result"][0]["meta"]
        close = meta["regularMarketPrice"]
        prev = meta["chartPreviousClose"]
        change_pct = (close - prev) / prev * 100 if prev else 0.0
        return {"close": close, "prev": prev, "change_pct": change_pct}
    except Exception as e:
        log(f"{symbol} fetch failed: {e}")
        return None


def fmt_change(pct):
    if pct > 0:
        return f"+{pct:.2f}% 🟢"
    if pct < 0:
        return f"{pct:.2f}% 🔴"
    return f"{pct:.2f}%"


def fmt_big_number(n):
    """5,892.42 -> '5,892' for >=1000, otherwise 1 decimal."""
    if abs(n) >= 1000:
        return f"{n:,.0f}"
    return f"{n:.2f}"


def main():
    lines = ["📈 Markets — last close"]

    # Indices
    for ticker, label in INDEX_TICKERS:
        data = fetch(ticker)
        if not data:
            lines.append(f"• {label}: unavailable")
            continue
        lines.append(f"• {label}: {fmt_big_number(data['close'])} ({fmt_change(data['change_pct'])})")

    # Gold (USD/oz) → INR per 10g
    gold_usd = fetch("GC=F")
    inr_rate = fetch("INR=X")
    if gold_usd and inr_rate:
        # Convert oz → 10g: per 10g = (USD/oz × INR/USD × 10) / 31.1035
        gold_inr_per_10g = (gold_usd["close"] * inr_rate["close"] * 10) / OZ_TO_GRAM
        # Compute % change combining both moves on the converted price
        prev_gold_per_10g = (gold_usd["prev"] * inr_rate["prev"] * 10) / OZ_TO_GRAM
        gold_change_pct = (gold_inr_per_10g - prev_gold_per_10g) / prev_gold_per_10g * 100 if prev_gold_per_10g else 0
        lines.append(f"• Gold: ₹{fmt_big_number(gold_inr_per_10g)}/10g ({fmt_change(gold_change_pct)})")
    else:
        lines.append("• Gold: unavailable")

    # INR vs USD (USD = X INR)
    if inr_rate:
        lines.append(f"• USD/INR: ₹{inr_rate['close']:.2f} ({fmt_change(inr_rate['change_pct'])})")
    else:
        lines.append("• USD/INR: unavailable")

    print("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {e}")
        print(f"📈 Markets — unavailable ({e})")
        sys.exit(1)
