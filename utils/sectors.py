"""
Sector configuration registry.

Each sector defines:
    - data_sources: which fetchers to use
    - hard_filter_params: default screening thresholds
    - extra_fields: sector-specific CompanySnapshot fields
    - cache_ttl: how long to cache each data source (seconds)
    - universe_source: how to fetch the ticker universe

Adding a new sector = adding a new entry here. No other code changes needed.
"""

from dataclasses import dataclass, field
from typing import Optional
from screener.models import HardFilterParams


@dataclass
class CacheTTL:
    """Cache TTL in seconds for each data source."""
    fundamentals: int = 7 * 24 * 3600    # 7 days — quarterly filings
    price: int = 24 * 3600               # 24 hours — daily price
    insider_trades: int = 24 * 3600      # 24 hours — Form 4 filed daily
    universe: int = 7 * 24 * 3600        # 7 days — universe refresh


@dataclass
class SectorConfig:
    """Full configuration for one sector."""
    name: str
    display_name: str
    data_sources: list[str]              # ["sec", "yfinance", "fred", "eia"]
    hard_filter_params: HardFilterParams = field(default_factory=HardFilterParams)
    extra_fields: list[str] = field(default_factory=list)
    cache_ttl: CacheTTL = field(default_factory=CacheTTL)
    universe_search_terms: list[str] = field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# Sector registry
# ---------------------------------------------------------------------------

SECTORS: dict[str, SectorConfig] = {

    "biotech": SectorConfig(
        name="biotech",
        display_name="Biotech / Biopharma",
        data_sources=["sec", "yfinance"],
        hard_filter_params=HardFilterParams(
            min_market_cap=50_000_000,
            max_market_cap=2_000_000_000,
            min_cash_runway_months=6.0,
            min_cash_ratio=0.20,
        ),
        extra_fields=[
            "pipeline_stage",
            "lead_indication",
            "cash_runway_months",
            "cash_ratio",
        ],
        cache_ttl=CacheTTL(),
        universe_search_terms=[
            "therapeutics", "biosciences", "biopharma", "biotech",
            "pharmaceuticals", "genomics", "oncology", "biologics",
            "biotherapeutics", "molecular", "genetic", "clinical",
        ],
        notes="Value/quality focus — beaten down fundamentally strong biotechs. "
              "Cash runway and cash ratio are primary filters.",
    ),

    "energy": SectorConfig(
        name="energy",
        display_name="Energy / Oil & Gas",
        data_sources=["sec", "yfinance", "eia"],   # EIA = US Energy Information Admin
        hard_filter_params=HardFilterParams(
            min_market_cap=100_000_000,
            max_market_cap=5_000_000_000,
            min_cash_runway_months=6.0,
            min_cash_ratio=0.05,                   # energy cos have more asset backing
        ),
        extra_fields=[
            "production_cost_per_barrel",
            "proven_reserves",
            "debt_to_equity",
        ],
        cache_ttl=CacheTTL(fundamentals=7 * 24 * 3600),
        universe_search_terms=[
            "energy", "petroleum", "oil", "gas", "drilling",
            "exploration", "refining", "midstream", "upstream",
        ],
        notes="Value/quality energy — beaten down E&P and midstream.",
    ),

    "generalist": SectorConfig(
        name="generalist",
        display_name="Generalist Value",
        data_sources=["sec", "yfinance"],
        hard_filter_params=HardFilterParams(
            min_market_cap=50_000_000,
            max_market_cap=2_000_000_000,
            min_cash_runway_months=0.0,    # non-biotech companies don't burn cash the same way
            min_cash_ratio=0.05,
        ),
        extra_fields=[],
        cache_ttl=CacheTTL(),
        universe_search_terms=[],          # too broad for name search — use screener instead
        notes="General undervalued stocks. Cash runway filter relaxed — "
              "applies to profitable businesses, not pre-revenue cos.",
    ),

}


def get_sector(name: str) -> SectorConfig:
    """Get sector config by name. Raises KeyError if not found."""
    if name not in SECTORS:
        raise KeyError(f"Unknown sector '{name}'. Available: {list(SECTORS.keys())}")
    return SECTORS[name]


def list_sectors() -> list[str]:
    """Return list of available sector names."""
    return list(SECTORS.keys())


# Active sectors — only these are included in automated scans
ACTIVE_SECTORS = ["biotech"]
