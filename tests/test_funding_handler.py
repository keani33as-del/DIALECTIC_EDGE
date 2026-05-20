from __future__ import annotations

import os
import unittest

os.environ.setdefault("BOT_TOKEN", "test:test")

from refactor.handlers.funding_handler import FundingRow, classify_funding, format_funding_report


class TestFundingClassification(unittest.TestCase):
    def test_positive_anomaly_is_short_edge(self):
        emoji, label = classify_funding(0.001)
        self.assertEqual(emoji, "🔴")
        self.assertIn("short-edge", label)

    def test_negative_anomaly_is_squeeze_risk(self):
        emoji, label = classify_funding(-0.001)
        self.assertEqual(emoji, "🟢")
        self.assertIn("squeeze", label)

    def test_neutral(self):
        emoji, label = classify_funding(0.00001)
        self.assertEqual(emoji, "⚪")
        self.assertIn("edge слабый", label)


class TestFundingReport(unittest.TestCase):
    def test_formats_rows_and_summary(self):
        text = format_funding_report([
            FundingRow("BTCUSDT", 0.0004, next_funding_time_ms=1760000000000),
            FundingRow("ETHUSDT", -0.0005, next_funding_time_ms=1760000000000),
        ])
        self.assertIn("Funding rates", text)
        self.assertIn("BTC", text)
        self.assertIn("ETH", text)
        self.assertIn("Short-watch", text)
        self.assertIn("Squeeze-watch", text)

    def test_empty_report(self):
        text = format_funding_report([])
        self.assertIn("Данных", text)


if __name__ == "__main__":
    unittest.main()
