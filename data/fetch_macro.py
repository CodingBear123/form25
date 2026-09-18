"""
Macro data fetcher for Form25.

Sources:
    FRED    — Fed funds rate, yield curve (10Y-2Y spread), CPI YoY,
               VIX, HY credit spread (BAMLH0A0HYM2)
              Endpoint: https://api.stlouisfed.org/fred/series/observations
              Requires FRED_API_KEY in .env

    US Treasury — Daily yield curve rates (3M, 2Y, 5Y, 10Y, 30Y)
              Endpoint: https://home.treasury.gov (CSV download)
              No API key required.

    Damodaran — Implied Equity Risk Premium (ERP), updated monthly.
              Source: https://pages.stern.nyu.edu/~adamodar/
              We download the CSV once per month and cache it.

Caching:
    All data is stored in form25.db in two tables:
        macro_series  — metadata for each series (id, name, source, last_updated)
        macro_data    — daily/monthly observations (series_id, date, value)

    FRED & Treasury:  refreshed monthly (data is mostly backward-looking)
    Damodaran ERP:    refreshed monthly

Public interface:
    get_macro_on_date(date_str)  ->  dict with all macro indicators for that date
    sync_all_macro()             ->  fetch/update all series
    get_macro_stats()            ->  coverage report
"""

import sqlite3
import os
import time
from datetime import date, datetime, timedelta
from typing import Optional

import httpx

from utils.config import FRED_API_KEY, FORM25_DB_PATH, web_headers

# ---------------------------------------------------------------------------
# FRED series we track
# ---------------------------------------------------------------------------

FRED_SERIES = {
    "FEDFUNDS":     "Fed Funds Rate (monthly avg, %)",
    "DFF":          "Fed Funds Rate (daily effective, %)",
    "T10Y2Y":       "10Y-2Y Treasury Spread (daily, %)",
    "T10Y3M":       "10Y-3M Treasury Spread (daily, %)",
    "CPIAUCSL":     "CPI All Urban Consumers (monthly, index)",
    "VIXCLS":       "CBOE VIX (daily, index)",
    "BAMLH0A0HYM2": "ICE BofA HY OAS Spread (daily, %)",
    "UMCSENT":      "U Michigan Consumer Sentiment (monthly)",
}

_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

# Damodaran ERP — monthly CSV
_DAMO_ERP_SERIES = "DAMO_ERP_IMPLIED"
_DAMO_RF_SERIES  = "DAMO_RF_RATE"
_DAMO_ERP_URL    = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/implprem/ERPbymonth.csv"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_conn(db_path: str = FORM25_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_macro_tables(db_path: str = FORM25_DB_PATH) -> None:
    """Create macro tables if they don't exist. Safe to call repeatedly."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = _get_conn(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS macro_series (
            series_id       TEXT PRIMARY KEY,
            source          TEXT NOT NULL,
            description     TEXT,
            last_synced_at  TEXT,
            earliest_date   TEXT,
            latest_date     TEXT
        );

        CREATE TABLE IF NOT EXISTS macro_data (
            series_id   TEXT NOT NULL,
            date        TEXT NOT NULL,
            value       REAL,
            PRIMARY KEY (series_id, date),
            FOREIGN KEY (series_id) REFERENCES macro_series(series_id)
        );
        CREATE INDEX IF NOT EXISTS idx_macro_date   ON macro_data(date);
        CREATE INDEX IF NOT EXISTS idx_macro_series ON macro_data(series_id);
    """)
    conn.commit()
    conn.close()


def _upsert_series_meta(
    series_id: str,
    source: str,
    description: str,
    earliest: Optional[str],
    latest: Optional[str],
    db_path: str = FORM25_DB_PATH,
) -> None:
    conn = _get_conn(db_path)
    conn.execute("""
        INSERT INTO macro_series (series_id, source, description, last_synced_at, earliest_date, latest_date)
        VALUES (?, ?, ?, datetime('now'), ?, ?)
        ON CONFLICT(series_id) DO UPDATE SET
            last_synced_at = datetime('now'),
            earliest_date  = COALESCE(excluded.earliest_date, macro_series.earliest_date),
            latest_date    = excluded.latest_date
    """, (series_id, source, description, earliest, latest))
    conn.commit()
    conn.close()


