"""
Guard against ticker recycling in the price DB.

The trap
--------
Exchanges reissue symbols. Sorrento Therapeutics delisted on 2023-04-12, yet
yfinance happily returns SRNE bars running to the present day — those belong
to a different company that later took the symbol. Backfilling delisted names
by symbol alone splices a live company's returns onto a dead company's
identity, which is worse than the survivorship bias we set out to fix: instead
of omitting a bankruptcy, the backtest records it as a going concern.

The guard
---------
A ticker's bars are only trustworthy up to its delisting date. Everything
after that date belongs to whoever holds the symbol now, and must be dropped.
This is provider-agnostic — it cleans up after any price source, so it stays
useful if the yfinance backfill is later replaced by a paid feed.

Identity is anchored on the CIK in the universe table, not on the symbol.

Usage:
    python data/ticker_recycling.py --report   # show suspected recycling
    python data/ticker_recycling.py --purge    # delete post-delisting bars
"""

from __future__ import annotations

import os
import sqlite3
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.config import PRICES_DB_PATH, FORM25_DB_PATH  # noqa: E402

# Bars within a few days of the delisting are ordinary settlement noise. A
# gap well beyond that means the symbol was reissued to someone else.
_RECYCLING_THRESHOLD_DAYS = 30


def get_delisting_cutoffs(
    form25_db: str = FORM25_DB_PATH,
    sector: str = "biotech",
) -> dict[str, str]:
    """Map ticker -> delisted_date for every name known to have stopped trading."""
    conn = sqlite3.connect(form25_db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(universe)")}
        if "delisted_date" not in cols:
            return {}
        rows = conn.execute(
            "SELECT ticker, delisted_date FROM universe "
            "WHERE delisted_date IS NOT NULL AND sector = ?",
            (sector,),
        ).fetchall()
        return {r[0].upper(): r[1] for r in rows}
    finally:
        conn.close()


def find_recycled_tickers(
    cutoffs: Optional[dict[str, str]] = None,
    prices_db: str = PRICES_DB_PATH,
    form25_db: str = FORM25_DB_PATH,
) -> list[dict]:
    """
    Tickers whose price history extends well past their delisting date.

    Each result is a symbol whose recent bars almost certainly belong to a
    different company.
    """
    if cutoffs is None:
        cutoffs = get_delisting_cutoffs(form25_db)
    if not cutoffs:
        return []

    conn = sqlite3.connect(prices_db)
    suspects: list[dict] = []
    try:
        for ticker, delisted in cutoffs.items():
            row = conn.execute(
                "SELECT MIN(date), MAX(date), COUNT(*) FROM daily_prices WHERE ticker = ?",
                (ticker,),
            ).fetchone()
            if not row or not row[1]:
                continue
            first_bar, last_bar, n_bars = row

            if last_bar <= _shift(delisted, _RECYCLING_THRESHOLD_DAYS):
                continue

            stale = conn.execute(
                "SELECT COUNT(*) FROM daily_prices WHERE ticker = ? AND date > ?",
                (ticker, delisted),
            ).fetchone()[0]

            suspects.append({
                "ticker": ticker,
                "delisted_date": delisted,
                "first_bar": first_bar,
                "last_bar": last_bar,
                "total_bars": n_bars,
                "bars_after_delisting": stale,
            })
    finally:
        conn.close()

    return sorted(suspects, key=lambda s: -s["bars_after_delisting"])


def purge_post_delisting_bars(
    cutoffs: Optional[dict[str, str]] = None,
    prices_db: str = PRICES_DB_PATH,
    form25_db: str = FORM25_DB_PATH,
    dry_run: bool = False,
) -> dict:
    """
    Delete price bars dated after a ticker's delisting.

    Run this after every price sync. Returns a summary of what was removed.
    """
    if cutoffs is None:
        cutoffs = get_delisting_cutoffs(form25_db)
    if not cutoffs:
        return {"tickers_affected": 0, "bars_deleted": 0, "dry_run": dry_run}

    conn = sqlite3.connect(prices_db)
    affected = 0
    deleted = 0
    try:
        for ticker, delisted in cutoffs.items():
            n = conn.execute(
                "SELECT COUNT(*) FROM daily_prices WHERE ticker = ? AND date > ?",
                (ticker, delisted),
            ).fetchone()[0]
            if not n:
                continue
            affected += 1
            deleted += n
            if not dry_run:
                conn.execute(
                    "DELETE FROM daily_prices WHERE ticker = ? AND date > ?",
                    (ticker, delisted),
                )
        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    return {"tickers_affected": affected, "bars_deleted": deleted, "dry_run": dry_run}


def _shift(date_str: str, days: int) -> str:
    from datetime import datetime, timedelta

    return (
        datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)
    ).strftime("%Y-%m-%d")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Ticker recycling guard")
    parser.add_argument("--report", action="store_true", help="List suspected recycled tickers")
    parser.add_argument("--purge", action="store_true", help="Delete post-delisting bars")
    parser.add_argument("--dry-run", action="store_true", help="With --purge, show counts only")
    args = parser.parse_args()

    if not args.report and not args.purge:
        parser.error("pass --report or --purge")

    if args.report:
        suspects = find_recycled_tickers()
        if not suspects:
            print("No recycled tickers detected.")
        else:
            print(f"{len(suspects)} ticker(s) with bars after their delisting date:\n")
            print(f"  {'TICKER':8s} {'DELISTED':12s} {'LAST BAR':12s} {'STALE BARS':>10s}")
            for s in suspects:
                print(f"  {s['ticker']:8s} {s['delisted_date']:12s} "
                      f"{s['last_bar']:12s} {s['bars_after_delisting']:>10d}")
            print("\nThese bars belong to whoever holds the symbol now, not to the "
                  "delisted company. Run --purge to remove them.")

    if args.purge:
        result = purge_post_delisting_bars(dry_run=args.dry_run)
        verb = "would delete" if args.dry_run else "deleted"
        print(f"{verb} {result['bars_deleted']} bars across "
              f"{result['tickers_affected']} ticker(s)")
