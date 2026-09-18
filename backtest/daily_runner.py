"""
Daily backtest runner for Form25.

Builds CompanySnapshot objects for every trading day over a date range,
using fully cached data to avoid redundant API calls.

Cache hierarchy (fastest to slowest):
    1. snapshots table     — full snapshot already built for (ticker, date) -> skip entirely
    2. filing_timelines    — point-in-time fundamentals via binary search (zero API calls)
    3. prices.db           — historical OHLCV from local SQLite (zero API calls)
    4. macro_data table    — macro indicators from local SQLite (zero API calls)

The only thing that requires SEC API calls is priming the filing timeline cache
(once per ticker, ~250 calls total). Everything else runs fully offline.

Usage:
    from backtest.daily_runner import run_daily_backtest

    results = run_daily_backtest(
        start_date="2020-01-01",
        end_date="2024-12-31",
        tickers=None,          # None = full price DB universe
        portfolio="value",
        include_macro=True,
    )
"""

import sqlite3
import json
import os
from datetime import date, datetime, timedelta
from typing import Optional

from screener.models import CompanySnapshot, HardFilterParams
from screener.filters import apply_hard_filters
from backtest.filing_timeline import (
    get_fundamentals_on_date,
    init_timeline_table,
    prime_timeline_cache,
    _load_timeline_from_cache,
)
from data.fetch_macro import get_macro_on_date, init_macro_tables
from utils.config import FORM25_DB_PATH, PRICES_DB_PATH

_BENCHMARKS = ["SPY", "XBI"]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _prices_conn(db_path: str = PRICES_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _form25_conn(db_path: str = FORM25_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ---------------------------------------------------------------------------
# Trading day calendar from prices DB
# ---------------------------------------------------------------------------

def get_trading_days(
    start_date: str,
    end_date: str,
    db_path: str = PRICES_DB_PATH,
) -> list[str]:
    """
    Return all trading days in [start_date, end_date] by querying the price DB.
    We use SPY as the reference instrument (it trades every market open day).
    Falls back to all dates in the DB if SPY not present.
    """
    conn = _prices_conn(db_path)

    # Try SPY first, then XBI, then any ticker
    for ref in ["SPY", "XBI"]:
        rows = conn.execute(
            "SELECT DISTINCT date FROM daily_prices WHERE ticker = ? AND date BETWEEN ? AND ? ORDER BY date",
            (ref, start_date, end_date),
        ).fetchall()
        if rows:
            conn.close()
            return [r[0] for r in rows]

    # Last resort: all dates present in the DB
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily_prices WHERE date BETWEEN ? AND ? ORDER BY date",
        (start_date, end_date),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Price lookups from local SQLite
# ---------------------------------------------------------------------------

def _get_price_on_date(
    ticker: str,
    target_date: str,
    conn: sqlite3.Connection,
    lookback_days: int = 5,
) -> Optional[float]:
    """
    Get closing price for ticker on or just before target_date.
    Looks back up to lookback_days to handle weekends/holidays.
    """
    for i in range(lookback_days):
        check = (
            datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=i)
        ).strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT close FROM daily_prices WHERE ticker = ? AND date = ?",
            (ticker.upper(), check),
        ).fetchone()
        if row and row[0] is not None:
            return float(row[0])
    return None


# ---------------------------------------------------------------------------
# Delisting handling
#
# A ticker whose price series simply stops is not "missing data" — it is a
# company that stopped trading. Treating it as missing drops the observation
# from the sample entirely, which books a bankruptcy as a non-event and
# inflates measured returns (survivorship bias).
#
# Convention, following CRSP practice: liquidation/bankruptcy is -100%, an
# acquisition is the deal price, and an unknown reason takes a haircut. We
# rarely know the reason, so the default below is the unknown-reason case.
# ---------------------------------------------------------------------------

DELISTING_RETURN_PCT = -30.0