def _bulk_upsert_observations(
    series_id: str,
    observations: list[tuple[str, float]],
    db_path: str = FORM25_DB_PATH,
) -> int:
    """Insert or replace (date, value) pairs for a series. Returns row count."""
    if not observations:
        return 0
    conn = _get_conn(db_path)
    conn.executemany(
        "INSERT OR REPLACE INTO macro_data (series_id, date, value) VALUES (?, ?, ?)",
        [(series_id, d, v) for d, v in observations],
    )
    conn.commit()
    count = len(observations)
    conn.close()
    return count


def _get_latest_stored_date(series_id: str, db_path: str = FORM25_DB_PATH) -> Optional[str]:
    conn = _get_conn(db_path)
    row = conn.execute(
        "SELECT MAX(date) FROM macro_data WHERE series_id = ?", (series_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _needs_refresh(series_id: str, max_age_days: int = 30, db_path: str = FORM25_DB_PATH) -> bool:
    """Return True if series hasn't been synced within max_age_days."""
    conn = _get_conn(db_path)
    row = conn.execute(
        "SELECT last_synced_at FROM macro_series WHERE series_id = ?", (series_id,)
    ).fetchone()
    conn.close()
    if not row or not row[0]:
        return True
    last = datetime.fromisoformat(row[0].replace("Z", ""))
    age = (datetime.now() - last).days
    return age >= max_age_days


# ---------------------------------------------------------------------------
# FRED fetcher
# ---------------------------------------------------------------------------

def _fetch_fred_series(
    series_id: str,
    observation_start: str = "2018-01-01",
    observation_end: Optional[str] = None,
) -> list[tuple[str, float]]:
    """
    Fetch observations for one FRED series.
    Returns list of (date_str, value), skipping missing ('.') values.
    """
    if not FRED_API_KEY:
        raise RuntimeError("FRED_API_KEY not set in .env")

    params: dict = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "observation_start": observation_start,
        "limit": 100000,
        "sort_order": "asc",
    }
    if observation_end:
        params["observation_end"] = observation_end

    resp = httpx.get(_FRED_BASE, params=params, timeout=30)
    resp.raise_for_status()

    observations = []
    for obs in resp.json().get("observations", []):
        val_str = obs.get("value", ".")
        if val_str == ".":
            continue
        try:
            observations.append((obs["date"], float(val_str)))
        except (ValueError, KeyError):
            continue
    return observations


def sync_fred(
    series_ids: Optional[list[str]] = None,
    start_date: str = "2018-01-01",
    force: bool = False,
    db_path: str = FORM25_DB_PATH,
) -> dict:
    """
    Sync all FRED series into macro_data.
    Incremental — only fetches from the day after the last stored date.
    """
    init_macro_tables(db_path)
    if series_ids is None:
        series_ids = list(FRED_SERIES.keys())

    summary: dict = {}
    for series_id in series_ids:
        description = FRED_SERIES.get(series_id, series_id)

        if not force and not _needs_refresh(series_id, max_age_days=30, db_path=db_path):
            print(f"  FRED {series_id}: up to date, skipping")
            summary[series_id] = "skipped"
            continue

        last_stored = _get_latest_stored_date(series_id, db_path)
        fetch_start = start_date
        if last_stored:
            next_day = (
                datetime.strptime(last_stored, "%Y-%m-%d") + timedelta(days=1)
            ).strftime("%Y-%m-%d")
            fetch_start = max(fetch_start, next_day)

        print(f"  FRED {series_id}: fetching from {fetch_start}...", end=" ", flush=True)
        try:
            observations = _fetch_fred_series(series_id, observation_start=fetch_start)
            if observations:
                _bulk_upsert_observations(series_id, observations, db_path)
                latest = observations[-1][0]
                _upsert_series_meta(series_id, "FRED", description, observations[0][0], latest, db_path)
                print(f"{len(observations)} obs → {latest}")
                summary[series_id] = {"rows": len(observations), "latest": latest}
            else:
                print("no new data")
                _upsert_series_meta(series_id, "FRED", description, None, last_stored, db_path)
                summary[series_id] = {"rows": 0}
            time.sleep(0.3)
        except Exception as e:
            print(f"ERROR: {e}")
            summary[series_id] = {"error": str(e)}

    return summary


# ---------------------------------------------------------------------------
# US Treasury daily yield curve
# ---------------------------------------------------------------------------

# Tenors we extract and their series IDs
_TREASURY_COL_MAP = {
    "3 Mo":  "TREAS_3M",
    "6 Mo":  "TREAS_6M",
    "1 Yr":  "TREAS_1Y",
    "2 Yr":  "TREAS_2Y",
    "5 Yr":  "TREAS_5Y",
    "10 Yr": "TREAS_10Y",
    "20 Yr": "TREAS_20Y",
    "30 Yr": "TREAS_30Y",
}

_TREASURY_SERIES_DESCRIPTIONS = {
    "TREAS_3M":  "US Treasury 3-Month Par Yield (%)",
    "TREAS_6M":  "US Treasury 6-Month Par Yield (%)",
    "TREAS_1Y":  "US Treasury 1-Year Par Yield (%)",
    "TREAS_2Y":  "US Treasury 2-Year Par Yield (%)",
    "TREAS_5Y":  "US Treasury 5-Year Par Yield (%)",
    "TREAS_10Y": "US Treasury 10-Year Par Yield (%)",
    "TREAS_20Y": "US Treasury 20-Year Par Yield (%)",
    "TREAS_30Y": "US Treasury 30-Year Par Yield (%)",
}


def _fetch_treasury_year(year: int) -> list[tuple[str, str, float]]:
    """
    Fetch one calendar year of daily Treasury par yield curve CSV.
    Returns list of (date_str YYYY-MM-DD, series_id, rate_pct).
    """
    url = (
        f"https://home.treasury.gov/resource-center/data-chart-center/"
        f"interest-rates/daily-treasury-rates.csv/{year}/all"
        f"?type=daily_treasury_yield_curve&field_tdr_date_value={year}&download=true"
    )
    resp = httpx.get(url, headers=web_headers(), timeout=30, follow_redirects=True)
    resp.raise_for_status()

    rows: list[tuple[str, str, float]] = []
    lines = resp.text.strip().split("\n")
    if len(lines) < 2:
        return rows

    header = [h.strip().strip('"') for h in lines[0].split(",")]

    for line in lines[1:]:
        if not line.strip():
            continue
        cols = [c.strip().strip('"') for c in line.split(",")]
        if not cols[0]:
            continue

        # Treasury date format: MM/DD/YYYY
        try:
            dt = datetime.strptime(cols[0], "%m/%d/%Y")
            date_str = dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

        for col_name, series_id in _TREASURY_COL_MAP.items():
            if col_name in header:
                idx = header.index(col_name)
                if idx < len(cols):
                    val_str = cols[idx].strip()
                    if val_str and val_str not in ("N/A", "", "null"):
                        try:
                            rows.append((date_str, series_id, float(val_str)))
                        except ValueError:
                            continue
    return rows


def sync_treasury(
    start_year: int = 2018,
    force: bool = False,
    db_path: str = FORM25_DB_PATH,
) -> dict:
    """
    Sync US Treasury daily yield curve data year by year.
    Skips past years already fully loaded unless force=True.
    Current year always refreshed.
    """
    init_macro_tables(db_path)
    current_year = date.today().year
    summary: dict = {}

    for year in range(start_year, current_year + 1):
        # Skip fully loaded past years
        if year < current_year and not force:
            conn = _get_conn(db_path)
            count = conn.execute(
                "SELECT COUNT(*) FROM macro_data WHERE series_id = 'TREAS_10Y' AND date LIKE ?",
                (f"{year}-%",),
            ).fetchone()[0]
            conn.close()
            if count > 200:
                print(f"  Treasury {year}: loaded ({count} rows), skipping")
                summary[year] = "skipped"
                continue

        print(f"  Treasury {year}: fetching...", end=" ", flush=True)
        try:
            all_rows = _fetch_treasury_year(year)
            if all_rows:
                by_series: dict[str, list[tuple[str, float]]] = {}
                for date_str, sid, val in all_rows:
                    by_series.setdefault(sid, []).append((date_str, val))

                for sid, obs in by_series.items():
                    _bulk_upsert_observations(sid, obs, db_path)

                for sid, desc in _TREASURY_SERIES_DESCRIPTIONS.items():
                    _upsert_series_meta(sid, "US_TREASURY", desc, None, None, db_path)

                print(f"{len(all_rows)} rate-date pairs across {len(by_series)} tenors")
                summary[year] = {"rows": len(all_rows)}
            else:
                print("no data")
                summary[year] = {"rows": 0}
            time.sleep(0.5)
        except Exception as e:
            print(f"ERROR: {e}")
            summary[year] = {"error": str(e)}

    return summary


# ---------------------------------------------------------------------------
# Damodaran ERP
# ---------------------------------------------------------------------------

def _parse_damodaran_csv(text: str) -> list[tuple[str, str, float]]:
    """
    Parse Damodaran ERPbymonth.csv.
    Returns list of (date_str YYYY-MM-01, series_id, value).
    Handles formatting variations across years.
    """
    rows: list[tuple[str, str, float]] = []
    lines = text.strip().split("\n")

    # Find the header row
    header_idx = 0
    for i, line in enumerate(lines):
        lower = line.lower()
        if "month" in lower or "date" in lower:
            header_idx = i
            break

    header = [h.strip().strip('"').lower() for h in lines[header_idx].split(",")]

    # Map columns by keyword — Damodaran's headers shift slightly year to year
    month_col = next((i for i, h in enumerate(header) if "month" in h or "date" in h), 0)
    # Implied ERP column
    erp_col = next(
        (i for i, h in enumerate(header) if "implied" in h and "erp" in h), None
    )
    if erp_col is None:
        erp_col = next((i for i, h in enumerate(header) if "erp" in h), None)
    # Risk-free rate column
    rf_col = next(
        (i for i, h in enumerate(header)
         if "t.bond" in h or "risk free" in h or "tbond" in h or "rf rate" in h),
        None,
    )

    if erp_col is None:
        return rows

    for line in lines[header_idx + 1:]:
        if not line.strip():
            continue
        cols = [c.strip().strip('"') for c in line.split(",")]
        needed = max(c for c in [month_col, erp_col, rf_col] if c is not None)
        if len(cols) <= needed:
            continue

        # Parse date — Damodaran uses "January 2024", "Jan-24", "1/1/2024" etc.
        date_raw = cols[month_col].strip()
        date_str: Optional[str] = None
        for fmt in ("%B %Y", "%b-%y", "%b %Y", "%m/%d/%Y", "%Y-%m-%d", "%b-%Y"):
            try:
                dt = datetime.strptime(date_raw, fmt)
                date_str = dt.strftime("%Y-%m-01")
                break
            except ValueError:
                continue

        if not date_str:
            continue

        # ERP value — normalize to percent (Damodaran sometimes gives decimals)
        erp_raw = cols[erp_col].strip().replace("%", "").replace(",", "")
        try:
            erp_val = float(erp_raw)
            if 0 < erp_val < 1.0:   # decimal form like 0.052
                erp_val *= 100.0
            rows.append((date_str, _DAMO_ERP_SERIES, round(erp_val, 4)))
        except ValueError:
            pass

        # Risk-free rate
        if rf_col is not None and rf_col < len(cols):
            rf_raw = cols[rf_col].strip().replace("%", "").replace(",", "")
            try:
                rf_val = float(rf_raw)
                if 0 < rf_val < 1.0:
                    rf_val *= 100.0
                rows.append((date_str, _DAMO_RF_SERIES, round(rf_val, 4)))
            except ValueError:
                pass

    return rows


def sync_damodaran(
    force: bool = False,
    db_path: str = FORM25_DB_PATH,
) -> dict:
    """
    Fetch and cache Damodaran's implied ERP monthly series.
    Refreshes at most once per 30 days unless force=True.
    """
    init_macro_tables(db_path)

    if not force and not _needs_refresh(_DAMO_ERP_SERIES, max_age_days=30, db_path=db_path):
        print("  Damodaran ERP: up to date, skipping")
        return {"status": "skipped"}

    print(f"  Damodaran ERP: fetching...", end=" ", flush=True)
    try:
        resp = httpx.get(_DAMO_ERP_URL, headers=web_headers(), timeout=30, follow_redirects=True)
        resp.raise_for_status()

        rows = _parse_damodaran_csv(resp.text)
        if not rows:
            # Try an alternate URL (Damodaran occasionally moves files)
            alt_url = "https://pages.stern.nyu.edu/~adamodar/pc/implprem/ERPbymonth.csv"
            resp2 = httpx.get(alt_url, headers=headers, timeout=30, follow_redirects=True)
            if resp2.status_code == 200:
                rows = _parse_damodaran_csv(resp2.text)

        if not rows:
            print("parse failed — check URL or CSV format")
            return {"error": "no rows parsed", "raw_preview": resp.text[:300]}

        by_series: dict[str, list[tuple[str, float]]] = {}
        for date_str, sid, val in rows:
            by_series.setdefault(sid, []).append((date_str, val))

        for sid, obs in by_series.items():
            # Sort by date before inserting
            obs.sort(key=lambda x: x[0])
            _bulk_upsert_observations(sid, obs, db_path)

        erp_obs = by_series.get(_DAMO_ERP_SERIES, [])
        _upsert_series_meta(
            _DAMO_ERP_SERIES, "DAMODARAN",
            "Damodaran Implied Equity Risk Premium (%)",
            erp_obs[0][0] if erp_obs else None,
            erp_obs[-1][0] if erp_obs else None,
            db_path,
        )
        _upsert_series_meta(
            _DAMO_RF_SERIES, "DAMODARAN",
            "Damodaran Risk-Free Rate / T-Bond Rate (%)",
            None, None, db_path,
        )

        print(f"{len(erp_obs)} monthly ERP observations")
        return {"erp_rows": len(erp_obs), "latest": erp_obs[-1][0] if erp_obs else None}

    except Exception as e:
        print(f"ERROR: {e}")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Master sync
# ---------------------------------------------------------------------------

def sync_all_macro(
    start_date: str = "2018-01-01",
    force: bool = False,
    db_path: str = FORM25_DB_PATH,
) -> dict:
    """
    Sync all macro sources: FRED -> Treasury -> Damodaran.
    Incremental and idempotent — safe to run repeatedly.
    Typical runtime: 30-60 seconds on first run, <5s on subsequent runs.
    """
    init_macro_tables(db_path)
    start_year = int(start_date[:4])

    print("=" * 60)
    print("Form25 macro sync")
    print("=" * 60)

    print(f"\n[1/3] FRED ({len(FRED_SERIES)} series)...")
    fred = sync_fred(start_date=start_date, force=force, db_path=db_path)

    print(f"\n[2/3] US Treasury yield curve ({date.today().year - start_year + 1} years)...")
    treasury = sync_treasury(start_year=start_year, force=force, db_path=db_path)

    print("\n[3/3] Damodaran ERP...")
    damo = sync_damodaran(force=force, db_path=db_path)

    print("\nMacro sync complete.")
    return {"fred": fred, "treasury": treasury, "damodaran": damo}


# ---------------------------------------------------------------------------
# Query interface — used by backtest and signal generation
# ---------------------------------------------------------------------------

def get_macro_on_date(
    target_date: str,
    db_path: str = FORM25_DB_PATH,
) -> dict:
    """
    Return all macro indicators as of a given date.

    For each series: returns the most recent observation on or before target_date.
    Monthly series (FRED monthly, Damodaran) will show the last month-start value.

    Returns dict with keys:
        date, fed_funds_rate, yield_spread_10y2y, yield_spread_10y3m,
        vix, hy_spread, cpi_yoy, treas_2y, treas_10y, treas_30y,
        erp_implied, rf_rate_damo,
        + derived labels: macro_regime, yield_curve_shape, risk_environment
    """
    conn = _get_conn(db_path)

    def _last(series_id: str) -> Optional[float]:
        row = conn.execute(
            """SELECT value FROM macro_data
               WHERE series_id = ? AND date <= ? AND value IS NOT NULL
               ORDER BY date DESC LIMIT 1""",
            (series_id, target_date),
        ).fetchone()
        return float(row[0]) if row else None

    cpi_now = _last("CPIAUCSL")
    cpi_yoy: Optional[float] = None
    if cpi_now is not None:
        year_ago = (
            datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=365)
        ).strftime("%Y-%m-%d")
        row = conn.execute(
            """SELECT value FROM macro_data
               WHERE series_id = 'CPIAUCSL' AND date <= ? AND value IS NOT NULL
               ORDER BY date DESC LIMIT 1""",
            (year_ago,),
        ).fetchone()
        if row and row[0]:
            cpi_yoy = round((cpi_now - float(row[0])) / float(row[0]) * 100, 2)

    fed_funds      = _last("DFF")
    spread_10y2y   = _last("T10Y2Y")
    spread_10y3m   = _last("T10Y3M")
    vix            = _last("VIXCLS")
    hy_spread      = _last("BAMLH0A0HYM2")
    treas_2y       = _last("TREAS_2Y")
    treas_10y      = _last("TREAS_10Y")
    treas_30y      = _last("TREAS_30Y")
    erp_implied    = _last(_DAMO_ERP_SERIES)
    rf_rate_damo   = _last(_DAMO_RF_SERIES)

    conn.close()

    result = {
        "date":               target_date,
        "fed_funds_rate":     fed_funds,
        "yield_spread_10y2y": spread_10y2y,
        "yield_spread_10y3m": spread_10y3m,
        "vix":                vix,
        "hy_spread":          hy_spread,
        "cpi_yoy":            cpi_yoy,
        "treas_2y":           treas_2y,
        "treas_10y":          treas_10y,
        "treas_30y":          treas_30y,
        "erp_implied":        erp_implied,
        "rf_rate_damo":       rf_rate_damo,
        "yield_curve_shape":  _classify_yield_curve(spread_10y2y, spread_10y3m),
        "macro_regime":       _classify_macro_regime(fed_funds, cpi_yoy),
        "risk_environment":   _classify_risk_environment(vix, hy_spread),
    }
    return result


