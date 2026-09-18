"""
Form25 backtest script.

Runs the full daily backtest over a date range, then prints the
statistical analysis report (permutation test, p-value, macro regime
breakdown, Sharpe ratio).

Usage (run from form25_root/form25/):
    python scripts/backtest.py --start 2019-06-14 --end 2026-09-18

    # Skip the build step and just re-run stats on existing snapshots:
    python scripts/backtest.py --start 2019-06-14 --end 2026-09-18 --stats-only

    # Specific tickers only:
    python scripts/backtest.py --start 2019-06-14 --end 2026-09-18 --tickers MRNA BEAM CRSP

    # More permutations for tighter p-value estimates (slower):
    python scripts/backtest.py --start 2019-06-14 --end 2026-09-18 --permutations 50000

Prerequisite: run scripts/setup.py first.
"""

import sys
import os
import argparse
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.config  # noqa: F401 — importing this loads the .env file

# Only one portfolio exists: the hard-filter value screen.
PORTFOLIO = "value"


def main() -> None:
    parser = argparse.ArgumentParser(description="Form25 backtest runner")
    parser.add_argument("--start",        required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end",          required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--tickers",      nargs="*", help="Specific tickers (default: full universe)")
    parser.add_argument("--hold-days",    nargs="*", type=int, default=[30, 60, 90],
                        help="Forward return windows in days (default: 30 60 90)")
    parser.add_argument("--permutations", type=int, default=10000,
                        help="Permutation test iterations (default: 10000)")
    parser.add_argument("--no-macro",     action="store_true",
                        help="Skip attaching macro context to snapshots")
    parser.add_argument("--stats-only",   action="store_true",
                        help="Skip building snapshots, just run stats on existing data")
    parser.add_argument("--save-report",  action="store_true",
                        help="Save JSON report to backtest/analysis_<start>_<end>.json")
    args = parser.parse_args()

    start = args.start
    end   = args.end

    print("=" * 60)
    print("Form25 backtest")
    print("=" * 60)
    print(f"Period:       {start} -> {end}")
    print(f"Hold periods: {args.hold_days}d")
    print(f"Permutations: {args.permutations:,}")
    if args.tickers:
        print(f"Tickers:      {args.tickers}")

    # ------------------------------------------------------------------
    # Step 1: Build daily snapshots
    # ------------------------------------------------------------------
    if not args.stats_only:
        print("\n" + "-" * 60)
        print("Building daily snapshots...")
        print("-" * 60)

        from backtest.daily_runner import run_daily_backtest

        result = run_daily_backtest(
            start_date=start,
            end_date=end,
            tickers=args.tickers,
            portfolio=PORTFOLIO,
            include_macro=not args.no_macro,
            hold_days=args.hold_days,
            skip_existing=True,
            save_snapshots=True,
        )

        print(f"\nSnapshot build complete:")
        print(f"  Trading days: {result['trading_days']}")
        print(f"  Tickers:      {result['tickers']}")
        print(f"  Built:        {result['snapshots_built']:,}")
        print(f"  Skipped:      {result['snapshots_skipped']:,} (already cached)")
        print(f"  Failed:       {result['snapshots_failed']:,} (no price/fundamental data)")

    # ------------------------------------------------------------------
    # Step 2: Statistical analysis
    # ------------------------------------------------------------------
    print("\n" + "-" * 60)
    print("Running statistical analysis...")
    print("-" * 60)

    from backtest.daily_runner import load_backtest_results, compute_benchmark_returns
    from backtest.stats import run_full_analysis, print_report, save_report

    results = load_backtest_results(
        start_date=start,
        end_date=end,
        portfolio=PORTFOLIO,
        hold_days=args.hold_days,
    )

    if not results:
        print("\nNo backtest results found in the snapshots table.")
        print("Make sure you ran setup.py first and the date range matches.")
        sys.exit(1)

    # Benchmark alpha. Sampled on exactly the days the screener traded —
    # beating random selection inside a biotech universe is a much weaker
    # claim than beating XBI, which is the alternative you could just buy.
    print("  Computing benchmark returns (SPY, XBI)...", end=" ", flush=True)
    benchmark_dates = sorted({r["snapshot_date"] for r in results})
    benchmark_returns = compute_benchmark_returns(
        benchmark_dates, hold_days=args.hold_days
    )
    print(f"{len(benchmark_returns)} benchmarks over {len(benchmark_dates)} dates")

    report = run_full_analysis(
        results,
        hold_days=args.hold_days,
        n_permutations=args.permutations,
        start_date=start,
        end_date=end,
        portfolio=PORTFOLIO,
        benchmark_returns=benchmark_returns,
    )

    print_report(report)

    if args.save_report:
        out_dir = os.path.join(os.path.dirname(__file__), '..', 'backtest')
        out_path = os.path.join(out_dir, f"analysis_{start}_{end}.json")
        save_report(report, out_path)

    # Exit code signals significance for CI/scripting use
    sys.exit(0 if report.overall_significant else 1)


if __name__ == "__main__":
    main()