# Trading-day slack when matching a forward date to an actual bar. Three days
# covers a long weekend; beyond that the ticker is presumed to have stopped.
_FORWARD_SLACK_DAYS = 3

_last_bar_cache: dict[str, Optional[str]] = {}
_db_end_cache: dict[int, Optional[str]] = {}


def get_listing_windows(form25_db: str) -> dict[str, tuple[Optional[str], Optional[str]]]:
    """
    Map ticker -> (listed_from, delisted_date) from the universe table.

    Either bound may be None when unknown. Returns {} if the table predates
    the listing columns, in which case no point-in-time screen is applied.
    """
    conn = _form25_conn(form25_db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(universe)")}
        if not {"listed_from", "delisted_date"} <= cols:
            return {}
        rows = conn.execute(
            "SELECT ticker, listed_from, delisted_date FROM universe"
        ).fetchall()
        return {r[0].upper(): (r[1], r[2]) for r in rows}
    finally:
        conn.close()


def _was_listed_on(
    ticker: str,
    day: str,
    windows: dict[str, tuple[Optional[str], Optional[str]]],
) -> bool:
    """
    True if the ticker was tradeable on `day`.

    Tickers absent from the map are assumed listed — the map is advisory, and
    an incomplete universe table should not silently empty the backtest.
    """
    window = windows.get(ticker.upper())
    if window is None:
        return True
    listed_from, delisted_date = window
    if listed_from and day < listed_from:
        return False
    if delisted_date and day >= delisted_date:
        return False
    return True


def _get_last_bar_date(ticker: str, conn: sqlite3.Connection) -> Optional[str]:
    """Date of the final bar we hold for this ticker, cached per process."""
    key = ticker.upper()
    if key not in _last_bar_cache:
        row = conn.execute(
            "SELECT MAX(date) FROM daily_prices WHERE ticker = ?", (key,)
        ).fetchone()
        _last_bar_cache[key] = row[0] if row else None
    return _last_bar_cache[key]


def _get_db_end_date(conn: sqlite3.Connection) -> Optional[str]:
    """Latest date present in the price DB, across all tickers."""
    key = id(conn)
    if key not in _db_end_cache:
        row = conn.execute("SELECT MAX(date) FROM daily_prices").fetchone()
        _db_end_cache[key] = row[0] if row else None
    return _db_end_cache[key]


def _get_forward_price(
    ticker: str,
    fwd_date: str,
    conn: sqlite3.Connection,
) -> tuple[Optional[float], str]:
    """
    Resolve the price at the end of a hold period.

    Returns (price, outcome) where outcome is one of:
        "ok"         — a real bar within _FORWARD_SLACK_DAYS of fwd_date
        "delisted"   — the ticker's series ends before fwd_date; caller should
                       book DELISTING_RETURN_PCT rather than discard the row
        "truncated"  — fwd_date is past the end of our data for every ticker,
                       i.e. the hold period hasn't completed yet
        "missing"    — a gap we can't explain; caller discards the row
    """
    for i in range(_FORWARD_SLACK_DAYS + 1):
        check = (
            datetime.strptime(fwd_date, "%Y-%m-%d") - timedelta(days=i)
        ).strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT close FROM daily_prices WHERE ticker = ? AND date = ?",
            (ticker.upper(), check),
        ).fetchone()
        if row and row[0] is not None:
            return float(row[0]), "ok"

    # No bar near fwd_date. Distinguish "hasn't happened yet" from "stopped
    # trading" — without this check every recent snapshot looks like a
    # delisting and the fix becomes worse than the bug.
    db_end = _get_db_end_date(conn)
    if db_end and fwd_date > db_end:
        return None, "truncated"

    last_bar = _get_last_bar_date(ticker, conn)
    if last_bar and last_bar < fwd_date:
        return None, "delisted"

    return None, "missing"


