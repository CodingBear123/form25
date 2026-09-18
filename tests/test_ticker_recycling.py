"""
Tests for the ticker-recycling guard.

Modelled on the real SRNE case: Sorrento delisted 2023-04-12, but yfinance
returns bars through 2026 belonging to a different company that later took
the symbol. Splicing those onto Sorrento's identity would turn a bankruptcy
into a going concern.

Run: python -m unittest discover tests
"""

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.ticker_recycling import (  # noqa: E402
    find_recycled_tickers,
    purge_post_delisting_bars,
)


def _bars(conn, ticker, start, end):
    day = dt.date.fromisoformat(start)
    stop = dt.date.fromisoformat(end)
    rows = []
    while day <= stop:
        if day.weekday() < 5:
            rows.append((ticker, day.isoformat(), 1.0))
        day += dt.timedelta(days=1)
    conn.executemany("INSERT INTO daily_prices VALUES (?,?,?)", rows)


class RecyclingGuardTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.prices = os.path.join(self.dir.name, "prices.db")
        conn = sqlite3.connect(self.prices)
        conn.execute("CREATE TABLE daily_prices (ticker TEXT, date TEXT, close REAL)")
        # SRNE: real history to the delisting, then a recycled company's bars.
        _bars(conn, "SRNE", "2022-01-03", "2023-04-12")
        _bars(conn, "SRNE", "2024-06-03", "2026-09-17")
        # CLVS: clean — stops at its delisting.
        _bars(conn, "CLVS", "2022-01-03", "2022-12-29")
        # MRNA: alive, never delisted, must be untouched.
        _bars(conn, "MRNA", "2022-01-03", "2026-09-17")
        conn.commit()
        conn.close()

        self.cutoffs = {"SRNE": "2023-04-12", "CLVS": "2022-12-29"}

    def tearDown(self):
        self.dir.cleanup()

    def _count(self, ticker):
        conn = sqlite3.connect(self.prices)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM daily_prices WHERE ticker = ?", (ticker,)
            ).fetchone()[0]
        finally:
            conn.close()

    def test_detects_the_recycled_symbol(self):
        found = find_recycled_tickers(self.cutoffs, prices_db=self.prices)
        self.assertEqual([f["ticker"] for f in found], ["SRNE"])
        self.assertGreater(found[0]["bars_after_delisting"], 500)

    def test_clean_delisting_is_not_flagged(self):
        found = find_recycled_tickers(self.cutoffs, prices_db=self.prices)
        self.assertNotIn("CLVS", [f["ticker"] for f in found])

    def test_purge_removes_only_post_delisting_bars(self):
        before = self._count("SRNE")
        result = purge_post_delisting_bars(self.cutoffs, prices_db=self.prices)
        after = self._count("SRNE")

        self.assertGreater(result["bars_deleted"], 0)
        self.assertLess(after, before)
        # Everything left must predate the delisting.
        conn = sqlite3.connect(self.prices)
        max_date = conn.execute(
            "SELECT MAX(date) FROM daily_prices WHERE ticker = 'SRNE'"
        ).fetchone()[0]
        conn.close()
        self.assertLessEqual(max_date, "2023-04-12")

    def test_purge_leaves_live_tickers_alone(self):
        before = self._count("MRNA")
        purge_post_delisting_bars(self.cutoffs, prices_db=self.prices)
        self.assertEqual(self._count("MRNA"), before)

    def test_dry_run_deletes_nothing(self):
        before = self._count("SRNE")
        result = purge_post_delisting_bars(
            self.cutoffs, prices_db=self.prices, dry_run=True
        )
        self.assertGreater(result["bars_deleted"], 0)
        self.assertEqual(self._count("SRNE"), before)

    def test_no_cutoffs_is_a_safe_noop(self):
        before = self._count("SRNE")
        result = purge_post_delisting_bars({}, prices_db=self.prices)
        self.assertEqual(result["bars_deleted"], 0)
        self.assertEqual(self._count("SRNE"), before)


if __name__ == "__main__":
    unittest.main()
