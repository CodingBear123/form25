"""
Analysis cache builder.

Extracts the columns needed for all analysis scripts from the snapshots
table and saves them as a compressed numpy .npz file.

Loading from .npz is ~100x faster than parsing 352k JSON blobs from SQLite.

The cache is invalidated automatically if new snapshots have been added
since it was last built (checked via snapshot count).

Called via:
    python form25.py cache              # build or rebuild cache
    from scripts.analysis_cache import load_cache   # used by analysis scripts
"""

import sys
import os
import json
import sqlite3
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.config import FORM25_DB_PATH, DEFAULT_START_DATE, DEFAULT_END_DATE

CACHE_PATH  = os.path.join(os.path.dirname(__file__), '..', 'db', 'analysis_cache.npz')
META_PATH   = os.path.join(os.path.dirname(__file__), '..', 'db', 'analysis_cache_meta.json')

# String columns stored as object arrays
STR_COLS = [
    "ticker", "snapshot_date", "macro_regime",
    "yield_curve_shape", "risk_environment",
]

# Float columns stored as float32 arrays (saves memory vs float64)
FLOAT_COLS = [
    "cash_ratio", "cash_runway_months",
    "market_cap", "price_vs_52w_high_pct",
    "current_price", "erp_implied", "vix", "fed_funds_rate",
    "return_30d_pct", "return_60d_pct", "return_90d_pct",
]

# Bool columns
BOOL_COLS = ["passed_filters"]


def _get_snapshot_count(db_path: str = FORM25_DB_PATH) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE sector='biotech' AND portfolio='value'"
    ).fetchone()[0]
    conn.close()
    return n


def _cache_is_valid(db_path: str = FORM25_DB_PATH) -> bool:
    """Return True if cache exists and snapshot count hasn't changed."""
    if not os.path.exists(CACHE_PATH) or not os.path.exists(META_PATH):
        return False
    try:
        with open(META_PATH) as f:
            meta = json.load(f)
        current_count = _get_snapshot_count(db_path)
        return meta.get("snapshot_count") == current_count
    except Exception:
        return False


def build_cache(
    db_path:    str = FORM25_DB_PATH,
    start_date: str = DEFAULT_START_DATE,
    end_date:   str = DEFAULT_END_DATE,
    force:      bool = False,
) -> str:
    """
    Build the analysis cache from the snapshots table.
    Returns path to the cache file.
    """
    if not force and _cache_is_valid(db_path):
        print(f"Cache is up to date ({_get_snapshot_count(db_path):,} snapshots). "
              f"Use --force to rebuild.")
        return CACHE_PATH

    print("Building analysis cache from snapshots table...")
    print("(This runs once — subsequent analyses load in < 1 second)\n")

    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT data FROM snapshots
        WHERE sector = 'biotech' AND portfolio = 'value'
          AND snapshot_date BETWEEN ? AND ?
        ORDER BY snapshot_date, ticker
    """, (start_date, end_date)).fetchall()
    conn.close()

    total = len(rows)
    print(f"Parsing {total:,} snapshots...", end=" ", flush=True)

    # Pre-allocate lists
    arrays: dict[str, list] = {col: [] for col in STR_COLS + FLOAT_COLS + BOOL_COLS}

    parsed = 0
    errors = 0
    for row in rows:
        try:
            d = json.loads(row[0])
        except Exception:
            errors += 1
            continue

        for col in STR_COLS:
            arrays[col].append(d.get(col, "") or "")

        for col in FLOAT_COLS:
            val = d.get(col)
            arrays[col].append(float(val) if val is not None else np.nan)

        for col in BOOL_COLS:
            arrays[col].append(bool(d.get(col, False)))

        parsed += 1

    print(f"done ({parsed:,} parsed, {errors} errors)")

    # Convert to numpy
    print("Converting to numpy arrays...", end=" ", flush=True)
    np_arrays: dict[str, np.ndarray] = {}

    for col in STR_COLS:
        np_arrays[col] = np.array(arrays[col], dtype=object)

    for col in FLOAT_COLS:
        np_arrays[col] = np.array(arrays[col], dtype=np.float32)

    for col in BOOL_COLS:
        np_arrays[col] = np.array(arrays[col], dtype=bool)

    print("done")

    # Save
    os.makedirs(os.path.dirname(os.path.abspath(CACHE_PATH)), exist_ok=True)
    print(f"Saving to {os.path.basename(CACHE_PATH)}...", end=" ", flush=True)
    np.savez_compressed(CACHE_PATH, **np_arrays)
    print("done")

    # Save metadata
    meta = {
        "snapshot_count": _get_snapshot_count(db_path),
        "parsed":         parsed,
        "start_date":     start_date,
        "end_date":       end_date,
        "built_at":       datetime.now().isoformat(),
        "columns":        list(np_arrays.keys()),
    }
    with open(META_PATH, "w") as f:
        json.dump(meta, f, indent=2)

    size_mb = os.path.getsize(CACHE_PATH) / 1e6
    print(f"\nCache built: {parsed:,} rows, {size_mb:.1f} MB")
    print(f"Location: {os.path.abspath(CACHE_PATH)}")
    return CACHE_PATH


def load_cache(
    db_path:    str = FORM25_DB_PATH,
    start_date: str = DEFAULT_START_DATE,
    end_date:   str = DEFAULT_END_DATE,
    auto_build: bool = True,
) -> dict[str, np.ndarray]:
    """
    Load the analysis cache. Builds it first if missing or stale.

    Returns dict of column_name -> numpy array.
    All arrays have the same length (one entry per snapshot).

    Usage:
        from scripts.analysis_cache import load_cache
        c = load_cache()
        passed = c["passed_filters"]
        returns_30d = c["return_30d_pct"]
        elevated = c["risk_environment"] == "elevated"
        # Combine masks
        mask = passed & elevated & ~np.isnan(returns_30d)
        print(returns_30d[mask].mean())
    """
    if not _cache_is_valid(db_path):
        if auto_build:
            build_cache(db_path, start_date, end_date)
        else:
            raise FileNotFoundError(
                "Analysis cache not found or stale. "
                "Run: python form25.py cache"
            )

    data = np.load(CACHE_PATH, allow_pickle=True)
    return {k: data[k] for k in data.files}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Rebuild even if up to date")
    parser.add_argument("--start", default=DEFAULT_START_DATE)
    parser.add_argument("--end",   default=DEFAULT_END_DATE)
    args = parser.parse_args()
    build_cache(force=args.force, start_date=args.start, end_date=args.end)
