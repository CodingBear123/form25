"""
Sync price DB for the full Massive universe.
Run from project root: python backtest/sync_universe.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.fetch_universe import get_universe
from data.fetch_flat_files import sync_price_db, get_price_db_stats

def main() -> None:
    print("Step 1: Fetching biotech universe...")
    universe = get_universe()
    tickers = [u["ticker"] for u in universe]
    print(f"\nUniverse: {len(tickers)} tickers")

    print("\nStep 2: Syncing price DB...")
    sync_price_db(tickers=tickers, days_back=1826)

    print("\nStep 3: DB stats")
    stats = get_price_db_stats()
    print(f"  Tickers: {stats['tickers']}")
    print(f"  Rows:    {stats['total_rows']}")
    print(f"  Range:   {stats['date_from']} → {stats['date_to']}")


if __name__ == "__main__":
    main()
