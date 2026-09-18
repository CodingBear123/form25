"""
Fetch market data from yfinance.

Provides:
    - Current price, market cap
    - 52 week high/low and % from high
    - Short interest
    - Price change over 30 days
    - Volume vs 30 day average

yfinance is free, no API key needed.
Data is sourced from Yahoo Finance — suitable for daily scans,
not for intraday or tick-level data (use Polygon for that later).
"""

import math
import yfinance as yf
from typing import Optional


def _clean(val: Optional[float]) -> Optional[float]:
    """Return None if val is NaN or infinite, otherwise return val."""
    if val is None:
        return None
    try:
        return None if (math.isnan(val) or math.isinf(val)) else val
    except (TypeError, ValueError):
        return None


def fetch_price_data(ticker: str) -> dict:
    """
    Fetch market data for a ticker using yfinance.

    Returns dict with:
        ticker, company_name,
        current_price, market_cap,
        price_52w_high, price_52w_low, price_vs_52w_high_pct,
        short_interest_pct,
        price_change_30d_pct,
        volume_avg_30d, volume_today, volume_vs_avg,
        data_quality: "full" | "partial" | "insufficient"
        missing_fields: list
    """
    stock = yf.Ticker(ticker)
    info = stock.info
    missing: list[str] = []

    # --- Current price ---
    current_price = _clean(
        info.get("currentPrice")
        or info.get("regularMarketPrice")
        or info.get("previousClose")
    )
    if current_price is None:
        missing.append("current_price")

    # --- Market cap ---
    market_cap = _clean(info.get("marketCap"))
    if market_cap is None:
        missing.append("market_cap")

    # --- 52 week high/low ---
    high_52w = _clean(info.get("fiftyTwoWeekHigh"))
    low_52w = _clean(info.get("fiftyTwoWeekLow"))
    if high_52w is None:
        missing.append("price_52w_high")
    if low_52w is None:
        missing.append("price_52w_low")

    # % from 52w high — negative means below high
    if current_price and high_52w:
        price_vs_52w_high_pct = _clean((current_price - high_52w) / high_52w)
    else:
        price_vs_52w_high_pct = None
        missing.append("price_vs_52w_high_pct")

    # --- Short interest ---
    shares_short = info.get("sharesShort")
    float_shares = info.get("floatShares")
    if shares_short and float_shares and float_shares > 0:
        short_interest_pct = _clean(shares_short / float_shares)
    else:
        short_interest_pct = None
        missing.append("short_interest_pct")

    # --- 30 day price change + volume ---
    try:
        hist = stock.history(period="35d")
        if len(hist) >= 20:
            price_30d_ago = _clean(float(hist["Close"].iloc[0]))
            price_now = _clean(float(hist["Close"].iloc[-1]))

            if price_30d_ago and price_now and price_30d_ago > 0:
                price_change_30d_pct = _clean((price_now - price_30d_ago) / price_30d_ago)
            else:
                price_change_30d_pct = None
                missing.append("price_change_30d")

            volume_avg_30d = _clean(float(hist["Volume"].mean()))
            volume_today = _clean(float(hist["Volume"].iloc[-1]))
            if volume_avg_30d and volume_avg_30d > 0 and volume_today is not None:
                volume_vs_avg = _clean(volume_today / volume_avg_30d)
            else:
                volume_vs_avg = None
        else:
            price_change_30d_pct = None
            volume_avg_30d = None
            volume_today = None
            volume_vs_avg = None
            missing.append("price_change_30d")
    except Exception:
        price_change_30d_pct = None
        volume_avg_30d = None
        volume_today = None
        volume_vs_avg = None
        missing.append("price_history")

    # --- Data quality ---
    critical = {"current_price", "market_cap"}
    missing_critical = critical & set(missing)
    if not missing_critical:
        data_quality = "full" if not missing else "partial"
    else:
        data_quality = "insufficient"

    return {
        "ticker": ticker.upper(),
        "company_name": info.get("longName") or info.get("shortName", ""),
        "current_price": current_price,
        "market_cap": market_cap,
        "price_52w_high": high_52w,
        "price_52w_low": low_52w,
        "price_vs_52w_high_pct": price_vs_52w_high_pct,
        "short_interest_pct": short_interest_pct,
        "price_change_30d_pct": price_change_30d_pct,
        "volume_avg_30d": volume_avg_30d,
        "volume_today": volume_today,
        "volume_vs_avg": volume_vs_avg,
        "data_quality": data_quality,
        "missing_fields": missing,
    }
