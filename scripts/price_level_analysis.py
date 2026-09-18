"""
Price vs 52-week high level analysis. Uses pre-built analysis cache for speed.
Called via: python form25.py analyze --price-levels
"""

import sys
import os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.analysis_cache import load_cache

HOLD_DAYS = [30, 60, 90]

LEVELS = [
    (0.00, 0.10, "0-10% down"),
    (0.10, 0.20, "10-20% down"),
    (0.20, 0.30, "20-30% down"),
    (0.30, 0.40, "30-40% down"),
    (0.40, 0.50, "40-50% down"),
    (0.50, 0.60, "50-60% down"),
    (0.60, 0.70, "60-70% down"),
    (0.70, 0.80, "70-80% down"),
    (0.80, 0.90, "80-90% down"),
    (0.90, 1.01, "90%+ down"),
]


def main():
    c = load_cache()

    passed   = c["passed_filters"]
    drawdown = np.abs(np.minimum(c["price_vs_52w_high_pct"], 0.0))
    elevated = c["risk_environment"] == "elevated"
    SEP      = "=" * 88

    for subset_name, subset_mask in [("ALL REGIMES", passed),
                                      ("ELEVATED RISK ONLY", passed & elevated)]:
        print(f"\n{SEP}")
        print(f"PRICE VS 52W HIGH — RETURN BY DRAWDOWN BUCKET ({subset_name})")
        print(SEP)

        for hd in HOLD_DAYS:
            rets = c[f"return_{hd}d_pct"]

            print(f"\n  {hd}-day hold:")
            print(f"  {'Bucket':<16} {'N':>6} {'Mean%':>8} {'Median%':>9} "
                  f"{'Win%':>7} {'p90%':>7} {'p10%':>7} {'Payoff':>8}")
            print(f"  {'-' * 72}")

            best_mean  = -999.0
            best_label = ""

            for lo, hi, label in LEVELS:
                mask  = subset_mask & (drawdown >= lo) & (drawdown < hi) & ~np.isnan(rets)
                vals  = rets[mask]
                if len(vals) < 10:
                    continue

                mean    = float(np.mean(vals))
                median  = float(np.median(vals))
                win     = float(np.mean(vals > 0))
                p90     = float(np.percentile(vals, 90))
                p10     = float(np.percentile(vals, 10))
                winners = vals[vals > 0]
                losers  = vals[vals < 0]
                payoff  = (float(np.mean(winners) / abs(np.mean(losers)))
                           if len(winners) > 0 and len(losers) > 0 else float("nan"))

                marker = " ←" if mean > best_mean else ""
                if mean > best_mean:
                    best_mean  = mean
                    best_label = label

                print(f"  {label:<16} {len(vals):>6} {mean:>+8.2f} {median:>+9.2f} "
                      f"{win:>7.1%} {p90:>+7.2f} {p10:>+7.2f} {payoff:>7.2f}x{marker}")

            print(f"\n  ✓ Best at {hd}d: {best_label} ({best_mean:+.2f}% mean)")

    # Scoring recommendation
    print(f"\n{SEP}")
    print("RETURN BY DRAWDOWN BUCKET (30d, elevated risk)")
    print(SEP)
    rets_30 = c["return_30d_pct"]
    print(f"\n  {'Drawdown':<16} {'Mean%':>8}   Relative to best bucket")
    print(f"  {'-'*55}")
    means: list[tuple[str, float | None]] = []
    for lo, hi, label in LEVELS:
        mask = passed & elevated & (drawdown >= lo) & (drawdown < hi) & ~np.isnan(rets_30)
        vals = rets_30[mask]
        if len(vals) < 5:
            means.append((label, None))
            continue
        means.append((label, float(np.mean(vals))))

    valid = [m for _, m in means if m is not None]
    if valid:
        best  = max(valid)
        worst = min(valid)
        rng   = best - worst if best != worst else 1.0

        for label, mean in means:
            if mean is None:
                print(f"  {label:<16} {'N/A':>8}   0.0  (insufficient data)")
                continue
            normalised = max(0.0, (mean - worst) / rng)
            bar = "█" * int(normalised * 20)
            print(f"  {label:<16} {mean:>+8.2f}   {normalised:.2f}  {bar}")

        print(f"\n  These buckets inform where to set the drawdown threshold")
        print(f"  in the hard filters, not a weighting — there is no score to tune.")
