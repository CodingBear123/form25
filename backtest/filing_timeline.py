"""
Filing timeline builder for the Form25 backtest engine.

The core problem with daily backtesting:
    We need the "correct" fundamentals for any historical date — meaning the
    fundamentals that were actually available on that date, not future data.

    Calling SEC EDGAR live for every (ticker, date) pair is far too slow for
    5 years × 250 tickers × ~252 trading days = ~315,000 lookups.

Solution — two-tier caching:
    Tier 1 (this module):
        For each ticker, fetch the complete SEC XBRL history ONCE and build a
        sorted list of (filed_date, fundamentals_dict) called a "filing timeline".
        Store it in SQLite table: filing_timelines (ticker, data JSON).

        For any backtest date D, binary-search the timeline to find the most
        recent filing where filed_date <= D. This gives correct point-in-time
        fundamentals with zero lookahead bias.

        Re-fetch only if the cache is > 7 days old (new 10-Q may have dropped).

    Tier 2 (daily_runner.py):
        Full CompanySnapshot objects are cached in the existing `snapshots` table.
        If (ticker, date) already exists, skip entirely.

Public interface:
    get_fundamentals_on_date(ticker, date_str)  ->  dict | None
    build_filing_timeline(ticker)               ->  list[dict] (also caches)
    prime_timeline_cache(tickers)               ->  batch-build all timelines
"""

import sqlite3
import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional
import bisect

import httpx

from utils.config import FORM25_DB_PATH, sec_headers

_XBRL_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# How long a negative (empty) cache entry survives. Short, so a transient SEC
# outage does not suppress a ticker for the full 7-day positive TTL.
_NEGATIVE_CACHE_TTL_DAYS = 1

# SEC fair-access ceiling is 10 requests/second; exceeding it earns a block
# that surfaces as "[Errno 61] Connection refused".
_SEC_MIN_INTERVAL = 0.12
_last_sec_call = 0.0


def _sec_throttle() -> None:
    """Space out calls to data.sec.gov to stay inside SEC's rate limit."""
    global _last_sec_call
    elapsed = time.monotonic() - _last_sec_call
    if elapsed < _SEC_MIN_INTERVAL:
        time.sleep(_SEC_MIN_INTERVAL - elapsed)
    _last_sec_call = time.monotonic()

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_conn(db_path: str = FORM25_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_timeline_table(db_path: str = FORM25_DB_PATH) -> None:
    """Create filing_timelines table if it doesn't exist."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = _get_conn(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS filing_timelines (
            ticker          TEXT PRIMARY KEY,
            cik             TEXT,
            entity_name     TEXT,
            data            TEXT NOT NULL,
            filing_count    INTEGER,
            earliest_date   TEXT,
            latest_date     TEXT,
            fetched_at      TEXT NOT NULL,
            expires_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_timeline_ticker ON filing_timelines(ticker);
    """)
    conn.commit()
    conn.close()


def _store_timeline(
    ticker: str,
    cik: str,
    entity_name: str,
    timeline: list[dict],
    ttl_days: int = 7,
    db_path: str = FORM25_DB_PATH,
    allow_empty: bool = False,
) -> None:
    """
    Cache a ticker's filing timeline.

    `allow_empty` records a *negative* result — "we asked SEC and there is
    nothing here". That distinction matters: without it, a ticker whose XBRL
    fetch fails is retried on every single trading day of the backtest, which
    is ~1,800 SEC requests per bad ticker and gets the whole run rate-limited
    into "Connection refused".
    """
    if not timeline and not allow_empty:
        return
    # Use naive UTC strings throughout so comparisons are consistent
    now     = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    expires = (datetime.utcnow() + timedelta(days=ttl_days)).strftime("%Y-%m-%dT%H:%M:%S")
    dates = [e["filed_date"] for e in timeline if e.get("filed_date")]
    conn = _get_conn(db_path)
    conn.execute("""
        INSERT INTO filing_timelines
            (ticker, cik, entity_name, data, filing_count, earliest_date, latest_date, fetched_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            cik=excluded.cik, entity_name=excluded.entity_name,
            data=excluded.data, filing_count=excluded.filing_count,
            earliest_date=excluded.earliest_date, latest_date=excluded.latest_date,
            fetched_at=excluded.fetched_at, expires_at=excluded.expires_at
    """, (
        ticker.upper(), cik, entity_name,
        json.dumps(timeline),
        len(timeline),
        min(dates) if dates else None,
        max(dates) if dates else None,
        now, expires,
    ))
    conn.commit()
    conn.close()


