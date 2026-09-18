"""
Dashboard data exporter.

Reads from form25.db and the analysis cache, writes dashboard/data.json.
Run this whenever you want to refresh the dashboard.

Usage:
    python scripts/export_dashboard.py
    python scripts/export_dashboard.py --start 2019-06-14 --end 2026-09-18
"""

import sys
import os
import json
import sqlite3
import argparse
from datetime import datetime, date, timedelta

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.config import (
    FORM25_DB_PATH, PRICES_DB_PATH, DEFAULT_START_DATE, DEFAULT_END_DATE,
)
from data.fetch_macro import get_macro_on_date
from scripts.analysis_cache import load_cache

DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), '..', 'dashboard')
OUTPUT_PATH   = os.path.join(DASHBOARD_DIR, 'data.json')

START_DATE = DEFAULT_START_DATE
END_DATE   = DEFAULT_END_DATE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nan_to_none(val):
    if val is None:
        return None
    try:
        if np.isnan(val):
            return None
    except (TypeError, ValueError):
        pass
    return val


def _safe(val):
    """Convert numpy types and NaN to JSON-safe Python types."""
    if val is None:
        return None
    if isinstance(val, (np.integer,)):
        return int(val)
    if isinstance(val, (np.floating,)):
        return None if np.isnan(val) else float(round(val, 4))
    if isinstance(val, (np.bool_,)):
        return bool(val)
    if isinstance(val, float) and (np.isnan(val) or np.isinf(val)):
        return None
    return val


# ---------------------------------------------------------------------------
# Section 1: Current macro conditions
# ---------------------------------------------------------------------------

def export_macro_now() -> dict:
    print("  Exporting current macro conditions...", end=" ", flush=True)
    today  = date.today().isoformat()
    macro  = get_macro_on_date(today)

    # Traffic light logic
    regime     = macro.get("macro_regime", "unknown")
    curve      = macro.get("yield_curve_shape", "unknown")
    risk_env   = macro.get("risk_environment", "unknown")
    vix        = macro.get("vix")
    hy         = macro.get("hy_spread")

    if risk_env == "elevated":
        signal = "GO"
        signal_label = "Deploy Screener"
        signal_color = "green"
        signal_detail = "Elevated risk conditions — this is when the screener has maximum edge (+7.23% mean, 55.9% win rate)"
    elif risk_env == "moderate":
        signal = "WATCH"
        signal_label = "Monitor Closely"
        signal_color = "yellow"
        signal_detail = "Moderate risk — screener has some edge but conditions not optimal. Watchlist only."
    else:
        signal = "WAIT"
        signal_label = "Sit Out"
        signal_color = "red"
        signal_detail = "Low risk environment — screener has no statistically significant edge (p=0.23). Wait for elevated conditions."

    # Historical macro for chart (last 252 trading days ~1 year)
    form25_conn = sqlite3.connect(FORM25_DB_PATH)
    vix_history = form25_conn.execute("""
        SELECT date, value FROM macro_data
        WHERE series_id = 'VIXCLS'
          AND date >= date(?, '-365 days')
          AND date <= ?
        ORDER BY date ASC
    """, (today, today)).fetchall()

    hy_history = form25_conn.execute("""
        SELECT date, value FROM macro_data
        WHERE series_id = 'BAMLH0A0HYM2'
          AND date >= date(?, '-365 days')
          AND date <= ?
        ORDER BY date ASC
    """, (today, today)).fetchall()

    spread_history = form25_conn.execute("""
        SELECT date, value FROM macro_data
        WHERE series_id = 'T10Y2Y'
          AND date >= date(?, '-365 days')
          AND date <= ?
        ORDER BY date ASC
    """, (today, today)).fetchall()
    form25_conn.close()

    print("done")
    return {
        "as_of":          today,
        "fed_funds":      _safe(macro.get("fed_funds_rate")),
        "vix":            _safe(vix),
        "hy_spread":      _safe(hy),
        "cpi_yoy":        _safe(macro.get("cpi_yoy")),
        "treas_2y":       _safe(macro.get("treas_2y")),
        "treas_10y":      _safe(macro.get("treas_10y")),
        "yield_spread":   _safe(macro.get("yield_spread_10y2y")),
        "erp_implied":    _safe(macro.get("erp_implied")),
        "macro_regime":   regime,
        "yield_curve":    curve,
        "risk_env":       risk_env,
        "signal":         signal,
        "signal_label":   signal_label,
        "signal_color":   signal_color,
        "signal_detail":  signal_detail,
        "vix_history": [
            {"date": r[0], "value": _safe(r[1])}
            for r in vix_history if r[1] is not None
        ],
        "hy_history": [
            {"date": r[0], "value": _safe(r[1])}
            for r in hy_history if r[1] is not None
        ],
        "spread_history": [
            {"date": r[0], "value": _safe(r[1])}
            for r in spread_history if r[1] is not None
        ],
    }


