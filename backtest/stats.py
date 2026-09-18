"""
Statistical analysis engine for Form25 backtest results.

The central question: is the screener's performance distinguishable from
random noise, or does it have genuine predictive power?

Approach:
    Permutation / bootstrap test — non-parametric, no distribution assumptions.
    Critical for biotech where returns are heavily right-skewed.

    H0: The screener selects stocks no better than random.
    H1: The screener generates statistically significant alpha.

    Procedure:
        1. Observed statistic: mean return of screener selections across all dates.
        2. For N=10,000 permutations: on each date, randomly draw the same
           number of tickers the screener selected, compute their mean return.
           This builds the null distribution under H0.
        3. P-value = fraction of permutation samples >= observed statistic.
        4. 95% CI = [2.5th, 97.5th] percentile of the null distribution.
        5. Effect size = (observed - null_mean) / null_std

    Additional analyses:
        - Macro-regime conditioning: performance by yield curve / rate regime
        - Sharpe ratio per hold period
        - Max drawdown

Public interface:
    run_full_analysis(results, hold_days)  ->  AnalysisReport
    print_report(report)                   ->  formatted stdout output
    save_report(report, path)              ->  JSON file
"""

import os
import sys
import json
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
from numpy.random import default_rng

from utils.config import DEFAULT_START_DATE, DEFAULT_END_DATE


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PermutationTestResult:
    hold_days:              int
    observed_mean:          float   # mean return of screener selections
    null_mean:              float   # mean of the permutation null distribution
    null_std:               float   # std of the null distribution
    effect_size:            float   # (observed - null_mean) / null_std
    p_value:                float   # fraction of permutations >= observed (one-tailed)
    ci_lower_95:            float   # 2.5th percentile of null distribution
    ci_upper_95:            float   # 97.5th percentile of null distribution
    n_permutations:         int
    n_observations:         int     # total (date, ticker) pairs in screener
    n_dates:                int     # unique dates with screener selections
    avg_selections_per_day: float
    significant:            bool    # p_value < 0.05
    interpretation:         str


@dataclass
class MacroRegimeStats:
    regime:       str
    n:            int
    mean_return:  float
    win_rate:     float
    vs_all_mean:  float   # difference from overall screener mean


@dataclass
class AnalysisReport:
    # Metadata
    start_date:     str
    end_date:       str
    n_tickers:      int
    n_trading_days: int
    portfolio:      str

    # Core permutation tests (one per hold period)
    permutation_tests: list[PermutationTestResult] = field(default_factory=list)

    # Benchmark comparison
    benchmark_returns:   dict = field(default_factory=dict)
    screener_vs_spy:     dict = field(default_factory=dict)
    screener_vs_xbi:     dict = field(default_factory=dict)

    # Macro regime conditioning
    macro_regime_stats: list[MacroRegimeStats] = field(default_factory=list)

    # Portfolio-level stats
    sharpe_ratio:          dict = field(default_factory=dict)   # {"30d": x, ...}
    max_drawdown:          dict = field(default_factory=dict)
    win_rates:             dict = field(default_factory=dict)
    avg_returns:           dict = field(default_factory=dict)
    median_returns:        dict = field(default_factory=dict)
    failed_filter_returns: dict = field(default_factory=dict)

    # Verdict
    overall_significant: bool = False
    summary:             str  = ""


# ---------------------------------------------------------------------------
# Permutation test
# ---------------------------------------------------------------------------