def _get_52w_stats(
    ticker: str,
    as_of_date: str,
    conn: sqlite3.Connection,
) -> tuple[Optional[float], Optional[float]]:
    """Return (52w_high, 52w_low) using price data up to as_of_date."""
    start = (
        datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=365)
    ).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT MAX(high), MIN(low) FROM daily_prices WHERE ticker = ? AND date BETWEEN ? AND ?",
        (ticker.upper(), start, as_of_date),
    ).fetchone()
    if row:
        return row[0], row[1]
    return None, None


def _get_price_change_30d(
    ticker: str,
    as_of_date: str,
    price_now: float,
    conn: sqlite3.Connection,
) -> Optional[float]:
    """Return 30-day price change % using price DB."""
    start = (
        datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=35)
    ).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT close FROM daily_prices WHERE ticker = ? AND date BETWEEN ? AND ? ORDER BY date ASC LIMIT 1",
        (ticker.upper(), start, as_of_date),
    ).fetchone()
    if rows and rows[0] and rows[0] > 0:
        return round((price_now - float(rows[0])) / float(rows[0]), 4)
    return None


# ---------------------------------------------------------------------------
# Snapshot cache helpers
# ---------------------------------------------------------------------------

def _snapshot_exists(
    ticker: str,
    snapshot_date: str,
    portfolio: str,
    conn: sqlite3.Connection,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM snapshots WHERE ticker = ? AND snapshot_date = ? AND portfolio = ?",
        (ticker.upper(), snapshot_date, portfolio),
    ).fetchone()
    return row is not None


def _save_snapshot_row(
    ticker: str,
    snapshot_date: str,
    portfolio: str,
    snapshot_data: dict,
    data_quality: str,
    passed_filters: bool,
    hard_filter_failures: list[str],
    conn: sqlite3.Connection,
) -> None:
    """Save a snapshot to the snapshots table (sector hardcoded to 'biotech' for now)."""
    conn.execute("""
        INSERT INTO snapshots
            (ticker, sector, snapshot_date, portfolio, data, data_quality, warnings)
        VALUES (?, 'biotech', ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, sector, snapshot_date, portfolio) DO NOTHING
    """, (
        ticker.upper(), snapshot_date, portfolio,
        json.dumps({**snapshot_data,
                    "passed_filters": passed_filters,
                    "hard_filter_failures": hard_filter_failures}),
        data_quality,
        json.dumps(hard_filter_failures),
    ))


# ---------------------------------------------------------------------------
# Universe helpers
# ---------------------------------------------------------------------------

def get_universe_from_price_db(db_path: str = PRICES_DB_PATH) -> list[str]:
    """Return all non-benchmark tickers with price data."""
    conn = _prices_conn(db_path)
    rows = conn.execute(
        "SELECT DISTINCT ticker FROM daily_prices ORDER BY ticker"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows if r[0] not in _BENCHMARKS]


def get_cik_map_from_cache(
    tickers: list[str],
    db_path: str = FORM25_DB_PATH,
) -> dict[str, str]:
    """
    Pull (ticker -> CIK) mapping from the fundamentals_cache table.
    Returns dict — tickers with no cached CIK are excluded.
    """
    conn = _form25_conn(db_path)
    result: dict[str, str] = {}
    for ticker in tickers:
        row = conn.execute(
            "SELECT cik FROM fundamentals_cache WHERE ticker = ? LIMIT 1",
            (ticker.upper(),),
        ).fetchone()
        if row and row[0]:
            result[ticker.upper()] = row[0]
    conn.close()
    return result


# ---------------------------------------------------------------------------
# Core: build one snapshot for one ticker on one date
# ---------------------------------------------------------------------------

