"""
Reason-weighted delisting returns for the survivorship bounds.

Run: python -m unittest discover tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.survivorship_bounds import blended_delisting_return  # noqa: E402


class BlendedDelistingReturnTest(unittest.TestCase):
    """
    A flat -30% assumes every delisting is a failure. The measured SEC mix for
    this universe is ~70% acquisitions to ~11% bankruptcies, and acquisitions
    are usually a positive outcome for a long-only holder. Weighting by the
    real mix matters more than any other single assumption in the bounds.
    """

    MIX = {"acquisition_or_private": 282, "unknown": 81, "bankruptcy": 43}

    def test_acquisition_dominated_mix_is_milder_than_flat_crsp(self):
        blended = blended_delisting_return(self.MIX, acquisition_return=0.0)
        self.assertGreater(blended, -30.0)
        self.assertLess(blended, 0.0)

    def test_acquisition_premium_moves_the_blend_positive(self):
        low = blended_delisting_return(self.MIX, acquisition_return=0.0)
        high = blended_delisting_return(self.MIX, acquisition_return=30.0)
        self.assertGreater(high, low)
        self.assertGreater(high, 0.0)

    def test_all_bankruptcies_reproduces_the_pessimistic_floor(self):
        self.assertEqual(
            blended_delisting_return({"bankruptcy": 10}, acquisition_return=0.0),
            -100.0,
        )

    def test_empty_mix_returns_none(self):
        self.assertIsNone(blended_delisting_return({}, acquisition_return=0.0))


if __name__ == "__main__":
    unittest.main()