def _permutation_test(
    dates: list[str],
    passed_by_date: dict[str, list[float]],
    all_by_date:    dict[str, list[float]],
    hold_days:      int,
    n_permutations: int = 10_000,
    seed:           int = 42,
) -> PermutationTestResult:
    """
    Run permutation test for one hold period.

    On each permutation: for every date d, sample the same number of
    tickers the screener picked, from the full universe on that date.
    Pool samples across dates → compute mean → builds null distribution.
    """
    rng = default_rng(seed)

    # Pool all screener returns
    all_passed: list[float] = []
    for d in dates:
        all_passed.extend(passed_by_date.get(d, []))

    if not all_passed:
        return PermutationTestResult(
            hold_days=hold_days, observed_mean=0.0, null_mean=0.0, null_std=0.0,
            effect_size=0.0, p_value=1.0, ci_lower_95=0.0, ci_upper_95=0.0,
            n_permutations=n_permutations, n_observations=0, n_dates=0,
            avg_selections_per_day=0.0, significant=False, interpretation="No data",
        )

    obs_arr = np.array(all_passed, dtype=np.float64)
    observed_mean      = float(np.mean(obs_arr))
    n_observations     = len(all_passed)
    n_dates_with_data  = sum(1 for d in dates if passed_by_date.get(d))
    avg_sel            = n_observations / max(n_dates_with_data, 1)

    # Pre-convert universe lists to numpy arrays for speed
    universe_arrays: dict[str, np.ndarray] = {
        d: np.array(v, dtype=np.float64)
        for d, v in all_by_date.items()
        if v
    }

    # Null distribution via vectorised permutation where possible
    null_means = np.empty(n_permutations, dtype=np.float64)

    for i in range(n_permutations):
        sample_parts: list[np.ndarray] = []
        for d in dates:
            n_sel    = len(passed_by_date.get(d, []))
            universe = universe_arrays.get(d)
            if n_sel == 0 or universe is None or len(universe) == 0:
                continue
            if n_sel <= len(universe):
                idx = rng.choice(len(universe), size=n_sel, replace=False)
            else:
                idx = rng.choice(len(universe), size=n_sel, replace=True)
            sample_parts.append(universe[idx])

        if sample_parts:
            null_means[i] = np.mean(np.concatenate(sample_parts))
        else:
            null_means[i] = np.nan

    null_means = null_means[~np.isnan(null_means)]

    if len(null_means) == 0:
        return PermutationTestResult(
            hold_days=hold_days, observed_mean=observed_mean, null_mean=0.0,
            null_std=0.0, effect_size=0.0, p_value=1.0,
            ci_lower_95=0.0, ci_upper_95=0.0,
            n_permutations=n_permutations, n_observations=n_observations,
            n_dates=n_dates_with_data, avg_selections_per_day=avg_sel,
            significant=False, interpretation="Insufficient null distribution",
        )

    null_mean  = float(np.mean(null_means))
    null_std   = float(np.std(null_means, ddof=1))
    p_value    = float(np.mean(null_means >= observed_mean))
    ci_lower   = float(np.percentile(null_means, 2.5))
    ci_upper   = float(np.percentile(null_means, 97.5))
    effect_sz  = (observed_mean - null_mean) / null_std if null_std > 0 else 0.0
    significant = p_value < 0.05

    if p_value < 0.01:
        strength = "highly significant"
    elif p_value < 0.05:
        strength = "statistically significant"
    elif p_value < 0.10:
        strength = "marginally significant (p < 0.10)"
    else:
        strength = "not statistically significant"

    direction = "outperforms" if observed_mean > null_mean else "underperforms"
    interpretation = (
        f"Screener {direction} random selection at {hold_days}d hold: "
        f"mean={observed_mean:+.2f}% vs null={null_mean:+.2f}% "
        f"(effect={effect_sz:+.2f}σ, p={p_value:.4f}, {strength})"
    )

    return PermutationTestResult(
        hold_days=hold_days,
        observed_mean=round(observed_mean, 3),
        null_mean=round(null_mean, 3),
        null_std=round(null_std, 3),
        effect_size=round(effect_sz, 3),
        p_value=round(p_value, 4),
        ci_lower_95=round(ci_lower, 3),
        ci_upper_95=round(ci_upper, 3),
        n_permutations=n_permutations,
        n_observations=n_observations,
        n_dates=n_dates_with_data,
        avg_selections_per_day=round(avg_sel, 1),
        significant=significant,
        interpretation=interpretation,
    )


# ---------------------------------------------------------------------------
# Macro regime conditioning
# ---------------------------------------------------------------------------