def build_daily_snapshot(
    ticker: str,
    snapshot_date: str,
    cik: str,
    price_conn: sqlite3.Connection,
    params: Optional[HardFilterParams] = None,
) -> Optional[dict]:
    """
    Build a CompanySnapshot for a ticker on a specific historical date,
    using only data available on that date (no lookahead).

    Returns dict with all snapshot fields plus:
        passed_filters, hard_filter_failures
    Or None if price data is unavailable.
    """
    if params is None:
        params = HardFilterParams()

    # --- Price (from local DB, no API call) ---
    price = _get_price_on_date(ticker, snapshot_date, price_conn)
    if price is None:
        return None  # No price data = can't build snapshot

    high_52w, low_52w = _get_52w_stats(ticker, snapshot_date, price_conn)
    price_vs_52w = (
        round((price - high_52w) / high_52w, 4) if high_52w and high_52w > 0 else None
    )
    price_change_30d = _get_price_change_30d(ticker, snapshot_date, price, price_conn)

    # --- Fundamentals (from timeline cache, binary search) ---
    fundamentals = get_fundamentals_on_date(ticker, snapshot_date, cik=cik)
    if fundamentals is None:
        return None

    cash = fundamentals.get("cash")
    burn = fundamentals.get("burn_rate_monthly")
    runway = fundamentals.get("cash_runway_months")
    shares = fundamentals.get("shares_outstanding")

    # Market cap from price × shares (no yfinance needed)
    market_cap = (price * shares) if price and shares else None
    cash_ratio = (
        round(cash / market_cap, 4)
        if cash and market_cap and market_cap > 0
        else None
    )

    # Recalculate runway with current market price (shares may differ from filing)
    if cash and burn and burn > 0:
        runway = round(cash / burn, 1)

    data_quality = fundamentals.get("data_quality", "partial")

    # --- Assemble snapshot ---
    snapshot = CompanySnapshot(
        ticker=ticker.upper(),
        entity_name=ticker,   # name not critical for backtest
        cik=cik,
        market_cap=market_cap,
        current_price=price,
        price_52w_high=high_52w,
        price_52w_low=low_52w,
        price_vs_52w_high_pct=price_vs_52w,
        cash=cash,
        burn_rate_monthly=burn,
        cash_runway_months=runway,
        cash_ratio=cash_ratio,
        shares_outstanding=shares,
        pipeline_stage="unknown",   # pipeline stage requires live clinical data
        insider_buys_90d=0,         # insider data not cached historically
    )

    # --- Hard filters ---
    snapshot = apply_hard_filters(snapshot, params)

    return {
        "ticker":               ticker.upper(),
        "snapshot_date":        snapshot_date,
        "current_price":        price,
        "market_cap":           market_cap,
        "cash":                 cash,
        "burn_rate_monthly":    burn,
        "cash_runway_months":   runway,
        "cash_ratio":           cash_ratio,
        "shares_outstanding":   shares,
        "price_52w_high":       high_52w,
        "price_52w_low":        low_52w,
        "price_vs_52w_high_pct": price_vs_52w,
        "price_change_30d_pct": price_change_30d,
        "passed_filters":       snapshot.passes_hard_filters,
        "hard_filter_failures": snapshot.hard_filter_failures,
        "data_quality":         data_quality,
        "fundamentals_as_of":   fundamentals.get("filed_date"),
    }


# ---------------------------------------------------------------------------
# Main: run daily backtest over a date range
# ---------------------------------------------------------------------------