def _load_timeline_from_cache(
    ticker: str,
    db_path: str = FORM25_DB_PATH,
) -> Optional[list[dict]]:
    """Return cached timeline if not expired, else None."""
    conn = _get_conn(db_path)
    # Compare expires_at as plain string against current UTC time in ISO format.
    # Both are stored/compared as naive ISO strings (no timezone suffix) so
    # string comparison is safe and consistent.
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    row = conn.execute(
        "SELECT data FROM filing_timelines WHERE ticker = ? AND expires_at > ?",
        (ticker.upper(), now),
    ).fetchone()
    conn.close()
    if row:
        return json.loads(row["data"])
    return None


# ---------------------------------------------------------------------------
# XBRL helpers — extract point-in-time fundamentals from company facts
# ---------------------------------------------------------------------------

_CASH_CONCEPTS = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsAndShortTermInvestments",
    "CashAndCashEquivalentsAndShortTermInvestments",
    "CashAndCashEquivalentsAndShortTermInvestmentsAtCarryingValue",
]

_OPEX_CONCEPTS = [
    "OperatingExpenses",
    "CostsAndExpenses",
    "OperatingCostsAndExpenses",
    "ResearchAndDevelopmentExpense",
]

_OCF_CONCEPTS = [
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
]

_SHARES_CONCEPTS = [
    "CommonStockSharesOutstanding",
    "EntityCommonStockSharesOutstanding",
]


