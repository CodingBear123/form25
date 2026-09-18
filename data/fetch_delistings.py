"""
Reconstruct the historical biotech universe from SEC EDGAR — including the
companies that no longer exist.

Why this module exists
----------------------
`sec/edgar.py` resolves tickers through company_tickers.json, which lists only
*current* registrants. Clovis Oncology, Sorrento, Athersys and Zosano are all
absent from it. Any universe built from that file is survivors-only, and a
backtest run against it books no bankruptcies.

EDGAR does retain the dead companies; they are just reachable by a different
route. Three facts make this work:

  1. browse-edgar filtered by SIC code lists every company that ever filed
     under that classification, active or not.
  2. The submissions JSON for a dead CIK still carries its SIC code and its
     Form 25 / Form 15 filings, which date the delisting precisely.
  3. The ticker symbol is stripped from the submissions JSON on deregistration
     (`tickers: []`), but survives on the cover page of the company's last
     10-K, which is still archived.

No API key, no paid provider. SEC asks for <10 requests/second and a
descriptive User-Agent; both are honoured below.
"""

from __future__ import annotations

import html
import os
import re
import sys
import time
from typing import Iterator, Optional

import httpx

# Allow running directly as a script (python data/fetch_delistings.py), the
# same bootstrap backtest/sync_universe.py uses.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.config import sec_headers  # noqa: E402
from utils.db import get_connection  # noqa: E402

_BROWSE_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_ARCHIVE_DOC = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{doc}"

# SIC codes covering the biotech/pharma space.
#   2834 Pharmaceutical Preparations
#   2835 In Vitro & In Vivo Diagnostic Substances
#   2836 Biological Products
#   8731 Commercial Physical & Biological Research
BIOTECH_SIC_CODES = ["2834", "2835", "2836", "8731"]

# SEC fair-access limit is 10 req/s. Stay comfortably under it.
_RATE_LIMIT_DELAY = 0.12

# Forms that mark the end of a listing.
_DELISTING_FORMS = ("25", "25-NSE")
_DEREGISTRATION_FORMS = ("15-12B", "15-12G", "15-15D", "15F-12B", "15F-12G")

# 8-K item number for "Bankruptcy or Receivership".
_BANKRUPTCY_ITEM = "1.03"

# Periodic reports. Continuing to file these after a Form 25 proves the
# company outlived the delisting event.
_PERIODIC_FORMS = ("10-K", "10-Q", "20-F", "40-F", "10-K/A", "10-Q/A")

# How long after a Form 25 a straggler filing may still arrive before we
# conclude the company never stopped reporting. Two quarters covers a late
# 10-Q plus the annual report that follows a mid-year exchange transfer.
_STILL_REPORTING_GRACE_DAYS = 200


def _add_days(date_str: str, days: int) -> str:
    """Shift a YYYY-MM-DD string by *days*, returning the same format."""
    from datetime import datetime, timedelta

    return (
        datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)
    ).strftime("%Y-%m-%d")


# A multi-hour scan will hit transient SEC timeouts. Without retries a single
# blip aborts the whole run, which is how the first full attempt died after
# enumerating 2,052 CIKs and saved nothing.
_MAX_RETRIES = 4
_BACKOFF_BASE = 2.0


def _get(url: str, **kwargs) -> httpx.Response:
    """GET with the SEC User-Agent, fair-access delay, and retry on transience."""
    last_exc: Optional[Exception] = None
    headers = sec_headers()

    for attempt in range(_MAX_RETRIES):
        try:
            resp = httpx.get(url, headers=headers, timeout=60, **kwargs)
            time.sleep(_RATE_LIMIT_DELAY)
            resp.raise_for_status()
            return resp
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
        except httpx.HTTPStatusError as e:
            # 429/5xx are worth retrying; 404 and friends are not.
            if e.response.status_code not in (429, 500, 502, 503, 504):
                raise
            last_exc = e

        if attempt < _MAX_RETRIES - 1:
            time.sleep(_BACKOFF_BASE ** attempt)

    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Step 1 — who ever existed
