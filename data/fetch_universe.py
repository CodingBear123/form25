"""
Fetch the biotech stock universe for screening.

Built from current XBI/IBB/ARKG/BBC holdings via yfinance — free, no API key.
The universe is the starting pool before any hard filters are applied.

SURVIVORSHIP WARNING
--------------------
An ETF holds only companies that are still listed, so this returns today's
survivors and nothing else. Seeding the price database from it is the origin
of the survivorship bias measured in the README: a company that went bankrupt
or was acquired never enters the sample, and the backtest cannot price what it
cannot see.

The point-in-time universe — who was listed on a given historical date — comes
from data/fetch_delistings.py instead, which reconstructs it from SEC EDGAR.
That reconstruction is what the survivorship correction is computed against.
This function is kept only to seed price history for currently-tradeable
names, which is all any free price source will serve.
"""

import yfinance as yf
import time
from typing import Optional

_BIOTECH_ETFS = ["XBI", "IBB", "ARKG", "BBC"]



def fetch_universe_from_etfs(
    etfs: list[str] | None = None,
) -> list[dict]:
    """Build biotech universe from ETF holdings."""
    if etfs is None:
        etfs = _BIOTECH_ETFS

    seen: set[str] = set()
    universe: list[dict] = []

    for etf in etfs:
        print(f"  Fetching {etf} holdings...")
        try:
            etf_ticker = yf.Ticker(etf)
            holdings = etf_ticker.funds_data.top_holdings if hasattr(etf_ticker, 'funds_data') else None

            if holdings is None or len(holdings) == 0:
                print(f"    {etf}: no holdings data available")
                continue

            for _, row in holdings.iterrows():
                symbol = str(row.name).upper().strip()
                if not symbol or symbol in seen:
                    continue
                seen.add(symbol)
                universe.append({
                    "ticker": symbol,
                    "company_name": str(row.get("Name", "")),
                    "market_cap": None,
                    "source_etf": etf,
                })
        except Exception as e:
            print(f"    {etf}: failed — {e}")
            continue

    print(f"  Raw universe: {len(universe)} tickers from ETF holdings")

    if not universe:
        print("  ETF holdings unavailable — using curated fallback list")
        universe = _curated_biotech_fallback()

    return universe


def enrich_universe(
    universe: list[dict],
    min_market_cap: float = 10_000_000,
    max_market_cap: float = 10_000_000_000,
    delay: float = 0.1,
) -> list[dict]:
    """Enrich universe with market cap data."""
    tickers = [u["ticker"] for u in universe]
    print(f"  Enriching {len(tickers)} tickers with market cap data...")

    enriched_map: dict[str, float] = {}

    for ticker in tickers:
        try:
            info = yf.Ticker(ticker).fast_info
            mc = getattr(info, "market_cap", None)
            if mc:
                enriched_map[ticker] = float(mc)
        except Exception:
            pass
        time.sleep(delay)

    filtered: list[dict] = []
    for u in universe:
        mc = enriched_map.get(u["ticker"])
        if mc is None:
            continue
        if mc < min_market_cap or mc > max_market_cap:
            continue
        u["market_cap"] = mc
        filtered.append(u)

    print(f"  After market cap filter: {len(filtered)} tickers")
    return filtered


def get_universe(
    min_market_cap: float = 50_000_000,
    max_market_cap: float = 2_000_000_000,
) -> list[dict]:
    """
    Main entry point — currently-listed biotech names, for price seeding.

    Returns list of dicts with ticker, company_name, market_cap, source_etf.
    See the survivorship warning in this module's docstring: these are
    survivors by construction.
    """
    print("Building universe from ETF holdings (survivors only)...")
    raw = fetch_universe_from_etfs()
    return enrich_universe(
        raw,
        min_market_cap=min_market_cap,
        max_market_cap=max_market_cap,
    )


def _curated_biotech_fallback() -> list[dict]:
    """Hardcoded fallback list when ETF data is unavailable."""
    tickers = [
        "MRNA", "BIIB", "ALNY", "INCY", "BMRN", "IONS",
        "AKBA", "ARQT", "ASMB", "AVXL", "BCAB", "BDTX",
        "BEAM", "BPMC", "CABA", "CORT", "CRSP", "DNLI",
        "EDIT", "EXAS", "FATE", "FOLD", "GERN", "HALO",
        "IMVT", "INSM", "IOVA", "ITOS", "KALA", "KMDA",
        "LNTH", "MDGL", "MGNX", "MIRM", "MNKD", "NKTR",
        "NVAX", "OCUL", "OMER", "PACB", "PCVX", "PRAX",
        "PRTA", "PTGX", "RCUS", "RETA", "RMTI", "RPRX",
        "SAGE", "SGEN", "SRPT", "TGTX", "TVTX", "VKTX",
        "VNDA", "VRTX", "XOMA", "ZYME",
    ]
    return [{"ticker": t, "company_name": "", "market_cap": None, "source_etf": "fallback"} for t in tickers]
