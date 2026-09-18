"""Fetch raw SEC filing content from EDGAR."""

import httpx

from utils.config import sec_headers

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
# Correct archive path: edgar/data/ not edgar/full-index/
_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{filename}"

# In-process cache so repeated calls don't re-download the ~1 MB tickers file
_ticker_to_cik: dict[str, str] = {}


def get_cik(ticker: str) -> str:
    """Return zero-padded 10-digit CIK string for *ticker*."""
    ticker = ticker.upper()
    if ticker in _ticker_to_cik:
        return _ticker_to_cik[ticker]

    resp = httpx.get(_TICKERS_URL, headers=sec_headers(), timeout=15)
    resp.raise_for_status()
    for entry in resp.json().values():
        _ticker_to_cik[entry["ticker"].upper()] = str(entry["cik_str"]).zfill(10)

    if ticker not in _ticker_to_cik:
        raise ValueError(f"Ticker '{ticker}' not found in EDGAR company list")
    return _ticker_to_cik[ticker]


def get_submissions(cik: str) -> dict:
    """Return the raw EDGAR submissions JSON for a CIK (zero-padded 10 digits)."""
    url = _SUBMISSIONS_URL.format(cik=cik)
    resp = httpx.get(url, headers=sec_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_filings(accession_number: str) -> dict:
    """
    Fetch raw content and metadata for a filing by accession number.

    accession_number: '0001234567-24-000001' (dashes included)

    Returns:
        accession_number, form_type, filed_date, cik, entity_name, raw_text
    """
    cik = accession_number.split("-")[0].zfill(10)
    accession_nodash = accession_number.replace("-", "")

    subs = get_submissions(cik)
    filings = subs.get("filings", {}).get("recent", {})

    try:
        idx = filings["accessionNumber"].index(accession_number)
    except ValueError:
        raise ValueError(f"Accession {accession_number} not found in submissions for CIK {cik}")

    primary_doc = filings["primaryDocument"][idx]
    form_type = filings["form"][idx]
    filed_date = filings["filingDate"][idx]
    entity_name = subs.get("name", "")

    # Archive URL uses CIK as plain integer (no leading zeros)
    cik_int = str(int(cik))
    url = _ARCHIVE_URL.format(
        cik_int=cik_int,
        accession_nodash=accession_nodash,
        filename=primary_doc,
    )

    doc_resp = httpx.get(url, headers=sec_headers(), timeout=20)
    doc_resp.raise_for_status()

    return {
        "accession_number": accession_number,
        "form_type": form_type,
        "filed_date": filed_date,
        "cik": cik,
        "entity_name": entity_name,
        "raw_text": doc_resp.text,
    }
