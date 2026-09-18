"""
Form25 data setup script.

Run this once before the backtest, and then weekly to keep data fresh.
Steps:
    1. Sync price DB          -- 5 years of daily OHLCV for all tickers
    2. Sync macro data        -- FRED + US Treasury + Damodaran ERP
    3. Bulk fetch CIKs        -- one SEC call, maps all tickers to CIKs
    4. Prime filing timelines -- SEC XBRL history per ticker (backtest cache)

Usage (run from form25_root/form25/):
    python scripts/setup.py

    # Skip individual steps:
    python scripts/setup.py --skip-prices
    python scripts/setup.py --skip-macro
    python scripts/setup.py --skip-ciks
    python scripts/setup.py --skip-timelines

    # Force re-fetch everything even if fresh:
    python scripts/setup.py --force
"""

import sys
import os
import argparse
import sqlite3
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.config import sec_headers

_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


# ---------------------------------------------------------------------------
# Step 1 — Price DB
# ---------------------------------------------------------------------------

def step_prices(force: bool = False) -> None:
    print("\n" + "=" * 60)
    print("STEP 1 -- Price DB sync (5 years, yfinance)")
    print("=" * 60)

    from data.fetch_flat_files import sync_price_db, get_price_db_stats
    from data.fetch_universe import get_universe, _curated_biotech_fallback

    print("Fetching universe...")
    try:
        universe = get_universe()
        tickers = [u["ticker"] for u in universe]
    except Exception as e:
        print(f"Universe fetch failed ({e}), using curated fallback list")
        tickers = [t["ticker"] for t in _curated_biotech_fallback()]

    for bm in ["SPY", "XBI"]:
        if bm not in tickers:
            tickers.append(bm)

    print(f"Universe: {len(tickers)} tickers")
    print("Syncing 1826 days (~5 years) of OHLCV...")
    sync_price_db(tickers=tickers, days_back=1826)

    stats = get_price_db_stats()
    print(f"\nPrice DB: {stats['tickers']} tickers, {stats['total_rows']:,} rows")
    print(f"Coverage: {stats['date_from']} -> {stats['date_to']}")


# ---------------------------------------------------------------------------
# Step 2 — Macro
# ---------------------------------------------------------------------------

def step_macro(start_date: str = "2018-01-01", force: bool = False) -> None:
    print("\n" + "=" * 60)
    print("STEP 2 -- Macro data sync (FRED + Treasury + Damodaran)")
    print("=" * 60)

    from data.fetch_macro import sync_all_macro, get_macro_stats
    sync_all_macro(start_date=start_date, force=force)

    stats = get_macro_stats()
    print(f"\nMacro DB: {stats['total_series']} series, "
          f"{stats['total_observations']:,} observations")
    for s in stats["series"]:
        print(f"  {s['series_id']:20s} {s['source']:12s} "
              f"{s['earliest'] or '?':10s} -> {s['latest'] or '?'}")


# ---------------------------------------------------------------------------
# Step 3 — Bulk CIK fetch
# ---------------------------------------------------------------------------

def _fetch_all_ciks() -> dict[str, str]:
    """
    Download SEC's full company_tickers.json (one HTTP call, ~1 MB).
    Returns dict of ticker -> zero-padded 10-digit CIK string.
    """
    resp = httpx.get(_SEC_TICKERS_URL, headers=sec_headers(), timeout=20)
    resp.raise_for_status()
    cik_map: dict[str, str] = {}
    for entry in resp.json().values():
        ticker = entry["ticker"].upper().strip()
        cik    = str(entry["cik_str"]).zfill(10)
        cik_map[ticker] = cik
    return cik_map