def _classify_yield_curve(
    spread_10y2y: Optional[float],
    spread_10y3m: Optional[float],
) -> str:
    """
    inverted = 10Y-3M < -0.25  (historically strongest recession predictor)
    flat     = spread between -0.25 and +0.25
    normal   = 10Y-3M > +0.25
    """
    primary = spread_10y3m if spread_10y3m is not None else spread_10y2y
    if primary is None:
        return "unknown"
    if primary < -0.25:
        return "inverted"
    if primary < 0.25:
        return "flat"
    return "normal"


def _classify_macro_regime(
    fed_funds: Optional[float],
    cpi_yoy: Optional[float],
) -> str:
    """
    restrictive   = Fed funds >= 4%  OR  (>= 2% AND CPI > 3%)
    neutral       = Fed funds 2-4%, CPI 2-3%
    accommodative = Fed funds < 2%
    """
    if fed_funds is None:
        return "unknown"
    if fed_funds >= 4.0:
        return "restrictive"
    if fed_funds >= 2.0:
        if cpi_yoy is not None and cpi_yoy > 3.0:
            return "restrictive"
        return "neutral"
    return "accommodative"


def _classify_risk_environment(
    vix: Optional[float],
    hy_spread: Optional[float],
) -> str:
    """
    elevated = VIX > 25 OR HY spread > 500 bps
               (tight credit conditions, hard for small biotechs to raise capital)
    moderate = VIX 15-25 OR HY 350-500
    low      = VIX < 15 AND HY < 350
    """
    if vix is None and hy_spread is None:
        return "unknown"
    if (vix is not None and vix > 25) or (hy_spread is not None and hy_spread > 5.0):
        return "elevated"
    if (vix is not None and vix > 15) or (hy_spread is not None and hy_spread > 3.5):
        return "moderate"
    return "low"