def _macro_regime_analysis(
    results:   list[dict],
    hold_days: int,
) -> list[MacroRegimeStats]:
    return_key = f"return_{hold_days}d_pct"

    passed = [
        r for r in results
        if r.get("passed_filters")
        and r.get(return_key) is not None
        and r.get("macro_regime")
        and r.get("yield_curve_shape")
    ]
    if not passed:
        return []

    overall_mean = float(np.mean([r[return_key] for r in passed]))

    regime_groups: dict[str, list[float]] = {}
    for r in passed:
        label = f"{r.get('macro_regime', 'unknown')} / {r.get('yield_curve_shape', 'unknown')} curve"
        regime_groups.setdefault(label, []).append(r[return_key])
        env = f"risk: {r.get('risk_environment', 'unknown')}"
        regime_groups.setdefault(env, []).append(r[return_key])

    out: list[MacroRegimeStats] = []
    for regime, vals in regime_groups.items():
        if len(vals) < 10:
            continue
        arr = np.array(vals, dtype=np.float64)
        mean_ret = float(np.mean(arr))
        out.append(MacroRegimeStats(
            regime=regime,
            n=len(vals),
            mean_return=round(mean_ret, 2),
            win_rate=round(float(np.mean(arr > 0)), 3),
            vs_all_mean=round(mean_ret - overall_mean, 2),
        ))

    out.sort(key=lambda x: -x.mean_return)
    return out


# ---------------------------------------------------------------------------
# Sharpe ratio and max drawdown
# ---------------------------------------------------------------------------

def _compute_sharpe(
    returns:             np.ndarray,
    periods_per_year:    float = 252.0,
    risk_free_annual_pct: float = 4.5,
) -> float:
    if len(returns) < 4:
        return 0.0
    rf = risk_free_annual_pct / periods_per_year
    excess = returns - rf
    std = float(np.std(excess, ddof=1))
    if std == 0:
        return 0.0
    return round(float(np.mean(excess)) / std * np.sqrt(periods_per_year), 3)


def _compute_max_drawdown(returns_pct: np.ndarray) -> float:
    """
    Max drawdown of a buy-and-hold portfolio of the screener selections.
    Returns the worst peak-to-trough decline as a negative percentage.
    """
    if len(returns_pct) < 2:
        return 0.0
    # Cumulative wealth index
    wealth = np.cumprod(1 + returns_pct / 100)
    peak   = np.maximum.accumulate(wealth)
    dd     = (wealth - peak) / peak
    return round(float(np.min(dd)) * 100, 2)   # negative number


# ---------------------------------------------------------------------------
# Full analysis
# ---------------------------------------------------------------------------