def run_daily_backtest(
    start_date: str,
    end_date: str,
    tickers: Optional[list[str]] = None,
    portfolio: str = "value",
    include_macro: bool = True,
    params: Optional[HardFilterParams] = None,
    hold_days: list[int] = [30, 60, 90],
    save_snapshots: bool = True,
    skip_existing: bool = True,
    prices_db: str = PRICES_DB_PATH,
    form25_db: str = FORM25_DB_PATH,
) -> dict:
    """
    Run the full backtest over every trading day in [start_date, end_date].

    For each day:
        1. For each ticker: build snapshot using cached price + filing timeline data
        2. Apply hard filters
        3. Fetch forward returns at hold_days intervals
        4. Optionally attach macro context for that day
        5. Save to snapshots table (skips if already exists)

    Returns a summary dict plus the full results list.

    Performance:
        After cache priming (~5-8 min one-time), this runs at ~1,000-5,000
        snapshots/second since everything is local SQLite reads.
        5 years × 250 tickers × 252 days = ~315,000 snapshots.
        Expected runtime after priming: 5-30 minutes depending on hardware.
    """
    init_timeline_table(form25_db)
    if include_macro:
        init_macro_tables(form25_db)

    if params is None:
        params = HardFilterParams()

    # Universe
    if tickers is None:
        tickers = get_universe_from_price_db(prices_db)
    tickers = [t.upper() for t in tickers if t.upper() not in _BENCHMARKS]

    print(f"Daily backtest: {len(tickers)} tickers, {start_date} -> {end_date}")

    listing_windows = get_listing_windows(form25_db)
    if listing_windows:
        known_delisted = sum(1 for _, d in listing_windows.values() if d)
        print(f"  Point-in-time universe: {len(listing_windows)} tickers, "
              f"{known_delisted} with a delisting date")
    else:
        print("  WARNING: no listing dates in the universe table — the universe "
              "is survivors-only and results will be biased upward.")

    # CIK map — needed for filing timeline lookups
    cik_map = get_cik_map_from_cache(tickers, form25_db)
    tickers_with_ciks = [(t, cik_map[t]) for t in tickers if t in cik_map]
    missing_ciks = [t for t in tickers if t not in cik_map]
    if missing_ciks:
        print(f"  Warning: {len(missing_ciks)} tickers have no CIK cached — run build_snapshot() first")

    # Check which tickers still need timeline cache
    needs_prime = [
        (t, c) for t, c in tickers_with_ciks
        if _load_timeline_from_cache(t, form25_db) is None
    ]
    if needs_prime:
        print(f"\n  Priming filing timeline cache for {len(needs_prime)} tickers...")
        prime_timeline_cache(needs_prime, db_path=form25_db)

    # Trading days calendar
    trading_days = get_trading_days(start_date, end_date, prices_db)
    print(f"\n  Trading days in range: {len(trading_days)}")
    print(f"  Total snapshots to build: ~{len(tickers_with_ciks) * len(trading_days):,}")

    price_conn  = _prices_conn(prices_db)
    form25_conn = _form25_conn(form25_db)

    all_results: list[dict] = []
    snapshots_built  = 0
    snapshots_skipped = 0
    snapshots_failed  = 0

    print("\nBuilding snapshots...")

    # Pre-load all macro data for the full date range in one pass.
    # Avoids opening a new DB connection once per trading day (1826x).
    macro_cache: dict[str, dict] = {}
    if include_macro:
        print("  Pre-loading macro data...", end=" ", flush=True)
        for d in trading_days:
            macro_cache[d] = get_macro_on_date(d, form25_db)
        print(f"{len(macro_cache)} days loaded")

    for day_idx, trading_day in enumerate(trading_days):
        if day_idx % 50 == 0:
            pct = round(day_idx / len(trading_days) * 100, 1)
            print(f"  {trading_day} ({pct}%) — {snapshots_built} built, {snapshots_skipped} skipped")

        # Macro context from pre-loaded cache
        macro = macro_cache.get(trading_day, {}) if include_macro else {}

        day_results: list[dict] = []

        for ticker, cik in tickers_with_ciks:
            # Point-in-time screen: only names listed on this day are
            # eligible, so we don't screen a 2026 universe on a 2019 day.
            if not _was_listed_on(ticker, trading_day, listing_windows):
                continue

            # Skip if already cached
            if skip_existing and _snapshot_exists(ticker, trading_day, portfolio, form25_conn):
                snapshots_skipped += 1
                continue

            snap = build_daily_snapshot(
                ticker, trading_day, cik,
                price_conn, params,
            )
            if snap is None:
                snapshots_failed += 1
                continue

            # Attach macro context
            if include_macro and macro:
                snap["macro_regime"]       = macro.get("macro_regime")
                snap["yield_curve_shape"]  = macro.get("yield_curve_shape")
                snap["risk_environment"]   = macro.get("risk_environment")
                snap["fed_funds_rate"]     = macro.get("fed_funds_rate")
                snap["vix"]                = macro.get("vix")
                snap["erp_implied"]        = macro.get("erp_implied")
                snap["hy_spread"]          = macro.get("hy_spread")

            # Forward returns — look up prices at hold_days intervals
            for days in hold_days:
                fwd_date = (
                    datetime.strptime(trading_day, "%Y-%m-%d") + timedelta(days=days)
                ).strftime("%Y-%m-%d")
                fwd_price, outcome = _get_forward_price(ticker, fwd_date, price_conn)
                if outcome == "ok" and snap["current_price"] and snap["current_price"] > 0:
                    snap[f"return_{days}d_pct"] = round(
                        (fwd_price - snap["current_price"]) / snap["current_price"] * 100, 2
                    )
                    snap[f"price_{days}d"] = fwd_price
                elif outcome == "delisted":
                    # Stopped trading mid-hold — book the loss, don't drop it.
                    snap[f"return_{days}d_pct"] = DELISTING_RETURN_PCT
                    snap[f"price_{days}d"] = None
                    snap[f"delisted_{days}d"] = True
                else:
                    snap[f"return_{days}d_pct"] = None
                    snap[f"price_{days}d"] = None

            # Save to DB
            if save_snapshots:
                _save_snapshot_row(
                    ticker, trading_day, portfolio,
                    snap, snap["data_quality"],
                    snap["passed_filters"],
                    snap["hard_filter_failures"],
                    form25_conn,
                )

            day_results.append(snap)
            snapshots_built += 1

        if day_results:
            form25_conn.commit()
        all_results.extend(day_results)

    price_conn.close()
    form25_conn.close()

    print(f"\nDone: {snapshots_built} built, {snapshots_skipped} skipped, {snapshots_failed} failed")

    return {
        "start_date":        start_date,
        "end_date":          end_date,
        "trading_days":      len(trading_days),
        "tickers":           len(tickers_with_ciks),
        "snapshots_built":   snapshots_built,
        "snapshots_skipped": snapshots_skipped,
        "snapshots_failed":  snapshots_failed,
        "results":           all_results,
    }


