"""
Hard filter logic — the entire screen.

A company passes only if market cap, cash runway, and cash ratio all sit
inside their configured bounds. There is no ranking among the survivors:
the soft score that used to provide one measured rho = 0.008 against forward
return over 2019-2026 and was removed rather than left in to look busy.
"""

from screener.models import CompanySnapshot, HardFilterParams


def apply_hard_filters(
    company: CompanySnapshot,
    params: HardFilterParams | None = None,
) -> CompanySnapshot:
    """
    Run the hard filters against a CompanySnapshot.
    Sets passes_hard_filters and hard_filter_failures.
    """
    if params is None:
        params = HardFilterParams()

    failures: list[str] = []

    # Market cap
    if company.market_cap is None:
        failures.append("market_cap: missing data")
    elif company.market_cap < params.min_market_cap:
        failures.append(
            f"market_cap: ${company.market_cap/1e6:.1f}m below minimum ${params.min_market_cap/1e6:.0f}m"
        )
    elif company.market_cap > params.max_market_cap:
        failures.append(
            f"market_cap: ${company.market_cap/1e6:.0f}m above maximum ${params.max_market_cap/1e6:.0f}m"
        )

    # Cash runway
    if company.cash_runway_months is None:
        failures.append("cash_runway: missing data")
    elif company.cash_runway_months < params.min_cash_runway_months:
        failures.append(
            f"cash_runway: {company.cash_runway_months:.1f} months below minimum {params.min_cash_runway_months:.0f} months"
        )

    # Cash ratio
    if company.cash_ratio is None:
        failures.append("cash_ratio: missing data")
    elif company.cash_ratio < params.min_cash_ratio:
        failures.append(
            f"cash_ratio: {company.cash_ratio:.2f} below minimum {params.min_cash_ratio:.2f}"
        )

    company.passes_hard_filters = len(failures) == 0
    company.hard_filter_failures = failures
    return company


def run_value_filters(
    companies: list[CompanySnapshot],
    params: HardFilterParams | None = None,
    top_n: int = 20,
) -> tuple[list[CompanySnapshot], list[dict]]:
    """
    Apply the hard filters to a list of companies.
    Returns (passed, excluded).
    """
    passed: list[CompanySnapshot] = []
    excluded: list[dict] = []

    for company in companies:
        company = apply_hard_filters(company, params)
        if company.passes_hard_filters:
            passed.append(company)
        else:
            excluded.append({
                "ticker": company.ticker,
                "entity_name": company.entity_name,
                "reasons": company.hard_filter_failures,
            })

    return passed[:top_n], excluded