# ---------------------------------------------------------------------------
# Saved backtest report
#
# The report written by `form25 backtest --save-report` is the single source
# of truth for the headline statistics. The dashboard reads it rather than
# recomputing or restating them, so the two can no longer disagree.
# ---------------------------------------------------------------------------

def _report_path(start: str, end: str) -> str:
    return os.path.join(
        DASHBOARD_DIR, '..', 'backtest', f'analysis_{start}_{end}.json'
    )


def _load_report(start: str, end: str) -> dict:
    path = _report_path(start, end)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No saved backtest report at {os.path.abspath(path)}.\n"
            f"Run: python form25.py backtest --start {start} --end {end} --save-report"
        )
    with open(path) as f:
        return json.load(f)


def _load_permutation_tests(start: str, end: str) -> list[dict]:
    """Permutation test results, shaped for the dashboard's chart."""
    return [
        {
            "hold_days":   t["hold_days"],
            "observed":    t["observed_mean"],
            "null_mean":   t["null_mean"],
            "effect":      t["effect_size"],
            "p_value":     t["p_value"],
            "ci_lo":       t["ci_lower_95"],
            "ci_hi":       t["ci_upper_95"],
            "significant": t["significant"],
        }
        for t in _load_report(start, end).get("permutation_tests", [])
    ]


# ---------------------------------------------------------------------------
# Section 2: Backtest evidence
# ---------------------------------------------------------------------------