def step_ciks(force: bool = False) -> None:
    """
    Fetch CIKs for all tickers in the price DB from SEC in one call,
    then store them in fundamentals_cache so prime_timelines can find them.
    Also fixes any corrupted/placeholder CIKs already in the DB.
    """
    print("\n" + "=" * 60)
    print("STEP 3 -- Bulk CIK fetch from SEC")
    print("=" * 60)

    from utils.config import FORM25_DB_PATH, PRICES_DB_PATH
    from utils.db import init_db

    init_db(FORM25_DB_PATH)

    # Tickers in price DB (exclude benchmarks)
    price_conn = sqlite3.connect(PRICES_DB_PATH)
    rows = price_conn.execute(
        "SELECT DISTINCT ticker FROM daily_prices ORDER BY ticker"
    ).fetchall()
    price_conn.close()
    all_tickers = [r[0] for r in rows if r[0] not in ("SPY", "XBI")]

    print(f"Tickers to resolve: {len(all_tickers)}")
    print("Fetching SEC company_tickers.json (one call)...", end=" ", flush=True)

    try:
        sec_cik_map = _fetch_all_ciks()
        print(f"{len(sec_cik_map):,} companies in SEC registry")
    except Exception as e:
        print(f"FAILED: {e}")
        return

    # Match our tickers against SEC registry
    matched   = {t: sec_cik_map[t] for t in all_tickers if t in sec_cik_map}
    unmatched = [t for t in all_tickers if t not in sec_cik_map]

    print(f"Matched:   {len(matched)} tickers")
    if unmatched:
        print(f"Unmatched: {len(unmatched)} tickers (delisted or OTC)")
        print(f"  {', '.join(unmatched[:20])}{'...' if len(unmatched) > 20 else ''}")

    # Write CIKs into fundamentals_cache
    # We store a minimal record — just enough for prime_timelines to find the CIK.
    # Full fundamentals will be filled in lazily when research() is called.
    import json
    from datetime import datetime, timezone

    now     = datetime.now(timezone.utc).isoformat()
    expires = "2099-01-01T00:00:00+00:00"  # CIKs don't expire

    form25_conn = sqlite3.connect(FORM25_DB_PATH)
    form25_conn.execute("PRAGMA journal_mode=WAL")

    inserted = 0
    updated  = 0
    skipped  = 0

    for ticker, cik in matched.items():
        # Check what's already there
        existing = form25_conn.execute(
            "SELECT cik FROM fundamentals_cache WHERE ticker = ? AND sector = 'biotech'",
            (ticker,),
        ).fetchone()

        if existing:
            existing_cik = existing[0]
            # Fix corrupted/placeholder CIKs (e.g. the 0001234567 Moderna issue)
            if existing_cik == cik and not force:
                skipped += 1
                continue
            # Update with correct CIK, preserve any existing fundamentals data
            form25_conn.execute(
                """UPDATE fundamentals_cache
                   SET cik = ?, fetched_at = ?, expires_at = ?
                   WHERE ticker = ? AND sector = 'biotech'""",
                (cik, now, expires, ticker),
            )
            updated += 1
        else:
            # Insert minimal stub — just CIK + empty data dict
            stub = json.dumps({"cik": cik, "entity_name": ticker, "data_quality": "stub"})
            form25_conn.execute(
                """INSERT INTO fundamentals_cache
                       (ticker, sector, cik, data, fetched_at, expires_at)
                   VALUES (?, 'biotech', ?, ?, ?, ?)""",
                (ticker, cik, stub, now, expires),
            )
            inserted += 1

    form25_conn.commit()
    form25_conn.close()

    print(f"\nCIK cache: {inserted} inserted, {updated} updated, {skipped} already correct")
    print("Ready for filing timeline priming.")


# ---------------------------------------------------------------------------
# Step 4 — Filing timeline cache
# ---------------------------------------------------------------------------

def step_timelines(force: bool = False) -> None:
    print("\n" + "=" * 60)
    print("STEP 4 -- SEC filing timeline cache (~1-2s per ticker)")
    print("=" * 60)

    from backtest.filing_timeline import prime_timeline_cache, get_timeline_cache_stats
    from utils.config import FORM25_DB_PATH, PRICES_DB_PATH

    # Tickers from price DB
    price_conn = sqlite3.connect(PRICES_DB_PATH)
    rows = price_conn.execute(
        "SELECT DISTINCT ticker FROM daily_prices ORDER BY ticker"
    ).fetchall()
    price_conn.close()
    all_tickers = [r[0] for r in rows if r[0] not in ("SPY", "XBI")]

    # CIKs from fundamentals_cache (now populated by step_ciks)
    form25_conn = sqlite3.connect(FORM25_DB_PATH)
    cik_map: dict[str, str] = {}
    for ticker in all_tickers:
        row = form25_conn.execute(
            "SELECT cik FROM fundamentals_cache WHERE ticker = ? AND sector = 'biotech' LIMIT 1",
            (ticker,),
        ).fetchone()
        if row and row[0]:
            cik_map[ticker] = row[0]
    form25_conn.close()

    pairs   = [(t, cik_map[t]) for t in all_tickers if t in cik_map]
    missing = [t for t in all_tickers if t not in cik_map]

    print(f"Tickers with CIK:    {len(pairs)}")
    if missing:
        print(f"Tickers without CIK: {len(missing)} (not in SEC registry, skipping)")

    if not pairs:
        print("No tickers with CIKs. Run step 3 first.")
        return

    prime_timeline_cache(pairs, force_refresh=force, db_path=FORM25_DB_PATH)

    stats = get_timeline_cache_stats()
    print(f"\nTimeline cache: {stats['total_tickers']} tickers "
          f"({stats['fresh']} fresh, {stats['stale']} stale)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Form25 data setup")
    parser.add_argument("--skip-prices",    action="store_true", help="Skip price DB sync")
    parser.add_argument("--skip-macro",     action="store_true", help="Skip macro data sync")
    parser.add_argument("--skip-ciks",      action="store_true", help="Skip bulk CIK fetch")
    parser.add_argument("--skip-timelines", action="store_true", help="Skip filing timeline cache")
    parser.add_argument("--macro-start",    default="2018-01-01",
                        help="Earliest macro date (default: 2018-01-01)")
    parser.add_argument("--force",          action="store_true",
                        help="Re-fetch all data even if recently synced")
    args = parser.parse_args()

    print("=" * 60)
    print("Form25 setup")
    print("=" * 60)

    if not args.skip_prices:
        step_prices(force=args.force)

    if not args.skip_macro:
        step_macro(start_date=args.macro_start, force=args.force)

    if not args.skip_ciks:
        step_ciks(force=args.force)

    if not args.skip_timelines:
        step_timelines(force=args.force)

    print("\n" + "=" * 60)
    print("Setup complete.")
    print("Next step -- run the backtest:")
    print("  python scripts/backtest.py --start 2019-06-14 --end 2026-09-18")
    print("=" * 60)


if __name__ == "__main__":
    main()