# ---------------------------------------------------------------------------
# Load results from DB for stats engine
# ---------------------------------------------------------------------------

def load_backtest_results(
    start_date: str,
    end_date: str,
    portfolio: str = "value",
    hold_days: list[int] = [30, 60, 90],
    form25_db: str = FORM25_DB_PATH,
    prices_db: str = PRICES_DB_PATH,
) -> list[dict]:
    """
    Load all snapshot records from the DB for a date range and compute returns.
    Used by stats.py after daily_runner has populated the snapshots table.

    Returns list of dicts, one per (ticker, date) with returns attached.
    """
    conn = _form25_conn(form25_db)
    rows = conn.execute("""
        SELECT ticker, snapshot_date, data, data_quality
        FROM snapshots
        WHERE sector = 'biotech'
          AND portfolio = ?
          AND snapshot_date BETWEEN ? AND ?
        ORDER BY snapshot_date, ticker
    """, (portfolio, start_date, end_date)).fetchall()
    conn.close()

    price_conn = _prices_conn(prices_db)
    results: list[dict] = []

    for row in rows:
        data = json.loads(row["data"])
        snap_date = row["snapshot_date"]
        entry_price = data.get("current_price")
        if not entry_price:
            continue

        record = {
            "ticker":               row["ticker"],
            "snapshot_date":        snap_date,
            "passed_filters":       data.get("passed_filters", False),
            "hard_filter_failures": data.get("hard_filter_failures", []),
            "market_cap":           data.get("market_cap"),
            "cash_runway_months":   data.get("cash_runway_months"),
            "cash_ratio":           data.get("cash_ratio"),
            "price_vs_52w_high_pct": data.get("price_vs_52w_high_pct"),
            "entry_price":          entry_price,
            "macro_regime":         data.get("macro_regime"),
            "yield_curve_shape":    data.get("yield_curve_shape"),
            "risk_environment":     data.get("risk_environment"),
            "erp_implied":          data.get("erp_implied"),
        }

        for days in hold_days:
            fwd_key = f"return_{days}d_pct"
            if fwd_key in data:
                record[fwd_key] = data[fwd_key]
            else:
                fwd_date = (
                    datetime.strptime(snap_date, "%Y-%m-%d") + timedelta(days=days)
                ).strftime("%Y-%m-%d")
                fwd_price, outcome = _get_forward_price(
                    row["ticker"], fwd_date, price_conn
                )
                if outcome == "ok" and entry_price > 0:
                    record[fwd_key] = round((fwd_price - entry_price) / entry_price * 100, 2)
                elif outcome == "delisted":
                    record[fwd_key] = DELISTING_RETURN_PCT
                else:
                    record[fwd_key] = None

        results.append(record)

    price_conn.close()
    return results


