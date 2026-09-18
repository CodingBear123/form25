"""
Survivorship-bias guards.

The failure these cover is silent: a delisted ticker whose forward return is
None drops out of the sample, so a bankruptcy scores as a non-event and
measured returns drift upward. The most dangerous regression here is the
opposite one — mistaking "the hold period hasn't finished yet" for a
delisting, which would book -30% on every recent snapshot.

Run: python -m unittest discover tests
"""

import datetime as dt
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.daily_runner import (  # noqa: E402
    DELISTING_RETURN_PCT,
    _get_forward_price,
    _was_listed_on,
)

DB_END = dt.date(2026, 9, 18)


def _price_db() -> sqlite3.Connection:
    """ALIVE trades to the end of the data; DEAD stops on 2021-03-01."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE daily_prices "
        "(ticker TEXT, date TEXT, close REAL, high REAL, low REAL)"
    )
    rows = []
    day = dt.date(2021, 1, 4)
    while day <= DB_END:
        if day.weekday() < 5:
            rows.append(("ALIVE", day.isoformat(), 10.0, 10.0, 10.0))
            if day <= dt.date(2021, 3, 1):
                rows.append(("DEAD", day.isoformat(), 5.0, 5.0, 5.0))
        day += dt.timedelta(days=1)
    conn.executemany("INSERT INTO daily_prices VALUES (?,?,?,?,?)", rows)
    return conn


class ForwardPriceTest(unittest.TestCase):
    def setUp(self) -> None:
        # The lookup memoises per ticker, so clear it between tests.
        from backtest import daily_runner

        daily_runner._last_bar_cache.clear()
        daily_runner._db_end_cache.clear()
        self.conn = _price_db()

    def tearDown(self) -> None:
        self.conn.close()

    def test_live_ticker_resolves_to_a_real_bar(self):
        price, outcome = _get_forward_price("ALIVE", "2021-06-01", self.conn)
        self.assertEqual(outcome, "ok")
        self.assertEqual(price, 10.0)

    def test_price_before_delisting_still_resolves(self):
        price, outcome = _get_forward_price("DEAD", "2021-02-15", self.conn)
        self.assertEqual(outcome, "ok")
        self.assertEqual(price, 5.0)

    def test_delisted_ticker_is_flagged_not_dropped(self):
        _, outcome = _get_forward_price("DEAD", "2021-06-01", self.conn)
        self.assertEqual(outcome, "delisted")

    def test_incomplete_hold_period_is_not_a_delisting(self):
        """The regression that would poison every recent snapshot."""
        for ticker in ("ALIVE", "DEAD"):
            _, outcome = _get_forward_price(ticker, "2027-01-01", self.conn)
            self.assertEqual(outcome, "truncated", ticker)

    def test_weekend_gap_is_tolerated(self):
        # 2021-06-05 is a Saturday; the Friday bar should satisfy it.
        _, outcome = _get_forward_price("ALIVE", "2021-06-05", self.conn)
        self.assertEqual(outcome, "ok")

    def test_delisting_return_is_a_loss(self):
        self.assertLess(DELISTING_RETURN_PCT, 0)


class ListingWindowTest(unittest.TestCase):
    WINDOWS = {
        "DEAD": (None, "2021-03-02"),
        "NEW": ("2022-01-01", None),
    }

    def test_delisted_name_is_screened_out_after_its_last_day(self):
        self.assertTrue(_was_listed_on("DEAD", "2021-01-05", self.WINDOWS))
        self.assertFalse(_was_listed_on("DEAD", "2021-06-01", self.WINDOWS))

    def test_ipo_is_screened_out_before_it_listed(self):
        self.assertFalse(_was_listed_on("NEW", "2021-06-01", self.WINDOWS))
        self.assertTrue(_was_listed_on("NEW", "2023-01-01", self.WINDOWS))

    def test_unknown_ticker_is_assumed_listed(self):
        # An incomplete universe table must not silently empty the backtest.
        self.assertTrue(_was_listed_on("UNKNOWN", "2021-06-01", self.WINDOWS))


if __name__ == "__main__":
    unittest.main()
