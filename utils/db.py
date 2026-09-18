"""
Form25 database layer.

Tables:
    daily_prices        — OHLCV per ticker per day (already populated)
    universe            — known tickers per sector with metadata
    fundamentals_cache  — XBRL fundamentals per ticker, TTL 7 days
    insider_cache       — Form 4 insider trades per ticker, TTL 24 hours
    snapshots           — assembled CompanySnapshot per ticker per date

All tables include a `sector` column for multi-sector support.
"""

import sqlite3
import json
import os
from datetime import datetime, timezone
from typing import Optional

_DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'db', 'form25.db')


def get_connection(db_path: str = _DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str = _DB_PATH) -> None:
    """Create all tables if they don't exist."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = get_connection(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS universe (
            ticker          TEXT NOT NULL,
            sector          TEXT NOT NULL,
            company_name    TEXT,
            market_cap      REAL,
            source          TEXT,
            active          INTEGER DEFAULT 1,
            -- Point-in-time listing window. A backtest day must only screen
            -- tickers that were actually listed on that day; without these
            -- the universe is whoever survived to today.
            listed_from     TEXT,
            delisted_date   TEXT,
            delisting_reason TEXT,
            added_at        TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (ticker, sector)
        );

        CREATE TABLE IF NOT EXISTS fundamentals_cache (
            ticker          TEXT NOT NULL,
            cik             TEXT,
            sector          TEXT NOT NULL,
            data            TEXT NOT NULL,
            fetched_at      TEXT NOT NULL,
            expires_at      TEXT NOT NULL,
            PRIMARY KEY (ticker, sector)
        );

        CREATE TABLE IF NOT EXISTS insider_cache (
            ticker          TEXT NOT NULL,
            sector          TEXT NOT NULL,
            data            TEXT NOT NULL,
            fetched_at      TEXT NOT NULL,
            expires_at      TEXT NOT NULL,
            PRIMARY KEY (ticker, sector)
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            ticker          TEXT NOT NULL,
            sector          TEXT NOT NULL,
            snapshot_date   TEXT NOT NULL,
            portfolio       TEXT NOT NULL,
            data            TEXT NOT NULL,
            data_quality    TEXT,
            warnings        TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (ticker, sector, snapshot_date, portfolio)
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_date ON snapshots(snapshot_date, sector);
        CREATE INDEX IF NOT EXISTS idx_snapshots_ticker ON snapshots(ticker, sector);
    """)
    _migrate_universe_listing_columns(conn)
    conn.commit()
    conn.close()


def _migrate_universe_listing_columns(conn: sqlite3.Connection) -> None:
    """
    Add the point-in-time listing columns to an existing universe table.

    CREATE TABLE IF NOT EXISTS silently skips tables that already exist, so
    DBs created before these columns were introduced need them added here.
    """
    existing = {r[1] for r in conn.execute("PRAGMA table_info(universe)")}
    for column in ("listed_from", "delisted_date", "delisting_reason"):
        if column not in existing:
            conn.execute(f"ALTER TABLE universe ADD COLUMN {column} TEXT")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_iso(ttl_seconds: int) -> str:
    now = datetime.now(timezone.utc)
    return datetime.fromtimestamp(now.timestamp() + ttl_seconds, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def upsert_universe(tickers: list[dict], sector: str, db_path: str = _DB_PATH) -> int:
    conn = get_connection(db_path)
    now = _now_iso()
    conn.executemany(
        """
        INSERT INTO universe (ticker, sector, company_name, market_cap, source, updated_at)
        VALUES (:ticker, :sector, :company_name, :market_cap, :source, :updated_at)
        ON CONFLICT(ticker, sector) DO UPDATE SET
            company_name=excluded.company_name, market_cap=excluded.market_cap,
            source=excluded.source, updated_at=excluded.updated_at
        """,
        [{"ticker": t["ticker"].upper(), "sector": sector,
          "company_name": t.get("company_name", ""), "market_cap": t.get("market_cap"),
          "source": t.get("source_etf", ""), "updated_at": now} for t in tickers]
    )
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM universe WHERE sector=? AND active=1", (sector,)).fetchone()[0]
    conn.close()
    return count


def get_universe_tickers(sector: str, db_path: str = _DB_PATH) -> list[str]:
    conn = get_connection(db_path)
    rows = conn.execute("SELECT ticker FROM universe WHERE sector=? AND active=1 ORDER BY ticker", (sector,)).fetchall()
    conn.close()
    return [r["ticker"] for r in rows]


# ---------------------------------------------------------------------------
# Fundamentals cache
# ---------------------------------------------------------------------------

def get_cached_fundamentals(ticker: str, sector: str, db_path: str = _DB_PATH) -> Optional[dict]:
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT data FROM fundamentals_cache WHERE ticker=? AND sector=? AND expires_at > ?",
        (ticker.upper(), sector, _now_iso())
    ).fetchone()
    conn.close()
    return json.loads(row["data"]) if row else None


