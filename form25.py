"""
Form25 — single entry point for all commands.

Usage:
    python form25.py status                         # DB health check, what's cached

    python form25.py setup                          # full data setup (run first time + weekly)
    python form25.py setup --skip-prices            # skip price sync
    python form25.py setup --skip-macro             # skip macro sync
    python form25.py setup --skip-ciks              # skip CIK fetch
    python form25.py setup --skip-timelines         # skip filing timeline cache
    python form25.py setup --force                  # re-fetch everything

    python form25.py backtest                       # build snapshots + run stats
    python form25.py backtest --stats-only          # re-run stats on existing snapshots
    python form25.py backtest --start 2019-06-14 --end 2026-09-18
    python form25.py backtest --save-report         # save JSON report to backtest/

    python form25.py cache                          # build analysis cache (run after backtest)
    python form25.py cache --force                  # rebuild even if up to date

    python form25.py analyze --regime               # performance by macro regime
    python form25.py analyze --price-levels         # optimal price vs 52w high threshold
    python form25.py analyze --all                  # run all analyses in sequence

Typical first-time workflow:
    1. python form25.py setup
    2. python form25.py backtest
    3. python form25.py cache
    4. python form25.py analyze --all

Weekly refresh:
    1. python form25.py setup            # top up prices + macro
    2. python form25.py backtest         # add new snapshots (skips existing)
    3. python form25.py cache --force    # rebuild cache with new data
"""

import sys
import os
import argparse

# Make sure imports work from anywhere
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.config import DEFAULT_START_DATE, DEFAULT_END_DATE  # noqa: E402

# The snapshots table keys on a portfolio name. There is one portfolio: the
# hard-filter value screen. A second "catalyst" name used to be selectable on
# the CLI while every code path ran this one regardless.
PORTFOLIO = "value"


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def cmd_status(_args) -> None:
    """Show DB health, coverage, and what's cached."""
    from data.fetch_flat_files import get_price_db_stats
    from data.fetch_macro import get_macro_stats, init_macro_tables
    from backtest.filing_timeline import get_timeline_cache_stats, init_timeline_table
    from utils.db import get_db_stats, init_db
    from utils.config import FORM25_DB_PATH, PRICES_DB_PATH

    init_db(FORM25_DB_PATH)
    init_macro_tables(FORM25_DB_PATH)
    init_timeline_table(FORM25_DB_PATH)

    SEP = "=" * 55

    print(f"\n{SEP}")
    print("FORM25 STATUS")
    print(SEP)

    # Price DB
    p = get_price_db_stats()
    print(f"\nPrice DB ({PRICES_DB_PATH.split(os.sep)[-1]}):")
    if p.get("exists"):
        print(f"  Tickers:  {p['tickers']}")
        print(f"  Rows:     {p['total_rows']:,}")
        print(f"  Coverage: {p['date_from']} -> {p['date_to']}")
    else:
        print("  NOT FOUND — run: python form25.py setup --skip-macro --skip-ciks --skip-timelines")

    # Macro DB
    m = get_macro_stats()
    print(f"\nMacro DB ({len(m['series'])} series, {m['total_observations']:,} observations):")
    if m["series"]:
        for s in m["series"]:
            synced = s["last_synced"][:10] if s["last_synced"] else "never"
            print(f"  {s['series_id']:20s} {s['earliest'] or '?':10s} -> "
                  f"{s['latest'] or '?':10s}  (synced {synced})")
    else:
        print("  EMPTY — run: python form25.py setup --skip-prices --skip-ciks --skip-timelines")

    # Filing timeline cache
    t = get_timeline_cache_stats()
    print(f"\nFiling timeline cache:")
    print(f"  Tickers: {t['total_tickers']} ({t['fresh']} fresh, {t['stale']} stale)")
    if t["stale"] > 0:
        print(f"  WARNING: {t['stale']} stale — run: python form25.py setup --skip-prices --skip-macro --skip-ciks")

    # Form25 DB
    s = get_db_stats(FORM25_DB_PATH)
    print(f"\nForm25 DB:")
    print(f"  Snapshots:           {s.get('snapshots', 0):,}")
    print(f"  Fundamentals cached: {s.get('fundamentals_cached', 0):,}")

    if s.get("snapshots", 0) == 0:
        print("\n  No snapshots yet — run: python form25.py backtest")
    elif s.get("snapshots", 0) < 100_000:
        print(f"\n  Backtest may be incomplete — run: python form25.py backtest")
    else:
        print(f"\n  Ready. Run analysis: python form25.py analyze --all")

    print()


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def cmd_setup(args) -> None:
    """Full data pipeline setup."""
    from scripts.setup import step_prices, step_macro, step_ciks, step_timelines

    print("=" * 55)
    print("Form25 setup")
    print("=" * 55)

    if not args.skip_prices:
        step_prices(force=args.force)
    if not args.skip_macro:
        step_macro(start_date=args.macro_start, force=args.force)
    if not args.skip_ciks:
        step_ciks(force=args.force)
    if not args.skip_timelines:
        step_timelines(force=args.force)

    print("\n" + "=" * 55)
    print("Setup complete. Next: python form25.py backtest")
    print("=" * 55)


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