# ---------------------------------------------------------------------------

def fetch_ciks_by_sic(sic: str, max_pages: int = 60) -> dict[str, str]:
    """
    Every CIK that has filed a 10-K under this SIC code, active or dead.

    browse-edgar paginates 100 at a time via `start`. We stop when a page
    yields no CIK we haven't already seen, which handles the final page
    without needing to parse the "next" control.
    """
    found: dict[str, str] = {}
    start = 0

    for _ in range(max_pages):
        resp = _get(_BROWSE_URL, params={
            "action": "getcompany",
            "SIC": sic,
            "type": "10-K",
            "owner": "include",
            "count": 100,
            "start": start,
        })

        rows = re.findall(
            r'CIK=(\d{10})&[^>]*>\s*([^<]+?)\s*</a>\s*</td>\s*<td[^>]*>\s*([^<]*?)\s*</td>',
            resp.text,
        )
        if not rows:
            # Fall back to bare CIK extraction if the row layout shifts.
            rows = [(c, "", "") for c in re.findall(r'CIK=(\d{10})', resp.text)]

        fresh = [r for r in rows if r[0] not in found]
        for cik, _link_text, name in rows:
            found.setdefault(cik, html.unescape(name).strip())

        if not fresh:
            break
        start += 100

    return found


def fetch_biotech_ciks(sic_codes: Optional[list[str]] = None) -> dict[str, str]:
    """Union of `fetch_ciks_by_sic` across the biotech SIC codes."""
    if sic_codes is None:
        sic_codes = BIOTECH_SIC_CODES

    universe: dict[str, str] = {}
    for sic in sic_codes:
        found = fetch_ciks_by_sic(sic)
        new = len(set(found) - set(universe))
        universe.update({k: v for k, v in found.items() if k not in universe})
        print(f"  SIC {sic}: {len(found)} CIKs ({new} new, total {len(universe)})")
    return universe


# ---------------------------------------------------------------------------
# Step 2 — when each one died, and why
# ---------------------------------------------------------------------------

def _live_ticker_map() -> dict[str, str]:
    """CIK -> ticker for companies still registered. Cheap; one request."""
    data = _get(_TICKERS_URL).json()
    return {
        str(e["cik_str"]).zfill(10): e["ticker"].upper()
        for e in data.values()
    }


def _iter_filing_block(block: dict) -> Iterator[tuple[str, str, str, str, str]]:
    """Yield (form, filing_date, accession, primary_doc, items) from one block."""
    forms = block.get("form", [])
    for i, form in enumerate(forms):
        yield (
            form,
            block.get("filingDate", [""] * len(forms))[i],
            block.get("accessionNumber", [""] * len(forms))[i],
            block.get("primaryDocument", [""] * len(forms))[i],
            block.get("items", [""] * len(forms))[i],
        )


def _iter_filings(submissions: dict) -> Iterator[tuple[str, str, str, str, str]]:
    """
    Every filing for a company, oldest included.

    `filings.recent` holds only the ~1000 most recent filings; long-lived
    filers spill the rest into paginated files listed under `filings.files`.
    Reading `recent` alone makes a company look as though it first filed a
    few years ago, which would wrongly screen it out of early backtest days.
    """
    filings = submissions.get("filings", {})
    yield from _iter_filing_block(filings.get("recent", {}))

    for extra in filings.get("files", []):
        name = extra.get("name")
        if not name:
            continue
        try:
            block = _get(f"https://data.sec.gov/submissions/{name}").json()
        except Exception:
            continue
        yield from _iter_filing_block(block)


