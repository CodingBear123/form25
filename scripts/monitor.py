"""
Form25 daily monitor — refreshes macro data, runs the screener, and
regenerates the dashboard export.

The dashboard is the overview; this script keeps it current and prints a
run digest to stdout.

Designed to run as a daily cron job on a Raspberry Pi.

Usage:
    python scripts/monitor.py

Cron (runs every weekday at 7am, logging the digest):
    0 7 * * 1-5 cd /path/to/form25 && .venv/bin/python scripts/monitor.py >> logs/monitor.log 2>&1
"""

import sys
import os
import json
import sqlite3
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.config  # noqa: F401 — importing this loads the .env file

from data.fetch_macro import get_macro_on_date
from utils.config import FORM25_DB_PATH


# ---------------------------------------------------------------------------
# Regime state tracking — detect changes
# ---------------------------------------------------------------------------

_STATE_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'db', 'monitor_state.json'
)

def _load_state() -> dict:
    try:
        with open(_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    with open(_STATE_PATH, 'w') as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Get current universe (passing tickers)
# ---------------------------------------------------------------------------

def _get_passing_tickers(lookback_days: int = 3) -> list[dict]:
    """
    Pull the most recent snapshot per ticker that passes hard filters,
    within the last few trading days.
    """
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    conn   = sqlite3.connect(FORM25_DB_PATH)

    rows = conn.execute("""
        SELECT ticker, snapshot_date, data
        FROM snapshots
        WHERE sector = 'biotech' AND portfolio = 'value'
          AND snapshot_date >= ?
        ORDER BY snapshot_date DESC, ticker ASC
    """, (cutoff,)).fetchall()
    conn.close()

    seen    = set()
    passing = []
    for ticker, snap_date, data_json in rows:
        if ticker in seen:
            continue
        seen.add(ticker)
        try:
            data = json.loads(data_json)
        except Exception:
            continue
        if not data.get("passed_filters"):
            continue
        passing.append({
            "ticker":             ticker,
            "cash_ratio":         data.get("cash_ratio"),
            "cash_runway_months": data.get("cash_runway_months"),
            "vs_52w_high_pct":    data.get("price_vs_52w_high_pct"),
            "market_cap_m":       (data["market_cap"] / 1e6
                                   if data.get("market_cap") else None),
        })

    # No ranking: the hard filters are binary and the soft score that used to
    # order this list had no predictive value (rho = 0.008). Alphabetical is
    # honest about that; any other order would imply a conviction ordering
    # the evidence does not support.
    passing.sort(key=lambda x: x["ticker"])
    return passing


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

def _fmt(val, suffix="", decimals=1, prefix=""):
    if val is None:
        return "—"
    return f"{prefix}{val:.{decimals}f}{suffix}"


def build_message(macro: dict, passing: list[dict], prev_state: dict) -> str:
    """Plain-text run digest for stdout / the cron log.

    The dashboard is the overview; this is a record of what a given run saw.
    """
    today       = date.today().strftime("%b %d %Y")
    risk_env    = macro.get("risk_environment", "unknown")
    curve       = macro.get("yield_curve_shape", "unknown")
    regime      = macro.get("macro_regime", "unknown")
    vix         = macro.get("vix")
    hy          = macro.get("hy_spread")
    fed         = macro.get("fed_funds_rate")
    spread      = macro.get("yield_spread_10y2y")

    # Regime change detection
    prev_risk = prev_state.get("risk_environment")
    regime_changed = prev_risk and prev_risk != risk_env

    action = {
        "elevated": "DEPLOY SCREENER",
        "moderate": "WATCHLIST MODE",
        "low":      "SIT OUT",
    }.get(risk_env, "UNKNOWN")

    # Edge reminder by regime
    edge_note = {
        "elevated": "Historical edge: +5.17% mean / 53.1% win rate at 30d (post-COVID)",
        "moderate": "Edge not statistically significant in this regime",
        "low":      "No edge (p=0.23) — preserve capital, wait for elevated conditions",
    }.get(risk_env, "")

    spread_str = (f"{spread:+.2f}%" if spread is not None else "—")
    hy_str     = (f"{hy:.2f}%" if hy is not None else "—")

    lines = [
        f"FORM25 DAILY — {today}",
        "",
        f"{action}",
        f"{edge_note}",
    ]
    if regime_changed:
        lines.append(f"Regime changed: {prev_risk} -> {risk_env}")
    lines += [
        "",
        "Macro conditions:",
        f"  VIX: {_fmt(vix, decimals=1):<8} HY spread: {hy_str}",
        f"  Fed funds: {_fmt(fed, suffix='%', decimals=2):<8} 10Y-2Y: {spread_str}",
        f"  Yield curve: {curve:<10} Regime: {regime}",
        "",
    ]

    # Universe block
    n = len(passing)
    if risk_env == "elevated":
        lines.append(f"{n} companies passing filters:")
        lines.append("")
        if n == 0:
            lines.append("  (none today)")
        else:
            lines.append("   # Ticker Runway  vs52w  CashR")
            for i, t in enumerate(passing[:15], 1):
                runway  = f"{t['cash_runway_months']:.0f}mo" if t['cash_runway_months'] else "  —"
                vs52w   = f"{t['vs_52w_high_pct']*100:.0f}%" if t['vs_52w_high_pct'] else "  —"
                cashr   = f"{t['cash_ratio']:.2f}" if t['cash_ratio'] else "  —"
                lines.append(
                    f"  {i:>2} {t['ticker']:<6} {runway:>5}  {vs52w:>5}  {cashr:>5}"
                )
            if n > 15:
                lines.append(f"  ... and {n - 15} more")
    elif risk_env == "moderate":
        lines.append(f"{n} companies on watchlist:")
        if passing:
            lines.append("  First 5: " + ", ".join(t["ticker"] for t in passing[:5]))
        lines.append("  Monitor conditions — deploy if risk becomes elevated")
    else:
        lines.append(f"{n} companies in universe but conditions not met — no action")

    if risk_env == "elevated" and curve == "inverted":
        lines.append("")
        lines.append("CRISIS REGIME ACTIVE (elevated + inverted curve)")
        lines.append("  Strongest historical edge: +11.15% mean / 62.1% win rate at 30d")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    today = date.today().isoformat()
    print(f"Form25 monitor — {today}")

    # Refresh macro data first
    print("  Syncing macro data...", end=" ", flush=True)
    try:
        from data.fetch_macro import sync_fred, sync_treasury
        sync_fred(series_ids=["DFF", "VIXCLS", "BAMLH0A0HYM2", "T10Y2Y", "T10Y3M"])
        print("done")
    except Exception as e:
        print(f"warning: {e}")

    # Get current macro
    print("  Reading macro conditions...", end=" ", flush=True)
    macro = get_macro_on_date(today)
    print(f"done ({macro.get('risk_environment', '?')} risk, "
          f"VIX={macro.get('vix', '?')})")

    # Get passing tickers
    print("  Loading passing tickers...", end=" ", flush=True)
    passing = _get_passing_tickers()
    print(f"{len(passing)} passing")

    # Load previous state
    prev_state = _load_state()

    # Print the digest to stdout — under cron, redirect to a log file.
    # The dashboard is the overview; this is just a run record.
    print("\n" + "-" * 55)
    print(build_message(macro, passing, prev_state))
    print("-" * 55 + "\n")

    # Save new state
    new_state = {
        "date":             today,
        "risk_environment": macro.get("risk_environment"),
        "yield_curve":      macro.get("yield_curve_shape"),
        "macro_regime":     macro.get("macro_regime"),
        "vix":              macro.get("vix"),
        "hy_spread":        macro.get("hy_spread"),
        "passing_count":    len(passing),
    }
    _save_state(new_state)

    # Refresh dashboard export
    print("  Refreshing dashboard...", end=" ", flush=True)
    try:
        from scripts.export_dashboard import main as export_main
        export_main()
        print("done")
    except Exception as e:
        print(f"warning: {e}")

    print(f"\nDone. Regime: {macro.get('risk_environment')} | "
          f"Passing: {len(passing)} tickers")


if __name__ == "__main__":
    run()