# ---------------------------------------------------------------------------
# Benchmark returns
# ---------------------------------------------------------------------------

def compute_benchmark_returns(
    dates: list[str],
    hold_days: list[int] = [30, 60, 90],
    benchmarks: Optional[list[str]] = None,
    prices_db: str = PRICES_DB_PATH,
) -> dict[str, dict[str, float]]:
    """
    Mean forward return for each benchmark, measured over `dates`.

    Returns {ticker: {"30d": pct, ...}}, the shape run_full_analysis expects.

    The benchmark must be sampled on exactly the days the screener traded.
    Averaging over a different calendar compares two different questions and
    the resulting "alpha" is meaningless.

    Loads each series once into memory rather than querying per date — at
    ~1,800 dates x 3 horizons that is the difference between seconds and
    minutes.
    """
    if benchmarks is None:
        benchmarks = list(_BENCHMARKS)
    if not dates:
        return {}

    conn = _prices_conn(prices_db)
    out: dict[str, dict[str, float]] = {}

    try:
        for ticker in benchmarks:
            rows = conn.execute(
                "SELECT date, close FROM daily_prices WHERE ticker = ? "
                "AND close IS NOT NULL ORDER BY date",
                (ticker.upper(),),
            ).fetchall()
            if not rows:
                continue

            series = {r[0]: float(r[1]) for r in rows}

            def price_on_or_before(day: str) -> Optional[float]:
                """Walk back a few days to clear weekends and holidays."""
                for i in range(5):
                    check = (
                        datetime.strptime(day, "%Y-%m-%d") - timedelta(days=i)
                    ).strftime("%Y-%m-%d")
                    if check in series:
                        return series[check]
                return None

            per_hold: dict[str, float] = {}
            for hd in hold_days:
                rets: list[float] = []
                for d in dates:
                    entry = price_on_or_before(d)
                    if not entry or entry <= 0:
                        continue
                    fwd_day = (
                        datetime.strptime(d, "%Y-%m-%d") + timedelta(days=hd)
                    ).strftime("%Y-%m-%d")
                    # Don't fabricate a return past the end of the data.
                    if fwd_day > rows[-1][0]:
                        continue
                    fwd = price_on_or_before(fwd_day)
                    if not fwd:
                        continue
                    rets.append((fwd - entry) / entry * 100)

                if rets:
                    per_hold[f"{hd}d"] = round(sum(rets) / len(rets), 2)

            if per_hold:
                out[ticker.upper()] = per_hold
    finally:
        conn.close()

    return out