def run_full_analysis(
    results:           list[dict],
    hold_days:         list[int]       = [30, 60, 90],
    n_permutations:    int             = 10_000,
    start_date:        str             = "",
    end_date:          str             = "",
    portfolio:         str             = "value",
    benchmark_returns: Optional[dict]  = None,
) -> AnalysisReport:
    """
    Run the complete statistical analysis on backtest results.

    Args:
        results:           from daily_runner.load_backtest_results()
        hold_days:         forward return windows to analyse
        n_permutations:    permutation iterations (10k ≈ 5-10s with numpy)
        start_date:        report metadata
        end_date:          report metadata
        portfolio:         portfolio name recorded in the report
        benchmark_returns: optional {ticker: {Nd: pct}} for alpha calc
    """
    if not results:
        return AnalysisReport(
            start_date=start_date, end_date=end_date,
            n_tickers=0, n_trading_days=0, portfolio=portfolio,
            summary="No results to analyse",
        )

    print(f"Running statistical analysis on {len(results):,} observations...")

    unique_tickers = len({r["ticker"] for r in results})
    unique_dates   = len({r["snapshot_date"] for r in results})
    passed_results = [r for r in results if r.get("passed_filters")]
    failed_results = [r for r in results if not r.get("passed_filters")]

    print(f"  Total:         {len(results):,}")
    print(f"  Passed:        {len(passed_results):,} ({len(passed_results)/len(results)*100:.1f}%)")
    print(f"  Tickers:       {unique_tickers}")
    print(f"  Trading days:  {unique_dates}")

    report = AnalysisReport(
        start_date=start_date, end_date=end_date,
        n_tickers=unique_tickers, n_trading_days=unique_dates,
        portfolio=portfolio,
    )

    all_dates  = sorted({r["snapshot_date"] for r in results})
    primary_hd = hold_days[0]

    for hd in hold_days:
        key = f"return_{hd}d_pct"
        print(f"\n  [{hd}d] Permutation test ({n_permutations:,} iterations)...", end=" ", flush=True)

        passed_by_date: dict[str, list[float]] = {}
        all_by_date:    dict[str, list[float]] = {}
        for r in results:
            d   = r["snapshot_date"]
            ret = r.get(key)
            if ret is None:
                continue
            all_by_date.setdefault(d, []).append(ret)
            if r.get("passed_filters"):
                passed_by_date.setdefault(d, []).append(ret)

        perm = _permutation_test(all_dates, passed_by_date, all_by_date, hd, n_permutations)
        report.permutation_tests.append(perm)
        print(f"p={perm.p_value:.4f}, effect={perm.effect_size:+.2f}σ")

        passed_rets = np.array(
            [r[key] for r in passed_results if r.get(key) is not None], dtype=np.float64
        )
        failed_rets = np.array(
            [r[key] for r in failed_results if r.get(key) is not None], dtype=np.float64
        )

        hd_key = f"{hd}d"
        if len(passed_rets):
            report.avg_returns[hd_key]    = round(float(np.mean(passed_rets)), 2)
            report.median_returns[hd_key] = round(float(np.median(passed_rets)), 2)
            report.win_rates[hd_key]      = round(float(np.mean(passed_rets > 0)) * 100, 1)
            report.sharpe_ratio[hd_key]   = _compute_sharpe(passed_rets, 252 / hd)
            report.max_drawdown[hd_key]   = _compute_max_drawdown(passed_rets)
        if len(failed_rets):
            report.failed_filter_returns[hd_key] = round(float(np.mean(failed_rets)), 2)

    # Macro regime breakdown
    if any(r.get("macro_regime") for r in results):
        print("  Macro regime analysis...", end=" ", flush=True)
        report.macro_regime_stats = _macro_regime_analysis(results, primary_hd)
        print(f"{len(report.macro_regime_stats)} groups")

    # Benchmark alpha
    if benchmark_returns:
        # Record the raw benchmark levels too, not just the alpha, so a saved
        # report shows what the screener was actually measured against.
        report.benchmark_returns = benchmark_returns
        for hd in hold_days:
            hd_key     = f"{hd}d"
            passed_avg = report.avg_returns.get(hd_key)
            spy        = benchmark_returns.get("SPY", {}).get(hd_key)
            xbi        = benchmark_returns.get("XBI", {}).get(hd_key)
            if passed_avg is not None and spy is not None:
                report.screener_vs_spy[hd_key] = round(passed_avg - spy, 2)
            if passed_avg is not None and xbi is not None:
                report.screener_vs_xbi[hd_key] = round(passed_avg - xbi, 2)

    # Verdict
    sig_tests = [t for t in report.permutation_tests if t.significant]
    report.overall_significant = len(sig_tests) >= len(hold_days) // 2 + 1

    if report.overall_significant:
        best = min(report.permutation_tests, key=lambda t: t.p_value)
        report.summary = (
            f"SIGNIFICANT: Screener outperforms random selection in "
            f"{len(sig_tests)}/{len(hold_days)} hold periods. "
            f"Best: {best.hold_days}d mean={best.observed_mean:+.2f}% "
            f"(p={best.p_value:.4f}, effect={best.effect_size:+.2f}σ)."
        )
    else:
        avg_p = float(np.mean([t.p_value for t in report.permutation_tests]))
        report.summary = (
            f"NOT SIGNIFICANT: Screener indistinguishable from random selection "
            f"(avg p={avg_p:.3f} across {len(hold_days)} hold periods). "
            f"Consider: larger sample, different filter thresholds, or macro-regime filtering."
        )

    return report