def set_cached_fundamentals(ticker: str, sector: str, data: dict, ttl_seconds: int = 7*24*3600, db_path: str = _DB_PATH) -> None:
    conn = get_connection(db_path)
    conn.execute(
        """
        INSERT INTO fundamentals_cache (ticker, sector, cik, data, fetched_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, sector) DO UPDATE SET
            data=excluded.data, fetched_at=excluded.fetched_at, expires_at=excluded.expires_at
        """,
        (ticker.upper(), sector, data.get("cik", ""), json.dumps(data), _now_iso(), _expires_iso(ttl_seconds))
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Insider trades cache
# ---------------------------------------------------------------------------

def get_cached_insider(ticker: str, sector: str, db_path: str = _DB_PATH) -> Optional[list]:
    """Return cached insider trades if not expired, else None."""
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT data FROM insider_cache WHERE ticker=? AND sector=? AND expires_at > ?",
        (ticker.upper(), sector, _now_iso())
    ).fetchone()
    conn.close()
    return json.loads(row["data"]) if row else None


def set_cached_insider(ticker: str, sector: str, data: list, ttl_seconds: int = 24*3600, db_path: str = _DB_PATH) -> None:
    """Store insider trades in cache with TTL (default 24 hours)."""
    conn = get_connection(db_path)
    conn.execute(
        """
        INSERT INTO insider_cache (ticker, sector, data, fetched_at, expires_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(ticker, sector) DO UPDATE SET
            data=excluded.data, fetched_at=excluded.fetched_at, expires_at=excluded.expires_at
        """,
        (ticker.upper(), sector, json.dumps(data), _now_iso(), _expires_iso(ttl_seconds))
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def save_snapshot(ticker: str, sector: str, snapshot_date: str, portfolio: str,
                  snapshot_data: dict, data_quality: str, warnings: list[str], db_path: str = _DB_PATH) -> None:
    conn = get_connection(db_path)
    conn.execute(
        """
        INSERT INTO snapshots (ticker, sector, snapshot_date, portfolio, data, data_quality, warnings)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, sector, snapshot_date, portfolio) DO UPDATE SET
            data=excluded.data, data_quality=excluded.data_quality,
            warnings=excluded.warnings, created_at=datetime('now')
        """,
        (ticker.upper(), sector, snapshot_date, portfolio,
         json.dumps(snapshot_data), data_quality, json.dumps(warnings))
    )
    conn.commit()
    conn.close()


def get_snapshot(ticker: str, sector: str, snapshot_date: str, portfolio: str, db_path: str = _DB_PATH) -> Optional[dict]:
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT data, data_quality, warnings FROM snapshots WHERE ticker=? AND sector=? AND snapshot_date=? AND portfolio=?",
        (ticker.upper(), sector, snapshot_date, portfolio)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"snapshot": json.loads(row["data"]), "data_quality": row["data_quality"], "warnings": json.loads(row["warnings"])}


def get_snapshot_history(ticker: str, sector: str, portfolio: str = "value", limit: int = 90, db_path: str = _DB_PATH) -> list[dict]:
    """Get historical snapshots for charting fundamentals over time."""
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT snapshot_date, data, data_quality FROM snapshots
        WHERE ticker=? AND sector=? AND portfolio=?
        ORDER BY snapshot_date DESC LIMIT ?
        """,
        (ticker.upper(), sector, portfolio, limit)
    ).fetchall()
    conn.close()
    return [{"date": r["snapshot_date"], "data_quality": r["data_quality"], **json.loads(r["data"])} for r in rows]


def get_db_stats(db_path: str = _DB_PATH) -> dict:
    if not os.path.exists(db_path):
        return {"exists": False}
    conn = get_connection(db_path)
    stats = {
        "exists": True,
        "db_path": os.path.abspath(db_path),
        "universe_tickers": conn.execute("SELECT COUNT(*) FROM universe").fetchone()[0],
        "fundamentals_cached": conn.execute("SELECT COUNT(*) FROM fundamentals_cache").fetchone()[0],
        "insider_cached": conn.execute("SELECT COUNT(*) FROM insider_cache").fetchone()[0],
        "snapshots": conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
    }
    conn.close()
    return stats
