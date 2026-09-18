"""Screen SEC filings for a ticker and return ranked metadata."""

from sec.edgar import get_cik, get_submissions, fetch_filings
from sec.models import FilingMeta
from sec.parser import parse_form4
from datetime import date


def screen_filings(
    ticker: str,
    form_type: str = "8-K",
    limit: int = 10,
) -> list[dict]:
    """
    Return recent SEC filings for *ticker* ordered by filing date descending.

    Each dict matches the FilingMeta schema:
        accession_number, form_type, filed_date, entity_name, cik, description
    """
    cik = get_cik(ticker)
    subs = get_submissions(cik)
    entity_name = subs.get("name", ticker)
    filings = subs.get("filings", {}).get("recent", {})

    accessions = filings.get("accessionNumber", [])
    forms = filings.get("form", [])
    dates = filings.get("filingDate", [])
    descriptions = filings.get("primaryDocDescription", [])

    results: list[dict] = []
    for acc, form, filed, desc in zip(accessions, forms, dates, descriptions):
        if form != form_type:
            continue
        results.append(
            FilingMeta(
                accession_number=acc,
                form_type=form,
                filed_date=date.fromisoformat(filed),
                entity_name=entity_name,
                cik=cik,
                description=desc or "",
            ).model_dump(mode="json")
        )
        if len(results) >= limit:
            break

    return results


def screen_insider_trades(
    ticker: str,
    transaction_type: str = "buy",   # "buy" | "sell" | "all"
    limit: int = 10,
    min_signal_strength: str = "weak",  # "weak" | "moderate" | "strong"
) -> list[dict]:
    """
    Return recent insider transactions for *ticker* from Form 4 filings.

    Filters by transaction_type and min_signal_strength.
    Results ordered by filed_date descending.

    signal_strength hierarchy: strong > moderate > weak
    """
    _strength_rank = {"weak": 0, "moderate": 1, "strong": 2}
    min_rank = _strength_rank.get(min_signal_strength, 0)

    # Get Form 4 filing metadata
    form4_filings = screen_filings(ticker=ticker, form_type="4", limit=50)

    results: list[dict] = []
    for filing_meta in form4_filings:
        if len(results) >= limit:
            break
        try:
            raw = fetch_filings(filing_meta["accession_number"])
            transactions = parse_form4(raw)
            for txn in transactions:
                if transaction_type != "all" and txn["transaction_type"] != transaction_type:
                    continue
                if _strength_rank.get(txn["signal_strength"], 0) < min_rank:
                    continue
                results.append(txn)
                if len(results) >= limit:
                    break
        except Exception:
            # Skip filings that fail to fetch/parse — don't abort the whole screen
            continue

    return results
