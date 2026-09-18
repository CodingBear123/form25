"""
Download historical daily OHLCV data using yfinance and store in SQLite.

Replaces the Massive S3 flat files approach — yfinance is free,
no API key needed, and covers 2+ years of daily data for all US equities.

Usage:
    python -m data.fetch_flat_files                    # sync full fallback list
    python -m data.fetch_flat_files MRNA FATE BEAM     # sync specific tickers

    Or import:
        from data.fetch_flat_files import sync_price_db, get_price_db_stats
"""

import os
import sqlite3
import sys
import time
from datetime import date, timedelta
from typing import Optional

import yfinance as yf

_DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'db', 'prices.db')


def _ensure_db(db_path: str) -> sqlite3.Connection:
    """Create SQLite DB and daily_prices table if they don't exist."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_prices (
            ticker  TEXT NOT NULL,
            date    TEXT NOT NULL,
            open    REAL,
            high    REAL,
            low     REAL,
            close   REAL,
            volume  REAL,
            PRIMARY KEY (ticker, date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON daily_prices(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ticker ON daily_prices(ticker)")
    conn.commit()
    return conn


def sync_ticker(
    ticker: str,
    conn: sqlite3.Connection,
    days_back: int = 1826,
) -> int:
    """
    Download historical OHLCV for one ticker and upsert into SQLite.
    Returns number of rows inserted/updated.
    """
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=f"{days_back}d", auto_adjust=True)

        if hist.empty:
            return 0

        rows = []
        for dt, row in hist.iterrows():
            date_str = dt.strftime("%Y-%m-%d")
            rows.append({
                "ticker": ticker.upper(),
                "date": date_str,
                "open": float(row["Open"]) if row["Open"] == row["Open"] else None,
                "high": float(row["High"]) if row["High"] == row["High"] else None,
                "low": float(row["Low"]) if row["Low"] == row["Low"] else None,
                "close": float(row["Close"]) if row["Close"] == row["Close"] else None,
                "volume": float(row["Volume"]) if row["Volume"] == row["Volume"] else None,
            })

        conn.executemany(
            """
            INSERT OR REPLACE INTO daily_prices (ticker, date, open, high, low, close, volume)
            VALUES (:ticker, :date, :open, :high, :low, :close, :volume)
            """,
            rows
        )
        conn.commit()
        return len(rows)

    except Exception as e:
        print(f"    {ticker}: error — {e}")
        return 0


def sync_price_db(
    tickers: list[str],
    days_back: int = 1826,
    db_path: str = _DB_PATH,
    delay: float = 0.1,
) -> dict:
    """
    Download historical daily OHLCV for a list of tickers into SQLite.

    Args:
        tickers:   list of tickers to sync
        days_back: how many days of history (default 2 years)
        db_path:   path to SQLite database
        delay:     seconds between yfinance calls (be polite)

    Returns summary dict.
    """
    conn = _ensure_db(db_path)
    total_rows = 0
    succeeded = 0
    failed = 0

    print(f"Syncing {len(tickers)} tickers ({days_back} days history)...")

    for i, ticker in enumerate(tickers):
        rows = sync_ticker(ticker, conn, days_back)
        if rows > 0:
            print(f"  [{i+1}/{len(tickers)}] {ticker}: {rows} rows")
            total_rows += rows
            succeeded += 1
        else:
            print(f"  [{i+1}/{len(tickers)}] {ticker}: no data")
            failed += 1
        time.sleep(delay)

    conn.close()

    summary = {
        "tickers_requested": len(tickers),
        "tickers_succeeded": succeeded,
        "tickers_failed": failed,
        "total_rows_inserted": total_rows,
        "db_path": os.path.abspath(db_path),
    }
    print(f"\nSync complete: {summary}")
    return summary


def get_price_db_stats(db_path: str = _DB_PATH) -> dict:
    """Return stats about the local price database."""
    db_path = os.path.abspath(db_path)
    if not os.path.exists(db_path):
        return {"exists": False, "db_path": db_path}

    conn = sqlite3.connect(db_path)
    total_rows = conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0]
    ticker_count = conn.execute("SELECT COUNT(DISTINCT ticker) FROM daily_prices").fetchone()[0]
    date_range = conn.execute(
        "SELECT MIN(date), MAX(date) FROM daily_prices"
    ).fetchone()
    conn.close()

    return {
        "exists": True,
        "db_path": db_path,
        "total_rows": total_rows,
        "tickers": ticker_count,
        "date_from": date_range[0],
        "date_to": date_range[1],
    }


def get_price(
    ticker: str,
    target_date: date,
    db_path: str = _DB_PATH,
) -> Optional[float]:
    """
    Get closing price for a ticker on or near target_date from local DB.
    Looks back up to 5 days to handle weekends and holidays.
    Returns None if not found.
    """
    db_path = os.path.abspath(db_path)
    if not os.path.exists(db_path):
        return None

    conn = sqlite3.connect(db_path)
    try:
        for days_back in range(5):
            check_date = (target_date - timedelta(days=days_back)).isoformat()
            row = conn.execute(
                "SELECT close FROM daily_prices WHERE ticker = ? AND date = ?",
                (ticker.upper(), check_date)
            ).fetchone()
            if row and row[0] is not None:
                return float(row[0])
    finally:
        conn.close()
    return None


if __name__ == "__main__":
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from data.fetch_universe import _curated_biotech_fallback

    # Allow passing specific tickers as args: python -m data.fetch_flat_files MRNA FATE
    if len(sys.argv) > 1:
        tickers = [t.upper() for t in sys.argv[1:]]
    else:
        tickers = [t["ticker"] for t in _curated_biotech_fallback()]

    print("Form25 price DB sync (yfinance)")
    print("=" * 50)
    print(f"Tickers: {len(tickers)}")
    print("This will take a few minutes on first run.\n")

    result = sync_price_db(tickers=tickers, days_back=1826)
    stats = get_price_db_stats()
    print(f"\nDB stats: {stats}")