def recover_ticker_from_cover_page(cik: str, submissions: dict) -> Optional[str]:
    """
    Pull the trading symbol off the cover page of the most recent 10-K.

    Deregistration clears `tickers` in the submissions JSON, but the cover
    page's "Securities registered pursuant to Section 12(b)" table still names
    the symbol. Returns None if no 10-K exists or the table can't be parsed —
    the caller should treat that as "ticker unknown", not as an error.
    """
    for form, _date, acc, doc, _items in _iter_filings(submissions):
        if form != "10-K" or not doc:
            continue

        url = _ARCHIVE_DOC.format(
            cik_int=int(cik), acc=acc.replace("-", ""), doc=doc
        )
        try:
            raw = _get(url).text
        except Exception:
            return None

        text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
        text = re.sub(r"\s+", " ", text)

        # The symbol sits between the security description and the exchange
        # name. Look for an all-caps 1-5 letter token in that window.
        window = re.search(
            r"Trading\s*Symbol.{0,400}", text, re.I
        )
        if not window:
            return None
        candidates = re.findall(r"\b([A-Z]{1,5})\b", window.group(0))
        noise = {
            "COMMON", "STOCK", "PAR", "VALUE", "PER", "SHARE", "THE", "NAME",
            "OF", "EACH", "ON", "WHICH", "REGISTERED", "EXCHANGE", "NASDAQ",
            "NYSE", "LLC", "INC", "GLOBAL", "SELECT", "MARKET", "CAPITAL",
            "TITLE", "CLASS", "SYMBOL", "SYMBOLS", "AND", "A", "B", "N",
            # Regulatory/venue boilerplate that sits in the same table.
            "FINRA", "SEC", "OTC", "OTCQB", "OTCQX", "PINK", "CBOE", "AMEX",
            "NYSEAMER", "ARCA", "BATS", "IEX", "NA", "NONE", "NOT", "NO",
            "USD", "US", "STOCKS", "SHARES", "EACH", "PAR", "PLC", "CORP",
        }
        for c in candidates:
            if c not in noise:
                return c
        return None

    return None


def get_listing_facts(cik: str, live_tickers: dict[str, str]) -> Optional[dict]:
    """
    Listing window and fate for one CIK.

    Returns a dict shaped for the `universe` table, or None if the CIK has no
    usable filing history.
    """
    try:
        submissions = _get(_SUBMISSIONS_URL.format(cik=cik)).json()
    except Exception as e:
        print(f"    {cik}: submissions fetch failed — {e}")
        return None

    filings = list(_iter_filings(submissions))
    if not filings:
        return None

    # NOTE: this is the first SEC filing, which precedes the IPO — companies
    # file privately for years first (Moderna: 2016 filings, Dec 2018 IPO).
    # It is deliberately a conservative floor, not a listing date: it can only
    # ever exclude days on which the company demonstrably did not exist, so it
    # cannot over-screen. Actual IPO timing is handled for free by the price
    # DB, which has no bars before the first trade.
    dates = [d for _f, d, _a, _p, _i in filings if d]
    first_filing = min(dates) if dates else None

    effective_delisting, reason = determine_fate(filings)

    ticker = live_tickers.get(cik)
    if ticker is None and effective_delisting:
        # Only pay for the cover-page fetch on names we can't resolve cheaply.
        ticker = recover_ticker_from_cover_page(cik, submissions)

    return {
        "cik": cik,
        "ticker": ticker,
        "company_name": submissions.get("name", ""),
        "sic": submissions.get("sic", ""),
        "listed_from": first_filing,
        "delisted_date": effective_delisting,
        "delisting_reason": reason,
    }