def export_backtest_evidence(c: dict, start: str, end: str) -> dict:
    print("  Exporting backtest evidence...", end=" ", flush=True)

    passed  = c["passed_filters"]
    dates   = c["snapshot_date"]
    tickers = c["ticker"]

    # --- Permutation test summary ---
    # Read from the saved backtest report rather than restating it. These used
    # to be hardcoded constants from an earlier run, which is how the dashboard
    # came to show a 90d mean of +7.55% while the report said +5.82% — the same
    # headline number, computed once and then frozen while the data moved on.
    perm_results = _load_permutation_tests(start, end)

    # --- Win rates and averages ---
    portfolio_stats = []
    for hd in [30, 60, 90]:
        key  = f"return_{hd}d_pct"
        rets = c[key]
        mask = passed & ~np.isnan(rets)
        vals = rets[mask]
        if len(vals) == 0:
            continue
        failed_mask = ~passed & ~np.isnan(rets)
        failed_vals = rets[failed_mask]
        portfolio_stats.append({
            "hold_days":     hd,
            "mean_return":   _safe(float(np.mean(vals))),
            "median_return": _safe(float(np.median(vals))),
            "win_rate":      _safe(float(np.mean(vals > 0)) * 100),
            "std":           _safe(float(np.std(vals, ddof=1))),
            "failed_mean":   _safe(float(np.mean(failed_vals))) if len(failed_vals) > 0 else None,
            "n":             int(len(vals)),
        })

    # --- Regime breakdown ---
    regime_stats = []
    regime_map = {
        "elevated":    ("Elevated Risk",    "risk_environment"),
        "moderate":    ("Moderate Risk",    "risk_environment"),
        "low":         ("Low Risk",         "risk_environment"),
        "inverted":    ("Inverted Curve",   "yield_curve_shape"),
        "flat":        ("Flat Curve",       "yield_curve_shape"),
        "normal":      ("Normal Curve",     "yield_curve_shape"),
        "restrictive": ("Restrictive Macro","macro_regime"),
        "neutral":     ("Neutral Macro",    "macro_regime"),
        "accommodative": ("Accommodative",  "macro_regime"),
    }

    rets_30 = c["return_30d_pct"]
    for key, (label, col) in regime_map.items():
        mask = passed & (c[col] == key) & ~np.isnan(rets_30)
        vals = rets_30[mask]
        if len(vals) < 20:
            continue
        regime_stats.append({
            "regime":      label,
            "category":    col.replace("_", " "),
            "n":           int(len(vals)),
            "mean_return": _safe(float(np.mean(vals))),
            "win_rate":    _safe(float(np.mean(vals > 0)) * 100),
        })
    regime_stats.sort(key=lambda x: -x["mean_return"])

    # --- Equity curve: monthly screener return vs XBI ---
    # Group by month, compute average return of screener selections
    equity_curve = []
    unique_months = sorted(set(d[:7] for d in dates[passed & ~np.isnan(rets_30)]))
    xbi_prices    = _load_xbi_prices()

    for month in unique_months:
        mask = passed & np.char.startswith(dates.astype(str), month) & ~np.isnan(rets_30)
        vals = rets_30[mask]
        if len(vals) == 0:
            continue
        xbi_ret = xbi_prices.get(month)
        equity_curve.append({
            "month":          month,
            "screener_return": _safe(float(np.mean(vals))),
            "xbi_return":     _safe(xbi_ret),
            "n_positions":    int(len(vals)),
        })

    # --- Price level analysis ---
    drawdown  = np.abs(np.minimum(c["price_vs_52w_high_pct"], 0.0))
    elevated  = c["risk_environment"] == "elevated"
    levels    = [
        (0.00, 0.10, "0-10%"), (0.10, 0.20, "10-20%"), (0.20, 0.30, "20-30%"),
        (0.30, 0.40, "30-40%"), (0.40, 0.50, "40-50%"), (0.50, 0.60, "50-60%"),
        (0.60, 0.70, "60-70%"), (0.70, 0.80, "70-80%"), (0.80, 0.90, "80-90%"),
        (0.90, 1.01, "90%+"),
    ]
    price_levels = []
    for lo, hi, label in levels:
        mask     = passed & (drawdown >= lo) & (drawdown < hi) & ~np.isnan(rets_30)
        mask_elv = mask & elevated
        vals     = rets_30[mask]
        vals_elv = rets_30[mask_elv]
        if len(vals) < 5:
            continue
        price_levels.append({
            "bucket":           label,
            "mean_all":         _safe(float(np.mean(vals))),
            "mean_elevated":    _safe(float(np.mean(vals_elv))) if len(vals_elv) >= 5 else None,
            "win_rate_all":     _safe(float(np.mean(vals > 0)) * 100),
            "win_rate_elevated": _safe(float(np.mean(vals_elv > 0)) * 100) if len(vals_elv) >= 5 else None,
            "n_all":            int(len(vals)),
            "n_elevated":       int(len(vals_elv)),
        })

    # --- Calendar: elevated risk days per year ---
    elevated_calendar = []
    for year in sorted(set(d[:4] for d in dates)):
        all_d  = np.sum(np.char.startswith(np.unique(dates).astype(str), year))
        elv_d  = np.sum(np.char.startswith(
            np.unique(dates[elevated]).astype(str), year))
        if all_d == 0:
            continue
        elevated_calendar.append({
            "year":           year,
            "total_days":     int(all_d),
            "elevated_days":  int(elv_d),
            "pct":            _safe(round(int(elv_d) / int(all_d) * 100, 1)),
        })

    print("done")
    return {
        "start_date":         start,
        "end_date":           end,
        "total_snapshots":    int(len(dates)),
        "passed_snapshots":   int(np.sum(passed)),
        "pass_rate":          _safe(round(float(np.sum(passed)) / len(dates) * 100, 1)),
        "unique_tickers":     int(len(np.unique(tickers))),
        "permutation_tests":  perm_results,
        "portfolio_stats":    portfolio_stats,
        "regime_stats":       regime_stats,
        "equity_curve":       equity_curve,
        "price_levels":       price_levels,
        "elevated_calendar":  elevated_calendar,
    }


def _load_xbi_prices() -> dict[str, float]:
    """Load monthly XBI returns from price DB."""
    conn = sqlite3.connect(PRICES_DB_PATH)
    rows = conn.execute("""
        SELECT date, close FROM daily_prices
        WHERE ticker = 'XBI'
        ORDER BY date ASC
    """).fetchall()
    conn.close()

    # Compute monthly return: last close of month vs last close of prior month
    by_month: dict[str, list] = {}
    for date_str, close in rows:
        m = date_str[:7]
        by_month.setdefault(m, []).append(close)

    monthly_rets: dict[str, float] = {}
    months = sorted(by_month.keys())
    for i in range(1, len(months)):
        prev = by_month[months[i - 1]][-1]
        curr = by_month[months[i]][-1]
        if prev and prev > 0:
            monthly_rets[months[i]] = round((curr - prev) / prev * 100, 2)
    return monthly_rets


# ---------------------------------------------------------------------------
# Section 3: Today's screener universe
# ---------------------------------------------------------------------------

