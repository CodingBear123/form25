"""
Tests for the SEC delisting heuristic.

The bug these exist to prevent: a Form 25 is filed whenever any class of
security is removed from an exchange — an expiring warrant, a maturing note,
a move from NYSE to Nasdaq. Treating every Form 25 as a death certificate
marked Amgen, ADMA and Brainstorm as delisted in a live run. Killing off live
companies corrupts the backtest just as badly as ignoring dead ones, in the
opposite direction.

Filing tuples are (form, date, accession, primary_doc, items).

Run: python -m unittest discover tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.fetch_delistings import determine_fate  # noqa: E402


def f(form, date, items=""):
    return (form, date, "0001-00", "doc.htm", items)


class DetermineFateTest(unittest.TestCase):

    def test_active_company_has_no_fate(self):
        date, reason = determine_fate([
            f("10-K", "2021-02-01"),
            f("10-Q", "2021-05-01"),
            f("10-K", "2022-02-01"),
        ])
        self.assertIsNone(date)
        self.assertIsNone(reason)

    def test_form_25_followed_by_more_10ks_is_not_a_delisting(self):
        """The Amgen case — an exchange transfer, not a death."""
        date, reason = determine_fate([
            f("10-K", "2020-02-01"),
            f("25-NSE", "2020-12-28"),
            f("10-K", "2021-02-15"),
            f("10-K", "2022-02-15"),
        ])
        self.assertIsNone(date, "company kept filing; must not be delisted")
        self.assertIsNone(reason)

    def test_form_25_then_silence_is_a_delisting(self):
        date, reason = determine_fate([
            f("10-K", "2022-02-24"),
            f("25-NSE", "2022-12-29"),
        ])
        self.assertEqual(date, "2022-12-29")
        self.assertEqual(reason, "unknown")

    def test_bankruptcy_8k_sets_the_reason(self):
        date, reason = determine_fate([
            f("10-K", "2022-02-24"),
            f("8-K", "2022-12-11", "1.03"),
            f("25-NSE", "2022-12-29"),
        ])
        self.assertEqual(date, "2022-12-29")
        self.assertEqual(reason, "bankruptcy")

    def test_delisting_plus_deregistration_reads_as_acquisition(self):
        date, reason = determine_fate([
            f("10-K", "2021-02-01"),
            f("25-NSE", "2021-11-22"),
            f("15-12B", "2021-12-02"),
        ])
        self.assertEqual(date, "2021-11-22")
        self.assertEqual(reason, "acquisition_or_private")

    def test_earliest_form_25_wins(self):
        date, _ = determine_fate([
            f("25-NSE", "2023-04-12"),
            f("25", "2023-06-01"),
        ])
        self.assertEqual(date, "2023-04-12")

    def test_straggler_filing_inside_grace_window_still_counts_as_dead(self):
        """A final 10-Q landing weeks after the Form 25 is normal wind-down."""
        date, _ = determine_fate([
            f("10-K", "2022-02-24"),
            f("25-NSE", "2022-12-29"),
            f("10-Q", "2023-01-20"),
        ])
        self.assertEqual(date, "2022-12-29")

    def test_deregistration_alone_is_a_delisting(self):
        date, reason = determine_fate([
            f("10-K", "2020-03-01"),
            f("15-12G", "2020-09-01"),
        ])
        self.assertEqual(date, "2020-09-01")
        self.assertEqual(reason, "unknown")

    def test_empty_history_is_not_a_delisting(self):
        self.assertEqual(determine_fate([]), (None, None))


if __name__ == "__main__":
    unittest.main()
