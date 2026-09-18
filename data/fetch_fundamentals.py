"""
Fetch structured financial fundamentals from SEC EDGAR XBRL API.

Endpoint: https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json

This returns every financial figure a company has ever reported — cash,
operating expenses, shares outstanding — all structured and dated.
No HTML parsing needed.

Key concepts:
    - us-gaap: standard US accounting taxonomy (most companies)
    - dei: document/entity info (shares outstanding, fiscal year end)
    - Each fact has a list of filings with value, filed date, form type
    - We always take the most recent 10-Q or 10-K value
"""

import httpx
from datetime import date
from typing import Optional

from utils.config import sec_headers

_XBRL_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# XBRL concept names we care about — each has fallbacks in priority order
# because companies use slightly different tag names
_CASH_CONCEPTS = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsAndShortTermInvestments",
    "CashAndCashEquivalentsAndShortTermInvestments",
]

_OPERATING_EXPENSES_CONCEPTS = [
    "OperatingExpenses",
    "CostsAndExpenses",
    "OperatingCostsAndExpenses",
]

_OPERATING_CASH_FLOW_CONCEPTS = [
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashUsedInOperatingActivities",
]

_SHARES_CONCEPTS = [
    "CommonStockSharesOutstanding",
    "EntityCommonStockSharesOutstanding",
]


def _get_company_facts(cik: str) -> dict:
    """Fetch raw XBRL company facts JSON for a CIK (zero-padded 10 digits)."""
    url = _XBRL_URL.format(cik=cik)
    resp = httpx.get(url, headers=sec_headers(), timeout=20)
    resp.raise_for_status()
    return resp.json()


def _get_most_recent_value(
    facts: dict,
    concepts: list[str],
    form_types: tuple[str, ...] = ("10-Q", "10-K"),
    unit: str = "USD",
) -> Optional[tuple[float, str, str]]:
    """
    Search facts for the most recent filed value for any of the given concepts.

    Returns (value, filed_date, form_type) or None if not found.
    Prioritises 10-Q over 10-K for recency, falls back to 10-K.
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    dei = facts.get("facts", {}).get("dei", {})

    for concept in concepts:
        # Check both us-gaap and dei namespaces
        concept_data = us_gaap.get(concept) or dei.get(concept)
        if not concept_data:
            continue

        units = concept_data.get("units", {})
        entries = units.get(unit, units.get("shares", []))

        if not entries:
            continue

        # Filter to annual/quarterly filings only, exclude amendments
        filtered = [
            e for e in entries
            if e.get("form") in form_types
            and e.get("filed")
            and e.get("val") is not None
        ]

        if not filtered:
            continue

        # Sort by filed date descending, take most recent
        filtered.sort(key=lambda e: e["filed"], reverse=True)
        best = filtered[0]
        return float(best["val"]), best["filed"], best["form"]

    return None


def _get_quarterly_operating_cash_flows(
    facts: dict,
    n_quarters: int = 4,
) -> list[float]:
    """
    Return the last n_quarters of operating cash flow values.
    Used to compute average monthly burn rate.

    XBRL reports cumulative YTD figures for 10-Q — we need to
    de-cumulate to get per-quarter values.
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})

    for concept in _OPERATING_CASH_FLOW_CONCEPTS:
        concept_data = us_gaap.get(concept)
        if not concept_data:
            continue

        entries = concept_data.get("units", {}).get("USD", [])

        # Filter to 10-Q filings with fiscal period info
        quarterly = [
            e for e in entries
            if e.get("form") == "10-Q"
            and e.get("filed")
            and e.get("val") is not None
            and e.get("fp")  # fiscal period e.g. Q1, Q2, Q3
        ]

        if not quarterly:
            continue

        # Sort by end date descending
        quarterly.sort(key=lambda e: e.get("end", ""), reverse=True)

        # Group by fiscal year to de-cumulate YTD figures
        # Q1 = Q1 value, Q2 = Q2 - Q1, Q3 = Q3 - Q2, Q4 from 10-K
        by_year: dict[str, dict[str, float]] = {}
        for e in quarterly:
            fy = e.get("fy", "")
            fp = e.get("fp", "")
            if fy and fp:
                if fy not in by_year:
                    by_year[fy] = {}
                by_year[fy][fp] = float(e["val"])

        quarterly_values: list[float] = []
        for fy in sorted(by_year.keys(), reverse=True):
            periods = by_year[fy]
            q1 = periods.get("Q1")
            q2 = periods.get("Q2")
            q3 = periods.get("Q3")
            if q3 and q2:
                quarterly_values.append(q3 - q2)
            if q2 and q1:
                quarterly_values.append(q2 - q1)
            if q1:
                quarterly_values.append(q1)
            if len(quarterly_values) >= n_quarters:
                break

        return quarterly_values[:n_quarters]

    return []


