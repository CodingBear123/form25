"""
Benchmark return computation.

The gap this closes: run_full_analysis accepted a benchmark_returns argument
and scripts/backtest.py never passed one, so `if benchmark_returns:` was always
false and every report shipped with empty screener_vs_spy / screener_vs_xbi.
The backtest was only ever compared against random selection inside its own
universe — a far weaker bar than the ETF you could simply buy instead.

Run: python -m unittest discover tests
"""

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.daily_runner import compute_benchmark_returns  # noqa: E402


class BenchmarkReturnsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "prices.db")
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE daily_prices "
            "(ticker TEXT, date TEXT, close REAL, high REAL, low REAL)"
        )
        # FLAT holds at 100. RISER gains exactly 1% per calendar day.
        rows = []
        day = dt.date(2021, 1, 1)
        end = dt.date(2021, 12, 31)
        i = 0
        while day <= end:
            if day.weekday() < 5:
                rows.append(("FLAT", day.isoformat(), 100.0, 100.0, 100.0))
                rows.append(("RISER", day.isoformat(), 100.0 + i, 0.0, 0.0))
            day += dt.timedelta(days=1)
            i += 1
        conn.executemany("INSERT INTO daily_prices VALUES (?,?,?,?,?)", rows)
        conn.commit()
        conn.close()

        self.dates = ["2021-03-01", "2021-04-01", "2021-05-03"]

    def tearDown(self):
        self.dir.cleanup()

    def test_flat_benchmark_returns_zero(self):
        out = compute_benchmark_returns(
            self.dates, hold_days=[30], benchmarks=["FLAT"], prices_db=self.db
        )
        self.assertAlmostEqual(out["FLAT"]["30d"], 0.0, places=2)

    def test_rising_benchmark_returns_positive(self):
        out = compute_benchmark_returns(
            self.dates, hold_days=[30], benchmarks=["RISER"], prices_db=self.db
        )
        self.assertGreater(out["RISER"]["30d"], 0)

    def test_longer_hold_gives_larger_return(self):
        out = compute_benchmark_returns(
            self.dates, hold_days=[30, 90], benchmarks=["RISER"], prices_db=self.db
        )
        self.assertGreater(out["RISER"]["90d"], out["RISER"]["30d"])

    def test_shape_matches_what_run_full_analysis_expects(self):
        out = compute_benchmark_returns(
            self.dates, hold_days=[30, 60], benchmarks=["FLAT"], prices_db=self.db
        )
        self.assertIn("FLAT", out)
        self.assertEqual(set(out["FLAT"]), {"30d", "60d"})

    def test_unknown_benchmark_is_skipped_not_fatal(self):
        out = compute_benchmark_returns(
            self.dates, hold_days=[30], benchmarks=["NOPE"], prices_db=self.db
        )
        self.assertEqual(out, {})

    def test_no_dates_returns_empty(self):
        self.assertEqual(
            compute_benchmark_returns([], benchmarks=["FLAT"], prices_db=self.db),
            {},
        )

    def test_horizon_past_end_of_data_is_excluded(self):
        """Must not fabricate a return by reusing the final available bar."""
        out = compute_benchmark_returns(
            ["2021-12-30"], hold_days=[90], benchmarks=["FLAT"], prices_db=self.db
        )
        self.assertNotIn("90d", out.get("FLAT", {}))


if __name__ == "__main__":
    unittest.main()
