"""
Bound the survivorship bias without paying for delisted price data.

The problem
-----------
Correcting the backtest properly needs price history for companies that no
longer trade, and no free source provides it (yfinance returns nothing for 5
of 6 test cases; Stooq gates its CSV endpoint). The paid feeds cost $20-50/mo.

The way around it
-----------------
You do not need the dead companies' price paths to answer the question that
matters, which is "is the edge real, or is it an artefact of only ever looking
at survivors?"

SEC EDGAR tells us — for free — exactly which companies existed, which died,
and when. That gives the *attrition rate* of the universe. If we assume the
strategy had no special ability to avoid the companies that later died, then
some fraction w of its positions were in names that went on to delist, and the
reported return overstates the truth by roughly:

    corrected = observed - w * (observed - delisting_return)

Running that across a range of delisting-return assumptions brackets the true
performance. The bracket is wide, but it is honest, and it usually settles the
question outright:

  - If the edge survives the pessimistic bound, survivorship is not your
    problem and you can stop worrying about it.
  - If the edge vanishes even under the optimistic bound, the strategy is an
    artefact and no amount of paid data will rescue it.
  - Only if the answer sits between the bounds do you actually need to spend
    money on data.

This is a bound, not a measurement. It cannot replace real delisted prices —
it tells you whether buying them is worth it.

Usage:
    python scripts/survivorship_bounds.py --observed-return 18.4
    python scripts/survivorship_bounds.py --observed-return 18.4 --attrition 0.133
"""

import argparse
import os
import sqlite3
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.config import (  # noqa: E402
    FORM25_DB_PATH, DEFAULT_START_DATE, DEFAULT_END_DATE,
)

# Delisting return conventions to bracket across.
#   -100%  every dead name was a total loss (pessimistic floor)
#    -30%  CRSP unknown-reason convention (the realistic middle)
#      0%  every dead name was acquired at the prevailing price (optimistic)
_SCENARIOS = [
    ("pessimistic (all bankruptcies, -100%)", -100.0),
    ("CRSP convention (-30%)", -30.0),
    ("optimistic (all acquisitions, 0%)", 0.0),
]


