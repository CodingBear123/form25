"""
Negative caching for filing timelines.

The bug: build_filing_timeline() returned [] when the SEC XBRL fetch failed or
yielded nothing, but _store_timeline() silently refused to persist an empty
timeline. So the cache never recorded the failure and the backtest re-fetched
the same dead ticker on every one of ~1,800 trading days. Nine such tickers
meant ~16,000 unthrottled requests, and SEC responded by refusing connections
partway through the run.

Run: python -m unittest discover tests
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest import filing_timeline as ft  # noqa: E402
from utils import config  # noqa: E402


class NegativeCacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "form25.db")
        ft.init_timeline_table(self.db)
        # sec_headers() refuses to build a User-Agent without a contact
        # address. These tests mock the network, so supply a stand-in rather
        # than depending on the developer's .env.
        self.contact = mock.patch.object(
            config, "SEC_CONTACT_EMAIL", "tests@example.com"
        )
        self.contact.start()

    def tearDown(self):
        self.contact.stop()
        self.dir.cleanup()

    def test_empty_timeline_is_not_stored_by_default(self):
        """The original guard stays intact for ordinary callers."""
        ft._store_timeline("AAA", "0001", "A Co", [], db_path=self.db)
        self.assertIsNone(ft._load_timeline_from_cache("AAA", self.db))

    def test_empty_timeline_is_stored_when_explicitly_allowed(self):
        ft._store_timeline(
            "BBB", "0002", "B Co", [], db_path=self.db, allow_empty=True
        )
        cached = ft._load_timeline_from_cache("BBB", self.db)
        self.assertEqual(cached, [], "negative result must be cached, not absent")
        self.assertIsNotNone(cached, "[] and None mean different things here")

    def test_failed_fetch_is_only_attempted_once(self):
        """The actual regression: no refetch storm across trading days."""
        calls = []

        def boom(*a, **kw):
            calls.append(1)
            raise OSError(61, "Connection refused")

        with mock.patch.object(ft.httpx, "get", side_effect=boom):
            for _ in range(50):  # stand-in for 50 trading days
                cached = ft._load_timeline_from_cache("CRDL", self.db)
                if cached is None:
                    ft.build_filing_timeline(
                        "CRDL", "0001234567", db_path=self.db
                    )

        self.assertEqual(
            len(calls), 1,
            f"expected 1 SEC call across 50 days, made {len(calls)}",
        )

    def test_empty_xbrl_response_is_only_attempted_once(self):
        """Foreign issuers: fetch succeeds but yields no 10-K/10-Q facts."""
        calls = []

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                calls.append(1)
                return {"entityName": "ProQR", "facts": {}}

        with mock.patch.object(ft.httpx, "get", return_value=Resp()):
            for _ in range(50):
                cached = ft._load_timeline_from_cache("PRQR", self.db)
                if cached is None:
                    ft.build_filing_timeline(
                        "PRQR", "0001604416", db_path=self.db
                    )

        self.assertEqual(
            len(calls), 1,
            f"expected 1 SEC call across 50 days, made {len(calls)}",
        )

    def test_throttle_spaces_out_calls(self):
        import time

        ft._last_sec_call = 0.0
        start = time.monotonic()
        for _ in range(5):
            ft._sec_throttle()
        elapsed = time.monotonic() - start
        # 5 calls, 4 gaps; allow slack for the first unthrottled call.
        self.assertGreater(elapsed, ft._SEC_MIN_INTERVAL * 3)


if __name__ == "__main__":
    unittest.main()