def export_current_universe() -> dict:
    print("  Exporting current universe...", end=" ", flush=True)

    today = date.today().isoformat()
    conn  = sqlite3.connect(FORM25_DB_PATH)

    # Get most recent snapshot per ticker (within last 5 trading days)
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    rows   = conn.execute("""
        SELECT ticker, snapshot_date, data, data_quality
        FROM snapshots
        WHERE sector = 'biotech' AND portfolio = 'value'
          AND snapshot_date >= ?
        ORDER BY snapshot_date DESC, ticker ASC
    """, (cutoff,)).fetchall()
    conn.close()

    # Deduplicate: one row per ticker (most recent)
    seen    = set()
    tickers = []
    for row in rows:
        if row[0] in seen:
            continue
        seen.add(row[0])
        try:
            data = json.loads(row[2])
        except Exception:
            continue

        if not data.get("passed_filters"):
            continue

        tickers.append({
            "ticker":              row[0],
            "snapshot_date":       row[1],
            "cash_ratio":          _safe(data.get("cash_ratio")),
            "cash_runway_months":  _safe(data.get("cash_runway_months")),
            "market_cap_m":        _safe(
                data["market_cap"] / 1e6 if data.get("market_cap") else None
            ),
            "price":               _safe(data.get("current_price")),
            "vs_52w_high_pct":     _safe(
                round(data["price_vs_52w_high_pct"] * 100, 1)
                if data.get("price_vs_52w_high_pct") is not None else None
            ),
            "hard_filter_failures": data.get("hard_filter_failures", []),
            "data_quality":        row[3],
            "macro_regime":        data.get("macro_regime"),
            "risk_environment":    data.get("risk_environment"),
        })

    # Alphabetical, not ranked. Passing the hard filters is binary, and the
    # soft score that used to order this table scored rho = 0.008 against
    # forward return, so any ordering here would be decoration.
    tickers.sort(key=lambda x: x["ticker"])

    print(f"{len(tickers)} passing tickers")
    return {
        "as_of":           today,
        "passing_count":   len(tickers),
        "tickers":         tickers[:50],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def export_survivorship(start: str, end: str) -> dict:
    """
    Delisting attrition and its effect on the reported edge.

    The backtest can only price companies that still trade, so every headline
    figure it produces is measured on survivors. This block quantifies what
    that omission is worth, so the correction sits beside the claim rather
    than in a footnote nobody reads.

    Returns {} when the universe table has no delisting data — the dashboard
    then says so plainly instead of implying the numbers are corrected.
    """
    conn = sqlite3.connect(FORM25_DB_PATH)
    try:
        # Prefer delisting_scan_cache: it holds every CIK the scan examined.
        # The universe table only holds rows whose ticker could be recovered,
        # which drops ~70% of dead companies from the numerator and a large
        # number of unlisted live filers from the denominator. Computing
        # attrition from it reported 28.0% against a true 14.9% — nearly
        # double, and in the direction that makes the strategy look worst.
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        source_table = (
            "delisting_scan_cache" if "delisting_scan_cache" in tables else "universe"
        )

        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({source_table})")}
        if "delisted_date" not in cols:
            return {}

        scanned = conn.execute(f"SELECT COUNT(*) FROM {source_table}").fetchone()[0]
        if not scanned:
            return {}

        in_window = conn.execute(
            f"SELECT COUNT(*) FROM {source_table} WHERE delisted_date BETWEEN ? AND ?",
            (start, end),
        ).fetchone()[0]

        mix = dict(conn.execute(
            f"SELECT COALESCE(delisting_reason, 'unknown'), COUNT(*) FROM {source_table} "
            "WHERE delisted_date BETWEEN ? AND ? GROUP BY 1",
            (start, end),
        ).fetchall())
    finally:
        conn.close()

    if not in_window:
        return {}

    attrition = in_window / scanned

    # Observed alpha vs XBI at the primary horizon, from the saved analysis.
    observed = (_load_report(start, end).get("screener_vs_xbi") or {}).get("90d")

    # Filters admitted dying companies at 50% vs a 64.6% baseline, so the
    # screener has partial but real skill at avoiding them. Small sample —
    # surfaced in the UI as an assumption, not a fact.
    skill_factor = 0.77

    sensitivity = []
    if observed is not None:
        for acq, label in [
            (-10.0, "Acquired below market"),
            (0.0,   "Acquired at market"),
            (10.0,  "Acquired at +10%"),
            (20.0,  "Acquired at +20%"),
            (30.0,  "Acquired at +30%"),
        ]:
            blended = _blend(mix, acq)
            if blended is None:
                continue
            eff = attrition * skill_factor
            sensitivity.append({
                "assumption":  label,
                "acq_return":  acq,
                "blended":     round(blended, 1),
                "corrected":   round(observed * (1 - attrition) + blended * attrition, 2),
                "corrected_adj": round(observed * (1 - eff) + blended * eff, 2),
            })

    return {
        "scanned":        scanned,
        "delisted":       in_window,
        "attrition_pct":  round(attrition * 100, 1),
        "reason_mix":     mix,
        "observed_alpha": observed,
        "skill_factor":   skill_factor,
        "sensitivity":    sensitivity,
        "source":         "SEC EDGAR (Form 25/15, 8-K item 1.03)",
    }


def _blend(mix: dict, acquisition_return: float) -> float | None:
    """Delisting return weighted by the observed mix of delisting reasons."""
    total = sum(mix.values())
    if not total:
        return None
    per_reason = {
        "bankruptcy":             -100.0,
        "acquisition_or_private": acquisition_return,
        "unknown":                -30.0,
    }
    return sum(
        per_reason.get(r, -30.0) * n for r, n in mix.items()
    ) / total


def main(start: str = START_DATE, end: str = END_DATE) -> None:
    os.makedirs(DASHBOARD_DIR, exist_ok=True)

    print("Exporting dashboard data...")
    print(f"Period: {start} -> {end}\n")

    c = load_cache()

    output = {
        "generated_at": datetime.now().isoformat(),
        "macro":        export_macro_now(),
        "backtest":     export_backtest_evidence(c, start, end),
        "universe":     export_current_universe(),
        "survivorship": export_survivorship(start, end),
    }

    # Write data.json (kept as reference / backup)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2, default=str)

    # Embed data directly into the HTML so it works when opened
    # as a local file:// in any browser (avoids CORS fetch block)
    html_path     = os.path.join(DASHBOARD_DIR, 'index.html')
    html_out_path = os.path.join(DASHBOARD_DIR, 'form25.html')

    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()

    data_js = json.dumps(output, default=str)
    injection = f'<script>window.__FORM25_DATA__ = {data_js};</script>'

    # Inject right before closing </head>
    html = html.replace('</head>', injection + '\n</head>', 1)

    # Replace the async fetch with a synchronous data read
    html = html.replace(
        "const res = await fetch('./data.json');\n          this.data = await res.json();",
        "this.data = window.__FORM25_DATA__;"
    )

    # Inline the vendored libraries. The page previously loaded Alpine and
    # Chart.js from a CDN, and Alpine is what clears x-cloak — so any browser
    # that blocks the CDN (Brave with Shields on, uBlock, Firefox strict)
    # rendered a blank page rather than a degraded one. Inlining makes the
    # output a genuinely single file with no external requests.
    # `defer` does nothing on an inline script, so the tags cannot simply be
    # swapped in place — Alpine would run against an unparsed DOM. Drop them
    # from <head> and inline both at the end of <body>, Chart.js first so it
    # is defined by the time an Alpine component calls buildCharts().
    html = html.replace('  <script src="vendor/chart.umd.min.js"></script>\n', '', 1)
    html = html.replace('  <script src="vendor/alpine.min.js" defer></script>\n', '', 1)

    libs = []
    for rel in ('vendor/chart.umd.min.js', 'vendor/alpine.min.js'):
        with open(os.path.join(DASHBOARD_DIR, rel), 'r', encoding='utf-8') as f:
            libs.append(f'<script>\n{f.read()}\n</script>')
    html = html.replace('</body>', '\n'.join(libs) + '\n</body>', 1)

    with open(html_out_path, 'w', encoding='utf-8') as f:
        f.write(html)

    # docs/index.html is what GitHub Pages serves. Write it from the same
    # render so the published dashboard cannot drift from the local one.
    docs_dir = os.path.join(os.path.dirname(DASHBOARD_DIR), 'docs')
    published = os.path.isdir(docs_dir)
    if published:
        with open(os.path.join(docs_dir, 'index.html'), 'w', encoding='utf-8') as f:
            f.write(html)

    size_kb = os.path.getsize(html_out_path) / 1024
    print(f"\nDashboard ready: {os.path.abspath(html_out_path)} ({size_kb:.0f} KB)")
    print("Open form25.html in any browser — no server needed.")
    if published:
        print("Also wrote docs/index.html (what GitHub Pages serves).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=START_DATE)
    parser.add_argument("--end",   default=END_DATE)
    args = parser.parse_args()
    main(start=args.start, end=args.end)