def _extract_all_filings(facts: dict, concepts: list[str], unit: str = "USD") -> list[dict]:
    """
    Extract every individual filing observation for any of the given concepts.
    Returns list of dicts with: value, filed_date, end_date, form, concept.
    Sorted by filed_date ascending.
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    dei     = facts.get("facts", {}).get("dei", {})
    all_entries: list[dict] = []

    for concept in concepts:
        data = us_gaap.get(concept) or dei.get(concept)
        if not data:
            continue
        units = data.get("units", {})
        entries = units.get(unit, units.get("shares", []))
        for e in entries:
            if e.get("form") not in ("10-Q", "10-K"):
                continue
            if not e.get("filed") or e.get("val") is None:
                continue
            all_entries.append({
                "concept":    concept,
                "value":      float(e["val"]),
                "filed_date": e["filed"],
                "end_date":   e.get("end", ""),
                "form":       e["form"],
                "fp":         e.get("fp", ""),
                "fy":         e.get("fy", ""),
            })
        if all_entries:
            break  # Use first concept that has data

    all_entries.sort(key=lambda x: x["filed_date"])
    return all_entries


def _compute_burn_from_ocf_history(
    ocf_entries: list[dict],
    as_of_date: str,
    lookback_quarters: int = 4,
) -> Optional[float]:
    """
    Compute monthly burn rate from operating cash flow history,
    using only filings available as of as_of_date.
    Returns burn_rate_monthly (positive = burning), or None if insufficient data.
    """
    available = [e for e in ocf_entries if e["filed_date"] <= as_of_date]
    if not available:
        return None

    # Group by fiscal year to de-accumulate YTD figures
    by_year: dict = {}
    for e in available:
        fy = e.get("fy", "")
        fp = e.get("fp", "")
        if fy and fp and e["form"] == "10-Q":
            if fy not in by_year:
                by_year[fy] = {}
            by_year[fy][fp] = e["value"]

    quarterly_vals: list[float] = []
    for fy in sorted(by_year.keys(), reverse=True):
        p = by_year[fy]
        q1 = p.get("Q1")
        q2 = p.get("Q2")
        q3 = p.get("Q3")
        if q3 is not None and q2 is not None:
            quarterly_vals.append(q3 - q2)
        if q2 is not None and q1 is not None:
            quarterly_vals.append(q2 - q1)
        if q1 is not None:
            quarterly_vals.append(q1)
        if len(quarterly_vals) >= lookback_quarters:
            break

    if not quarterly_vals:
        return None

    avg_quarterly = sum(quarterly_vals[:lookback_quarters]) / len(quarterly_vals[:lookback_quarters])
    if avg_quarterly < 0:
        return abs(avg_quarterly) / 3.0  # monthly burn (positive number)
    return 0.0  # company has positive operating cash flow


def build_filing_timeline(
    ticker: str,
    cik: str,
    entity_name: str = "",
    force_refresh: bool = False,
    db_path: str = FORM25_DB_PATH,
) -> list[dict]:
    """
    Build a point-in-time filing timeline for a ticker.

    Each entry in the timeline represents a date when new fundamental data
    became available (i.e. a 10-Q or 10-K was filed), and contains the
    fundamentals as they were known on that date.

    The timeline is sorted ascending by filed_date.
    Binary search by date gives O(log n) lookup for any historical date.

    Caches result in SQLite for 7 days.

    Returns list of entries:
    {
        "filed_date":          "2022-08-10",
        "form":                "10-Q",
        "cash":                234_000_000,
        "burn_rate_monthly":   8_500_000,
        "cash_runway_months":  27.5,
        "shares_outstanding":  45_000_000,
        "data_quality":        "full" | "partial" | "insufficient",
    }
    """
    init_timeline_table(db_path)

    if not force_refresh:
        cached = _load_timeline_from_cache(ticker, db_path)
        if cached is not None:
            return cached

    # Built outside the try below: a missing SEC_CONTACT_EMAIL is a config
    # error, and swallowing it there would negative-cache every ticker as
    # "no data" instead of saying what is actually wrong.
    headers = sec_headers()

    # Fetch full XBRL facts from SEC
    try:
        url = _XBRL_URL.format(cik=cik)
        _sec_throttle()
        resp = httpx.get(url, headers=headers, timeout=25)
        resp.raise_for_status()
        facts = resp.json()
    except Exception as e:
        print(f"    {ticker}: XBRL fetch failed — {e}")
        # Remember the failure, or the backtest re-attempts this ticker on
        # every trading day. Short TTL so a transient outage self-heals on the
        # next run rather than poisoning the cache for a week.
        _store_timeline(
            ticker, cik, entity_name or ticker, [],
            ttl_days=_NEGATIVE_CACHE_TTL_DAYS, db_path=db_path, allow_empty=True,
        )
        return []

    if not entity_name:
        entity_name = facts.get("entityName", ticker)

    # Extract all historical filings for each concept
    cash_entries   = _extract_all_filings(facts, _CASH_CONCEPTS, "USD")
    ocf_entries    = _extract_all_filings(facts, _OCF_CONCEPTS, "USD")
    opex_entries   = _extract_all_filings(facts, _OPEX_CONCEPTS, "USD")
    shares_entries = _extract_all_filings(facts, _SHARES_CONCEPTS, "shares")

    # Build a sorted set of all unique filing dates
    all_filed_dates: set[str] = set()
    for entry_list in [cash_entries, ocf_entries, opex_entries, shares_entries]:
        for e in entry_list:
            all_filed_dates.add(e["filed_date"])

    if not all_filed_dates:
        # Genuinely nothing to extract — most often a foreign private issuer
        # that files 20-F/6-K rather than the 10-K/10-Q this parser reads.
        # Cache the negative so we don't ask again every trading day.
        _store_timeline(
            ticker, cik, entity_name, [],
            ttl_days=_NEGATIVE_CACHE_TTL_DAYS, db_path=db_path, allow_empty=True,
        )
        return []

    # For each filing date, compute point-in-time fundamentals
    # using only data available on or before that date
    timeline: list[dict] = []
    sorted_dates = sorted(all_filed_dates)

    for filed_date in sorted_dates:
        # Cash: most recent filing on or before filed_date
        cash_available = [e for e in cash_entries if e["filed_date"] <= filed_date]
        cash = cash_available[-1]["value"] if cash_available else None

        # Shares: most recent filing on or before filed_date
        shares_available = [e for e in shares_entries if e["filed_date"] <= filed_date]
        shares = shares_available[-1]["value"] if shares_available else None

        # Form type of this filing date
        # (use the most significant form filed on this date)
        forms_today = set()
        for el in [cash_entries, ocf_entries, opex_entries, shares_entries]:
            for e in el:
                if e["filed_date"] == filed_date:
                    forms_today.add(e["form"])
        form = "10-K" if "10-K" in forms_today else ("10-Q" if "10-Q" in forms_today else "unknown")

        # Burn rate from operating cash flow history
        burn = _compute_burn_from_ocf_history(ocf_entries, filed_date)

        # Fallback: use operating expenses if no OCF data
        if burn is None:
            opex_available = [e for e in opex_entries if e["filed_date"] <= filed_date]
            if opex_available:
                burn = opex_available[-1]["value"] / 3.0  # most recent quarterly opex

        # Runway
        if cash is not None and burn is not None and burn > 0:
            runway = round(cash / burn, 1)
        else:
            runway = None

        # Data quality
        missing = []
        if cash is None:
            missing.append("cash")
        if burn is None:
            missing.append("burn_rate")
        if shares is None:
            missing.append("shares")

        if {"cash", "burn_rate"} & set(missing):
            quality = "insufficient"
        elif missing:
            quality = "partial"
        else:
            quality = "full"

        timeline.append({
            "filed_date":         filed_date,
            "form":               form,
            "cash":               cash,
            "burn_rate_monthly":  round(burn, 0) if burn is not None else None,
            "cash_runway_months": runway,
            "shares_outstanding": shares,
            "data_quality":       quality,
            "missing_fields":     missing,
        })

    # Sort and deduplicate (keep only entries where something changed)
    timeline.sort(key=lambda x: x["filed_date"])
    deduplicated: list[dict] = []
    prev = None
    for entry in timeline:
        if prev is None or (
            entry["cash"] != prev["cash"] or
            entry["shares_outstanding"] != prev["shares_outstanding"] or
            entry["burn_rate_monthly"] != prev["burn_rate_monthly"]
        ):
            deduplicated.append(entry)
            prev = entry

    _store_timeline(ticker, cik, entity_name, deduplicated, db_path=db_path)
    return deduplicated


# ---------------------------------------------------------------------------
# Point-in-time lookup
# ---------------------------------------------------------------------------

def get_fundamentals_on_date(
    ticker: str,
    target_date: str,
    cik: Optional[str] = None,
    db_path: str = FORM25_DB_PATH,
) -> Optional[dict]:
    """
    Return the fundamentals that were available on target_date for a ticker.
    Uses cached timeline — builds it on first call.

    target_date: ISO string "YYYY-MM-DD"
    Returns the most recent filing entry where filed_date <= target_date,
    or None if no data exists before that date.
    """
    init_timeline_table(db_path)
    timeline = _load_timeline_from_cache(ticker, db_path)

    if timeline is None:
        if cik is None:
            # Try to get CIK from form25.db
            conn = _get_conn(db_path)
            row = conn.execute(
                "SELECT cik FROM fundamentals_cache WHERE ticker = ? LIMIT 1",
                (ticker.upper(),),
            ).fetchone()
            conn.close()
            if row and row[0]:
                cik = row[0]
            else:
                return None  # Can't build without CIK
        timeline = build_filing_timeline(ticker, cik, db_path=db_path)

    if not timeline:
        return None

    # Binary search: find rightmost entry where filed_date <= target_date
    dates = [e["filed_date"] for e in timeline]
    idx = bisect.bisect_right(dates, target_date) - 1
    if idx < 0:
        return None  # No filing before this date

    return timeline[idx]


# ---------------------------------------------------------------------------
# Batch primer — build timelines for a full universe
# ---------------------------------------------------------------------------

def prime_timeline_cache(
    tickers_with_ciks: list[tuple[str, str]],
    force_refresh: bool = False,
    delay: float = 0.5,
    db_path: str = FORM25_DB_PATH,
) -> dict:
    """
    Build and cache filing timelines for a list of (ticker, cik) pairs.

    This is the one-time setup step before running the daily backtest loop.
    Typical runtime: ~1-2 seconds per ticker (one SEC HTTP call each).
    For 250 tickers: ~5-8 minutes. Run once, then fully cached for 7 days.

    Args:
        tickers_with_ciks: list of (ticker, cik) pairs
        force_refresh:     rebuild even if cache is fresh
        delay:             seconds between SEC API calls (SEC rate limit: 10/sec)

    Returns summary dict.
    """
    init_timeline_table(db_path)
    total = len(tickers_with_ciks)
    built  = 0
    skipped = 0
    failed  = 0

    print(f"Priming filing timeline cache for {total} tickers...")
    print("(One SEC API call per ticker — runs once, cached 7 days)")
    print("=" * 55)

    for i, (ticker, cik) in enumerate(tickers_with_ciks):
        # Check if already cached and not expired
        if not force_refresh and _load_timeline_from_cache(ticker, db_path) is not None:
            print(f"  [{i+1}/{total}] {ticker}: cached, skipping")
            skipped += 1
            continue

        print(f"  [{i+1}/{total}] {ticker}...", end=" ", flush=True)
        try:
            timeline = build_filing_timeline(ticker, cik, db_path=db_path)
            if timeline:
                earliest = timeline[0]["filed_date"]
                latest   = timeline[-1]["filed_date"]
                print(f"{len(timeline)} filings ({earliest} -> {latest})")
                built += 1
            else:
                print("no data")
                failed += 1
        except Exception as e:
            print(f"ERROR: {e}")
            failed += 1

        time.sleep(delay)

    print(f"\nCache priming complete: {built} built, {skipped} skipped, {failed} failed")
    return {"total": total, "built": built, "skipped": skipped, "failed": failed}


def get_timeline_cache_stats(db_path: str = FORM25_DB_PATH) -> dict:
    """Return statistics about the filing timeline cache."""
    init_timeline_table(db_path)
    conn = _get_conn(db_path)
    rows = conn.execute("""
        SELECT ticker, filing_count, earliest_date, latest_date, fetched_at, expires_at
        FROM filing_timelines ORDER BY ticker
    """).fetchall()
    conn.close()
    now = datetime.now().isoformat()
    fresh  = sum(1 for r in rows if r[5] and r[5] > now)
    stale  = len(rows) - fresh
    return {
        "total_tickers": len(rows),
        "fresh": fresh,
        "stale": stale,
        "tickers": [
            {
                "ticker": r[0], "filings": r[1],
                "earliest": r[2], "latest": r[3],
                "fetched": r[4], "expires": r[5],
                "is_fresh": bool(r[5] and r[5] > now),
            }
            for r in rows
        ],
    }
