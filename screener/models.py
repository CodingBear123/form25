"""Pydantic models for the screening pipeline."""

from pydantic import BaseModel
from typing import Optional


class HardFilterParams(BaseModel):
    """
    Hard filters — a company must pass ALL of these to proceed.

    These thresholds are the whole screen. A soft scoring layer used to rank
    the survivors; it was removed after the 2019-2026 backtest measured its
    rank correlation with forward return at rho = 0.008. See the README.
    """
    min_market_cap: float = 50_000_000
    max_market_cap: float = 2_000_000_000
    min_cash_runway_months: float = 6.0
    min_cash_ratio: float = 0.20


class CompanySnapshot(BaseModel):
    """Point-in-time state of one company, assembled from SEC + market data."""
    # Identity
    ticker: str
    entity_name: str
    cik: str

    # Market data
    market_cap: Optional[float] = None
    current_price: Optional[float] = None
    price_52w_high: Optional[float] = None
    price_52w_low: Optional[float] = None
    price_vs_52w_high_pct: Optional[float] = None
    short_interest_pct: Optional[float] = None

    # Fundamentals from SEC
    cash: Optional[float] = None
    burn_rate_monthly: Optional[float] = None
    cash_runway_months: Optional[float] = None
    cash_ratio: Optional[float] = None
    shares_outstanding: Optional[float] = None
    dilution_events_3y: Optional[int] = None

    # Pipeline
    pipeline_stage: Optional[str] = None      # "phase1"|"phase2"|"phase3"|"approved"|"unknown"
    lead_indication: Optional[str] = None

    # Insider activity (last 90 days)
    insider_buys_90d: int = 0
    insider_sells_90d: int = 0
    insider_buy_value_90d: Optional[float] = None

    # Hard filter result
    passes_hard_filters: Optional[bool] = None
    hard_filter_failures: list[str] = []


class ScreenerResult(BaseModel):
    """Output of one screener pass."""
    portfolio: str                  # "value"
    run_date: str
    universe_size: int
    hard_filter_passed: int
    candidates: list[CompanySnapshot]
    excluded: list[dict] = []