def get_macro_stats(db_path: str = FORM25_DB_PATH) -> dict:
    """Return coverage statistics for the macro database."""
    init_macro_tables(db_path)
    conn = _get_conn(db_path)
    series_rows = conn.execute(
        "SELECT series_id, source, description, earliest_date, latest_date, last_synced_at "
        "FROM macro_series ORDER BY source, series_id"
    ).fetchall()
    result = []
    total_obs = 0
    for r in series_rows:
        count = conn.execute(
            "SELECT COUNT(*) FROM macro_data WHERE series_id = ?", (r[0],)
        ).fetchone()[0]
        total_obs += count
        result.append({
            "series_id": r[0], "source": r[1], "description": r[2],
            "earliest": r[3], "latest": r[4],
            "last_synced": r[5], "row_count": count,
        })
    conn.close()
    return {
        "series": result,
        "total_series": len(result),
        "total_observations": total_obs,
    }


if __name__ == "__main__":
    result = sync_all_macro(start_date="2018-01-01")
    stats = get_macro_stats()
    print("\nMacro DB coverage:")
    for s in stats["series"]:
        print(
            f"  {s['series_id']:20s} {s['source']:12s} "
            f"{s['earliest'] or '?':10s} -> {s['latest'] or '?':10s}  "
            f"({s['row_count']} rows)"
        )
    print(f"\nTotal: {stats['total_observations']} observations across {stats['total_series']} series")

    # Quick test
    sample = get_macro_on_date("2023-06-15")
    print("\nSample macro snapshot 2023-06-15:")
    for k, v in sample.items():
        print(f"  {k}: {v}")