def fetch_fundamentals(cik: str) -> dict:
    """
    Fetch and structure key financial fundamentals for a company.

    Args:
        cik: zero-padded 10-digit CIK string

    Returns dict with:
        cik, entity_name,
        cash, cash_filed_date,
        burn_rate_monthly, burn_rate_source,
        cash_runway_months,
        shares_outstanding, shares_filed_date,
        data_quality: "full" | "partial" | "insufficient"
        missing_fields: list of what couldn't be found
    """
    facts = _get_company_facts(cik)
    entity_name = facts.get("entityName", "")
    missing: list[str] = []

    # --- Cash ---
    cash_result = _get_most_recent_value(facts, _CASH_CONCEPTS)
    if cash_result:
        cash, cash_filed, cash_form = cash_result
    else:
        cash, cash_filed, cash_form = None, None, None
        missing.append("cash")

    # --- Burn rate from operating cash flow ---
    quarterly_ocf = _get_quarterly_operating_cash_flows(facts)
    if quarterly_ocf:
        # Average quarterly operating cash flow (negative = burning cash)
        avg_quarterly = sum(quarterly_ocf) / len(quarterly_ocf)
        # Convert to monthly — negative means burning cash, we store as positive burn
        burn_rate_monthly = abs(avg_quarterly) / 3.0 if avg_quarterly < 0 else 0.0
        burn_rate_source = f"avg of {len(quarterly_ocf)} quarters of operating cash flow"
    else:
        # Fallback: try operating expenses / 3 from most recent quarter
        opex_result = _get_most_recent_value(facts, _OPERATING_EXPENSES_CONCEPTS)
        if opex_result:
            opex_quarterly, _, _ = opex_result
            burn_rate_monthly = opex_quarterly / 3.0
            burn_rate_source = "operating expenses (fallback)"
        else:
            burn_rate_monthly = None
            burn_rate_source = None
            missing.append("burn_rate")

    # --- Cash runway ---
    if cash is not None and burn_rate_monthly and burn_rate_monthly > 0:
        cash_runway_months = cash / burn_rate_monthly
    else:
        cash_runway_months = None
        if "burn_rate" not in missing:
            missing.append("cash_runway")

    # --- Shares outstanding ---
    shares_result = _get_most_recent_value(
        facts, _SHARES_CONCEPTS, unit="shares"
    )
    if shares_result:
        shares_outstanding, shares_filed, _ = shares_result
    else:
        shares_outstanding, shares_filed = None, None
        missing.append("shares_outstanding")

    # --- Data quality assessment ---
    critical_fields = {"cash", "burn_rate"}
    missing_critical = critical_fields & set(missing)
    if not missing_critical:
        data_quality = "full" if not missing else "partial"
    else:
        data_quality = "insufficient"

    return {
        "cik": cik,
        "entity_name": entity_name,
        "cash": cash,
        "cash_filed_date": cash_filed,
        "cash_form": cash_form,
        "burn_rate_monthly": burn_rate_monthly,
        "burn_rate_source": burn_rate_source,
        "cash_runway_months": round(cash_runway_months, 1) if cash_runway_months else None,
        "shares_outstanding": shares_outstanding,
        "shares_filed_date": shares_filed,
        "data_quality": data_quality,
        "missing_fields": missing,
    }
