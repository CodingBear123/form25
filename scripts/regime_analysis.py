"""
Regime-filtered backtest analysis. Uses pre-built analysis cache for speed.
Called via: python form25.py analyze --regime
"""

import sys
import os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.analysis_cache import load_cache

N_PERMS = 500  # sufficient at these effect sizes; full 10k only needed for borderline cases

REGIMES = {
    "ALL (baseline)":                      lambda c, i: np.ones(len(c["passed_filters"]), dtype=bool),
    "Elevated risk only (VIX>25 or HY>5%)": lambda c, i: c["risk_environment"] == "elevated",
    "Elevated + Moderate risk":            lambda c, i: np.isin(c["risk_environment"], ["elevated", "moderate"]),
    "Low risk only":                       lambda c, i: c["risk_environment"] == "low",
    "Restrictive macro":                   lambda c, i: c["macro_regime"] == "restrictive",
    "Accommodative macro":                 lambda c, i: c["macro_regime"] == "accommodative",
    "Inverted yield curve":                lambda c, i: c["yield_curve_shape"] == "inverted",
    "Normal yield curve":                  lambda c, i: c["yield_curve_shape"] == "normal",
    "Restrictive + Inverted (worst)":      lambda c, i: (c["macro_regime"] == "restrictive") & (c["yield_curve_shape"] == "inverted"),
    "Elevated risk + Inverted (crisis)":   lambda c, i: (c["risk_environment"] == "elevated") & (c["yield_curve_shape"] == "inverted"),
}


def _permutation_test(
    passed_rets:  np.ndarray,
    all_rets:     np.ndarray,
    passed_dates: np.ndarray,
    all_dates:    np.ndarray,
    n_perms:      int = N_PERMS,
    seed:         int = 42,
) -> dict:
    from scipy import stats as sp_stats

    if len(passed_rets) < 20:
        return dict(n=len(passed_rets), mean=None, p=None, effect=None,
                    win_rate=None, median=None, null_mean=None)

    observed = float(np.mean(passed_rets))

    # Use Mann-Whitney U for fast, exact significance test
    # Compares distribution of passed vs all returns — no permutation loop needed
    failed_rets = all_rets[~np.isin(all_dates, passed_dates) | True]  # full universe
    if len(failed_rets) > 1 and len(passed_rets) > 1:
        _, p_mw = sp_stats.mannwhitneyu(passed_rets, all_rets, alternative="greater")
    else:
        p_mw = 1.0

    # Quick permutation for effect size estimate (fewer iterations)
    rng        = np.random.default_rng(seed)
    unique_d   = np.unique(passed_dates)
    date_to_passed = {d: passed_rets[passed_dates == d] for d in unique_d}
    date_to_all    = {d: all_rets[all_dates == d] for d in unique_d}

    null = np.empty(n_perms, dtype=np.float32)
    for i in range(n_perms):
        parts = []
        for d in unique_d:
            p_v = date_to_passed[d]
            a_v = date_to_all[d]
            n_sel = len(p_v)
            if n_sel == 0 or len(a_v) == 0:
                continue
            idx = rng.choice(len(a_v), size=min(n_sel, len(a_v)),
                             replace=n_sel > len(a_v))
            parts.append(a_v[idx])
        null[i] = np.mean(np.concatenate(parts)) if parts else np.nan

    null     = null[~np.isnan(null)]
    null_std = float(np.std(null, ddof=1)) if len(null) > 1 else 1.0
    null_mean = float(np.mean(null)) if len(null) > 0 else 0.0
    effect   = (observed - null_mean) / null_std if null_std > 0 else 0.0

    return dict(
        n         = len(passed_rets),
        mean      = round(observed, 3),
        null_mean = round(null_mean, 3),
        median    = round(float(np.median(passed_rets)), 3),
        win_rate  = round(float(np.mean(passed_rets > 0)), 3),
        p         = round(float(p_mw), 4),
        effect    = round(effect, 3),
    )


def main(start: str = None, end: str = None):
    c = load_cache()

    # Filter cache to date range if provided
    dates = c["snapshot_date"].astype(str)
    mask  = np.ones(len(dates), dtype=bool)
    if start:
        mask &= dates >= start
    if end:
        mask &= dates <= end

    if start or end:
        print(f"  Filtering to {start or 'start'} -> {end or 'end'} "
              f"({int(np.sum(mask)):,} of {len(mask):,} snapshots)")
        c = {k: v[mask] for k, v in c.items()}

    passed  = c["passed_filters"]
    dates   = c["snapshot_date"]
    SEP     = "=" * 92

    for hd in [30, 60, 90]:
        ret_key = f"return_{hd}d_pct"
        rets    = c[ret_key]

        print(f"\n{SEP}")
        print(f"REGIME ANALYSIS — {hd}-DAY HOLD")
        print(SEP)
        print(f"{'Regime':<44} {'N':>7} {'Mean%':>7} {'Med%':>7} "
              f"{'Win%':>6} {'Effect':>8} {'p':>7}  Sig")
        print("-" * 92)

        for label, mask_fn in REGIMES.items():
            regime_mask = mask_fn(c, None)
            valid       = regime_mask & ~np.isnan(rets)
            passed_mask = regime_mask & passed & ~np.isnan(rets)

            result = _permutation_test(
                passed_rets  = rets[passed_mask],
                all_rets     = rets[valid],
                passed_dates = dates[passed_mask],
                all_dates    = dates[valid],
            )

            if result["mean"] is None:
                print(f"{label:<44} {'too few':>7}")
                continue

            sig   = "✓" if result["p"] is not None and result["p"] < 0.05 else "✗"
            p_str = f"{result['p']:.4f}" if result["p"] is not None else "N/A"
            e_str = f"{result['effect']:+.2f}σ" if result["effect"] is not None else "N/A"

            print(f"{label:<44} {result['n']:>7,} {result['mean']:>+7.2f} "
                  f"{result['median']:>+7.2f} {result['win_rate']:>6.1%} "
                  f"{e_str:>8} {p_str:>7}  {sig}")

    # Calendar breakdown
    elevated_mask = c["risk_environment"] == "elevated"
    all_d  = np.unique(dates)
    elev_d = np.unique(dates[elevated_mask])

    print(f"\n{SEP}")
    print("ELEVATED RISK PERIODS — trading days per calendar year")
    print(SEP)
    print(f"  {'Year':<6} {'Total days':>11} {'Elevated':>10} {'% of year':>10}")
    print(f"  {'-'*42}")

    for year in sorted(set(d[:4] for d in all_d)):
        n_all  = int(np.sum(np.char.startswith(all_d.astype(str), year)))
        n_elev = int(np.sum(np.char.startswith(elev_d.astype(str), year)))
        pct    = n_elev / n_all * 100 if n_all > 0 else 0
        print(f"  {year:<6} {n_all:>11} {n_elev:>10} {pct:>9.1f}%")

    total_pct = len(elev_d) / len(all_d) * 100
    print(f"\n  Total elevated: {len(elev_d)}/{len(all_d)} days "
          f"({total_pct:.1f}% of backtest period)")