def measure_attrition(
    start_date: str,
    end_date: str,
    form25_db: str = FORM25_DB_PATH,
    sector: str = "biotech",
) -> Optional[dict]:
    """
    Fraction of the universe that delisted inside the window, per SEC data.

    Returns None if the universe table has not been populated by
    data/fetch_delistings.py yet.
    """
    if not os.path.exists(form25_db):
        return None

    conn = sqlite3.connect(form25_db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(universe)")}
        if "delisted_date" not in cols:
            return None

        total = conn.execute(
            "SELECT COUNT(*) FROM universe WHERE sector = ?", (sector,)
        ).fetchone()[0]
        if not total:
            return None

        died = conn.execute(
            "SELECT COUNT(*) FROM universe WHERE sector = ? "
            "AND delisted_date IS NOT NULL "
            "AND delisted_date BETWEEN ? AND ?",
            (sector, start_date, end_date),
        ).fetchone()[0]

        by_reason = dict(conn.execute(
            "SELECT COALESCE(delisting_reason, 'unspecified'), COUNT(*) FROM universe "
            "WHERE sector = ? AND delisted_date BETWEEN ? AND ? "
            "GROUP BY 1", (sector, start_date, end_date),
        ).fetchall())

        return {
            "universe_size": total,
            "delisted_in_window": died,
            "attrition_rate": died / total,
            "by_reason": by_reason,
        }
    finally:
        conn.close()


def blended_delisting_return(
    mix: dict[str, int],
    acquisition_return: float,
    bankruptcy_return: float = -100.0,
    unknown_return: float = -30.0,
) -> Optional[float]:
    """
    Weight the delisting return by the observed mix of delisting reasons.

    A flat -30% assumes every delisting is a failure. In this universe it
    isn't: acquisitions outnumber bankruptcies roughly 7:1, and being acquired
    is usually a *positive* outcome for a long-only holder. Applying the
    bankruptcy convention to an acquisition-dominated cohort overstates the
    correction badly.
    """
    total = sum(mix.values())
    if not total:
        return None

    per_reason = {
        "bankruptcy": bankruptcy_return,
        "acquisition_or_private": acquisition_return,
        "unknown": unknown_return,
        "unspecified": unknown_return,
    }
    return sum(
        per_reason.get(reason, unknown_return) * n for reason, n in mix.items()
    ) / total


def bound_return(observed_return: float, attrition: float) -> list[dict]:
    """
    Corrected return under each delisting-return scenario.

    `attrition` is the fraction of positions assumed to have been held in
    names that later delisted.
    """
    results = []
    for label, delisting_return in _SCENARIOS:
        corrected = (
            observed_return * (1 - attrition) + delisting_return * attrition
        )
        results.append({
            "scenario": label,
            "delisting_return": delisting_return,
            "corrected_return": corrected,
            "give_up": observed_return - corrected,
        })
    return results


def print_report(
    observed: float,
    attrition: float,
    attrition_source: str,
    stats: Optional[dict] = None,
) -> None:
    print("=" * 66)
    print("Survivorship bias bounds")
    print("=" * 66)
    print(f"Observed return (survivors only): {observed:>8.2f}%")
    print(f"Assumed attrition:                {attrition:>8.1%}  ({attrition_source})")

    if stats:
        print(f"\nUniverse per SEC EDGAR: {stats['universe_size']} names, "
              f"{stats['delisted_in_window']} delisted in window")
        if stats["by_reason"]:
            for reason, n in sorted(stats["by_reason"].items(), key=lambda x: -x[1]):
                print(f"    {reason:26s} {n:>4d}")

    print("\n" + "-" * 66)
    print(f"  {'SCENARIO':40s} {'CORRECTED':>10s} {'GIVE-UP':>10s}")
    print("-" * 66)
    for r in bound_return(observed, attrition):
        print(f"  {r['scenario']:40s} {r['corrected_return']:>9.2f}% "
              f"{r['give_up']:>9.2f}pp")
    print("-" * 66)

    bounds = bound_return(observed, attrition)
    worst = bounds[0]["corrected_return"]
    best = bounds[-1]["corrected_return"]

    print("\nHow to read this:")
    if worst > 0:
        print(f"  The edge survives even the pessimistic bound ({worst:.2f}%).")
        print("  Survivorship is not what is driving your results. Paid delisted")
        print("  price data would refine the number, not change the conclusion.")
    elif best < 0:
        print(f"  The edge is gone even under the optimistic bound ({best:.2f}%).")
        print("  The result is an artefact of looking only at survivors. Buying")
        print("  delisted data will confirm this, not rescue it.")
    else:
        print(f"  The answer straddles zero ({worst:.2f}% to {best:.2f}%).")
        print("  This is the one case where the bound cannot settle it and real")
        print("  delisted price data is worth paying for.")

    print("\nCaveat: this assumes the strategy was no better than chance at")
    print("avoiding companies that later died. If its fundamental filters")
    print("(cash runway, cash ratio) genuinely screened out failing companies,")
    print("the true attrition among its positions is lower and these bounds")
    print("are too harsh.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bound survivorship bias without paid data")
    parser.add_argument("--observed-return", type=float, required=True,
                        help="Mean return %% reported by the current backtest")
    parser.add_argument("--attrition", type=float,
                        help="Override attrition rate as a fraction (e.g. 0.133)")
    parser.add_argument("--start", default=DEFAULT_START_DATE)
    parser.add_argument("--end", default=DEFAULT_END_DATE)
    args = parser.parse_args()

    stats = measure_attrition(args.start, args.end)

    if args.attrition is not None:
        attrition = args.attrition
        source = "supplied on the command line"
    elif stats and stats["delisted_in_window"]:
        attrition = stats["attrition_rate"]
        source = "measured from SEC EDGAR"
    else:
        print("Universe table has no delisting data yet.")
        print("Run: python data/fetch_delistings.py")
        print("Or pass --attrition to supply a rate directly.\n")
        sys.exit(1)

    print_report(args.observed_return, attrition, source, stats)


if __name__ == "__main__":
    main()