def determine_fate(
    filings: list[tuple[str, str, str, str, str]],
) -> tuple[Optional[str], Optional[str]]:
    """
    Decide whether a filing history describes a company that stopped existing.

    Returns (delisted_date, reason); (None, None) means still listed.

    This is the correctness-critical heuristic in this module, kept pure so it
    can be tested without touching the network.
    """
    delisted_date: Optional[str] = None
    dereg_date: Optional[str] = None
    bankruptcy = False

    for form, date, _acc, _doc, items in filings:
        if form in _DELISTING_FORMS:
            # Keep the earliest — a company can file more than one.
            if delisted_date is None or date < delisted_date:
                delisted_date = date
        elif form in _DEREGISTRATION_FORMS:
            if dereg_date is None or date < dereg_date:
                dereg_date = date
        elif form.startswith("8-K") and items and _BANKRUPTCY_ITEM in items:
            bankruptcy = True

    # Form 25 is the delisting itself; Form 15 only follows it. Prefer 25.
    candidate = delisted_date or dereg_date

    # A Form 25 alone does NOT mean the company died. It is filed whenever any
    # class of security is removed from an exchange — a warrant expiring, a
    # note maturing, a transfer between NYSE and Nasdaq. Amgen, ADMA and CASI
    # all have one and are still trading.
    #
    # The signal that separates a dead company from a housekeeping filing is
    # whether it kept filing periodic reports afterwards. A company that is
    # genuinely gone stops filing 10-Ks.
    last_periodic = None
    for form, date, _acc, _doc, _items in filings:
        if form in _PERIODIC_FORMS and date:
            if last_periodic is None or date > last_periodic:
                last_periodic = date

    effective_delisting = candidate
    if candidate and last_periodic and last_periodic > _add_days(candidate, _STILL_REPORTING_GRACE_DAYS):
        # Still filing well after the Form 25 — it survived the event.
        effective_delisting = None
        delisted_date = None
        dereg_date = None

    if effective_delisting is None:
        return None, None
    if bankruptcy:
        return effective_delisting, "bankruptcy"
    if delisted_date and dereg_date:
        # Delisted then deregistered without a bankruptcy 8-K — most often an
        # acquisition or a going-private transaction.
        return effective_delisting, "acquisition_or_private"
    return effective_delisting, "unknown"


# ---------------------------------------------------------------------------
# Step 3 — persist
# ---------------------------------------------------------------------------