# ---------------------------------------------------------------------------
# Report output
# ---------------------------------------------------------------------------

def print_report(report: AnalysisReport) -> None:
    SEP = "=" * 70
    print(f"\n{SEP}")
    print("FORM25 BACKTEST STATISTICAL ANALYSIS")
    print(SEP)
    print(f"Period:    {report.start_date} -> {report.end_date}")
    print(f"Portfolio: {report.portfolio.upper()}")
    print(f"Universe:  {report.n_tickers} tickers, {report.n_trading_days} trading days")
    print(f"\n>>> {report.summary}")

    print(f"\n{'-'*70}")
    print("PERMUTATION TESTS")
    print(f"{'Hold':>6}  {'Observed':>9}  {'Null':>9}  {'Effect':>7}  {'p-value':>8}  {'95% CI':>18}  Sig")
    print("-" * 70)
    for t in report.permutation_tests:
        ci = f"[{t.ci_lower_95:+.2f}, {t.ci_upper_95:+.2f}]"
        sig = "✓" if t.significant else "✗"
        print(f"{t.hold_days:>4}d  {t.observed_mean:>+9.2f}  {t.null_mean:>+9.2f}  "
              f"{t.effect_size:>+7.2f}  {t.p_value:>8.4f}  {ci:>18}  {sig}")

    print(f"\n{'-'*70}")
    print("PORTFOLIO PERFORMANCE")
    hds = sorted(report.avg_returns.keys())
    print(f"{'Metric':<32}" + "".join(f"{h:>12}" for h in hds))
    print("-" * (32 + 12 * len(hds)))
    for label, src in [
        ("Screener mean return %",   report.avg_returns),
        ("Screener median return %",  report.median_returns),
        ("Failed-filter mean %",      report.failed_filter_returns),
        ("Win rate %",                report.win_rates),
        ("Sharpe ratio",              report.sharpe_ratio),
        ("Max drawdown %",            report.max_drawdown),
    ]:
        print(f"{label:<32}" + "".join(f"{src.get(h, 'N/A'):>12}" for h in hds))
    if report.screener_vs_spy:
        print(f"{'vs SPY (alpha %)':<32}" + "".join(
            f"{report.screener_vs_spy.get(h, 'N/A'):>12}" for h in hds))
    if report.screener_vs_xbi:
        print(f"{'vs XBI (alpha %)':<32}" + "".join(
            f"{report.screener_vs_xbi.get(h, 'N/A'):>12}" for h in hds))

    if report.macro_regime_stats:
        print(f"\n{'-'*70}")
        print("MACRO REGIME BREAKDOWN")
        print(f"{'Regime':<42}  {'N':>5}  {'Mean%':>7}  {'Win%':>6}  {'vs All':>7}")
        print("-" * 72)
        for m in report.macro_regime_stats[:10]:
            print(f"{m.regime:<42}  {m.n:>5}  {m.mean_return:>+7.2f}  "
                  f"{m.win_rate:>6.1%}  {m.vs_all_mean:>+7.2f}")

    print(f"\n{SEP}\n")


def report_to_dict(report: AnalysisReport) -> dict:
    return asdict(report)


def save_report(report: AnalysisReport, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(report_to_dict(report), f, indent=2)
    print(f"Report saved to {path}")


if __name__ == "__main__":
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from backtest.daily_runner import load_backtest_results

    start = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_START_DATE
    end   = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_END_DATE

    print(f"Loading results {start} -> {end}...")
    results = load_backtest_results(start, end)
    if not results:
        print("No results found. Run scripts/backtest.py first.")
        sys.exit(1)

    report = run_full_analysis(results, hold_days=[30, 60, 90], start_date=start, end_date=end)
    print_report(report)
    save_report(report, f"backtest/analysis_{start}_{end}.json")