def cmd_backtest(args) -> None:
    """Build snapshots and/or run statistical analysis."""
    start = args.start
    end   = args.end

    print("=" * 60)
    print("Form25 backtest")
    print("=" * 60)
    print(f"Period:       {start} -> {end}")
    print(f"Hold periods: {args.hold_days}d")
    print(f"Permutations: {args.permutations:,}")

    if not args.stats_only:
        print("\n" + "-" * 60)
        print("Building daily snapshots...")
        print("-" * 60)
        from backtest.daily_runner import run_daily_backtest
        result = run_daily_backtest(
            start_date=start, end_date=end,
            portfolio=PORTFOLIO,
            include_macro=True,
            hold_days=args.hold_days,
            skip_existing=True,
            save_snapshots=True,
        )
        print(f"\nSnapshot build complete:")
        print(f"  Trading days: {result['trading_days']}")
        print(f"  Tickers:      {result['tickers']}")
        print(f"  Built:        {result['snapshots_built']:,}")
        print(f"  Skipped:      {result['snapshots_skipped']:,}")
        print(f"  Failed:       {result['snapshots_failed']:,}")

    print("\n" + "-" * 60)
    print("Running statistical analysis...")
    print("-" * 60)
    from backtest.daily_runner import load_backtest_results
    from backtest.stats import run_full_analysis, print_report, save_report

    results = load_backtest_results(
        start_date=start, end_date=end,
        portfolio=PORTFOLIO,
        hold_days=args.hold_days,
    )
    if not results:
        print("No results found. Run without --stats-only first.")
        sys.exit(1)

    report = run_full_analysis(
        results, hold_days=args.hold_days,
        n_permutations=args.permutations,
        start_date=start, end_date=end,
        portfolio=PORTFOLIO,
    )
    print_report(report)

    if args.save_report:
        out = os.path.join("backtest", f"analysis_{start}_{end}.json")
        save_report(report, out)


# ---------------------------------------------------------------------------
# Analyze subcommands
# ---------------------------------------------------------------------------

def cmd_analyze(args) -> None:
    """Run one or all analysis modules."""
    run_all = args.all or not any([args.regime, args.price_levels])

    if args.regime or run_all:
        _analyze_regime(args)

    if args.price_levels or run_all:
        _analyze_price_levels(args)


def _analyze_regime(args) -> None:
    """Regime-filtered performance analysis."""
    from scripts.regime_analysis import main as regime_main
    print("\n" + "=" * 55)
    print("REGIME ANALYSIS")
    print("=" * 55)
    regime_main(start=args.start, end=args.end)


def _analyze_price_levels(_args) -> None:
    """Optimal price vs 52w high threshold analysis."""
    from scripts.price_level_analysis import main as price_levels_main
    print("\n" + "=" * 55)
    print("PRICE VS 52W HIGH LEVEL ANALYSIS")
    print("=" * 55)
    price_levels_main()


# ---------------------------------------------------------------------------
# Main router
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="form25",
        description="Form25 biotech screener — one entry point for everything",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  status      Show DB health and what's cached
  setup       Sync price data, macro data, CIKs, and filing timelines
  backtest    Build snapshots and run statistical analysis
  analyze     Run diagnostic analyses (regime, price levels)

Examples:
  python form25.py status
  python form25.py setup
  python form25.py backtest --stats-only
  python form25.py analyze --regime
  python form25.py analyze --all
        """
    )
    sub = parser.add_subparsers(dest="command")

    # status
    sub.add_parser("status", help="DB health check")

    # setup
    p_setup = sub.add_parser("setup", help="Sync all data sources")
    p_setup.add_argument("--skip-prices",    action="store_true")
    p_setup.add_argument("--skip-macro",     action="store_true")
    p_setup.add_argument("--skip-ciks",      action="store_true")
    p_setup.add_argument("--skip-timelines", action="store_true")
    p_setup.add_argument("--macro-start",    default="2018-01-01")
    p_setup.add_argument("--force",          action="store_true")

    # backtest
    p_bt = sub.add_parser("backtest", help="Build snapshots + run stats")
    p_bt.add_argument("--start",        default=DEFAULT_START_DATE)
    p_bt.add_argument("--end",          default=DEFAULT_END_DATE)
    p_bt.add_argument("--hold-days",    nargs="*", type=int, default=[30, 60, 90])
    p_bt.add_argument("--permutations", type=int, default=10000)
    p_bt.add_argument("--stats-only",   action="store_true")
    p_bt.add_argument("--save-report",  action="store_true")

    # monitor
    sub.add_parser("monitor", help="Refresh macro data and the dashboard export")

    # export
    p_exp = sub.add_parser("export", help="Export data.json for the dashboard")
    p_exp.add_argument("--start", default=DEFAULT_START_DATE)
    p_exp.add_argument("--end",   default=DEFAULT_END_DATE)

    # cache
    p_cache = sub.add_parser("cache", help="Build analysis cache (speeds up all analyses)")
    p_cache.add_argument("--force", action="store_true", help="Rebuild even if up to date")
    p_cache.add_argument("--start", default=DEFAULT_START_DATE)
    p_cache.add_argument("--end",   default=DEFAULT_END_DATE)

    # analyze
    p_an = sub.add_parser("analyze", help="Run diagnostic analyses")
    p_an.add_argument("--regime",       action="store_true", help="Regime performance breakdown")
    p_an.add_argument("--price-levels", action="store_true", help="Price vs 52w high analysis")
    p_an.add_argument("--all",          action="store_true", help="Run all analyses")
    p_an.add_argument("--start",        default=DEFAULT_START_DATE)
    p_an.add_argument("--end",          default=DEFAULT_END_DATE)

    args = parser.parse_args()

    if args.command == "status":
        cmd_status(args)
    elif args.command == "monitor":
        from scripts.monitor import run as monitor_run
        monitor_run()
    elif args.command == "export":
        from scripts.export_dashboard import main as export_main
        export_main(start=args.start, end=args.end)
    elif args.command == "cache":
        from scripts.analysis_cache import build_cache
        build_cache(force=args.force, start_date=args.start, end_date=args.end)
    elif args.command == "setup":
        cmd_setup(args)
    elif args.command == "backtest":
        cmd_backtest(args)
    elif args.command == "analyze":
        cmd_analyze(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