def _init_scan_cache(conn) -> None:
    """
    Per-CIK cache of resolved listing facts.

    Exists so a long scan is resumable: each CIK is written as soon as it
    resolves, and a re-run skips whatever is already here. Without it a crash
    at hour two discards hour two.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS delisting_scan_cache (
            cik              TEXT PRIMARY KEY,
            ticker           TEXT,
            company_name     TEXT,
            sic              TEXT,
            listed_from      TEXT,
            delisted_date    TEXT,
            delisting_reason TEXT,
            scanned_at       TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def _load_scanned_ciks(conn) -> dict[str, dict]:
    """Facts already resolved in a previous run, keyed by CIK."""
    try:
        rows = conn.execute(
            "SELECT cik, ticker, company_name, sic, listed_from, "
            "delisted_date, delisting_reason FROM delisting_scan_cache"
        ).fetchall()
    except Exception:
        return {}
    return {
        r[0]: {
            "cik": r[0], "ticker": r[1], "company_name": r[2], "sic": r[3],
            "listed_from": r[4], "delisted_date": r[5], "delisting_reason": r[6],
        }
        for r in rows
    }


def _cache_scan_result(conn, facts: dict) -> None:
    conn.execute("""
        INSERT INTO delisting_scan_cache
            (cik, ticker, company_name, sic, listed_from, delisted_date, delisting_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cik) DO UPDATE SET
            ticker=excluded.ticker, company_name=excluded.company_name,
            sic=excluded.sic, listed_from=excluded.listed_from,
            delisted_date=excluded.delisted_date,
            delisting_reason=excluded.delisting_reason,
            scanned_at=datetime('now')
    """, (
        facts["cik"], facts.get("ticker"), facts.get("company_name"),
        str(facts.get("sic", "")), facts.get("listed_from"),
        facts.get("delisted_date"), facts.get("delisting_reason"),
    ))


def save_listing_facts(
    records: list[dict],
    sector: str = "biotech",
    db_path: Optional[str] = None,
) -> int:
    """Upsert listing windows into the universe table. Returns rows written."""
    conn = get_connection(db_path) if db_path else get_connection()
    written = 0
    try:
        for r in records:
            if not r.get("ticker"):
                continue
            conn.execute("""
                INSERT INTO universe
                    (ticker, sector, company_name, source, active,
                     listed_from, delisted_date, delisting_reason)
                VALUES (?, ?, ?, 'sec_edgar', ?, ?, ?, ?)
                ON CONFLICT(ticker, sector) DO UPDATE SET
                    company_name     = excluded.company_name,
                    listed_from      = excluded.listed_from,
                    delisted_date    = excluded.delisted_date,
                    delisting_reason = excluded.delisting_reason,
                    active           = excluded.active,
                    updated_at       = datetime('now')
            """, (
                r["ticker"].upper(), sector, r.get("company_name", ""),
                0 if r.get("delisted_date") else 1,
                r.get("listed_from"), r.get("delisted_date"),
                r.get("delisting_reason"),
            ))
            written += 1
        conn.commit()
    finally:
        conn.close()
    return written


def build_historical_universe(
    sic_codes: Optional[list[str]] = None,
    limit: Optional[int] = None,
    db_path: Optional[str] = None,
) -> dict:
    """
    Full rebuild: enumerate biotech CIKs, resolve each one's listing window,
    and write the result to the universe table.

    Expect this to take 10-30 minutes — it is rate limited to SEC's fair-access
    ceiling and touches a few thousand CIKs. It only needs to be run
    periodically, not per backtest.
    """
    print("Step 1: enumerating biotech CIKs from EDGAR...")
    ciks = fetch_biotech_ciks(sic_codes)
    print(f"  {len(ciks)} distinct CIKs")

    if limit:
        ciks = dict(list(ciks.items())[:limit])
        print(f"  limited to {len(ciks)} for this run")

    print("\nStep 2: resolving listing windows (live ticker map first)...")
    live = _live_ticker_map()
    print(f"  {len(live)} currently-registered tickers")

    scan_conn = get_connection(db_path) if db_path else get_connection()
    _init_scan_cache(scan_conn)
    already = _load_scanned_ciks(scan_conn)
    if already:
        print(f"  resuming: {len(already)} CIKs already scanned, skipping those")

    records: list[dict] = list(already.values())
    todo = [c for c in ciks if c not in already]

    try:
        for i, cik in enumerate(todo):
            if i and i % 100 == 0:
                print(f"  {i}/{len(todo)} resolved (+{len(already)} cached)...")
            try:
                facts = get_listing_facts(cik, live)
            except Exception as e:
                # One bad CIK must not end the scan.
                print(f"    {cik}: skipped — {e}")
                continue
            if facts:
                records.append(facts)
                # Persist immediately so a crash costs one CIK, not the run.
                _cache_scan_result(scan_conn, facts)
                if i % 25 == 0:
                    scan_conn.commit()
        scan_conn.commit()
    except KeyboardInterrupt:
        scan_conn.commit()
        print(f"\n  interrupted — {len(records)} CIKs saved, re-run to resume")
        raise
    finally:
        scan_conn.commit()
        scan_conn.close()

    delisted = [r for r in records if r["delisted_date"]]
    unresolved = [r for r in delisted if not r["ticker"]]

    print(f"\nStep 3: writing to universe table...")
    written = save_listing_facts(records, db_path=db_path)

    summary = {
        "ciks_scanned": len(ciks),
        "records": len(records),
        "delisted": len(delisted),
        "delisted_without_ticker": len(unresolved),
        "written": written,
        "by_reason": {
            reason: sum(1 for r in delisted if r["delisting_reason"] == reason)
            for reason in ("bankruptcy", "acquisition_or_private", "unknown")
        },
    }
    print(f"  wrote {written} rows; {len(delisted)} delisted "
          f"({len(unresolved)} without a recoverable ticker)")
    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Rebuild historical biotech universe from SEC")
    parser.add_argument("--limit", type=int, help="Only process N CIKs (for testing)")
    parser.add_argument("--sic", nargs="*", help="SIC codes (default: biotech set)")
    args = parser.parse_args()

    result = build_historical_universe(sic_codes=args.sic, limit=args.limit)
    print("\nSummary:")
    for k, v in result.items():
        print(f"  {k}: {v}")
