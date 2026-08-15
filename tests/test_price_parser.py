"""Unit tests for daft_monitor.price_parser.

Run with:
    python -m unittest tests.test_price_parser
"""

from __future__ import annotations

import unittest

from daft_monitor.price_parser import monthly_equivalent, parse_price, parse_price_fields


class ParsePriceTests(unittest.TestCase):
    def test_per_month(self) -> None:
        self.assertEqual(parse_price("€1,200 per month"), (1200.0, "month"))
        self.assertEqual(parse_price("EUR1,150 per month"), (1150.0, "month"))

    def test_per_week(self) -> None:
        self.assertEqual(parse_price("€250 per week"), (250.0, "week"))
        self.assertEqual(parse_price("€160 per week"), (160.0, "week"))

    def test_from_to_month_midpoint(self) -> None:
        value, period = parse_price("From €700 to €1,250 per month")
        self.assertEqual(period, "month")
        self.assertEqual(value, 975.0)

    def test_from_to_bedrooms_heuristic(self) -> None:
        high, _ = parse_price("From €700 to €1,250 per month", "Single & Double Room")
        self.assertEqual(high, 1250.0)
        low, _ = parse_price("From €700 to €1,250 per month", "Double & Twin Room")
        self.assertEqual(low, 700.0)

    def test_from_to_week(self) -> None:
        value, period = parse_price("From €1 to €210 per week")
        self.assertEqual(period, "week")
        self.assertEqual(value, 105.5)

    def test_sale_plain(self) -> None:
        self.assertEqual(parse_price("€395,000"), (395000.0, "sale"))
        self.assertEqual(parse_price("€449,000"), (449000.0, "sale"))

    def test_sale_from(self) -> None:
        self.assertEqual(parse_price("From €310,000"), (310000.0, "sale"))

    def test_sale_amv(self) -> None:
        self.assertEqual(parse_price("AMV: €300,000"), (300000.0, "sale"))
        self.assertEqual(parse_price("AMV €265,000"), (265000.0, "sale"))

    def test_poa(self) -> None:
        self.assertEqual(parse_price("Price on Application"), (None, None))
        self.assertEqual(parse_price("POA"), (None, None))

    def test_empty(self) -> None:
        self.assertEqual(parse_price(None), (None, None))
        self.assertEqual(parse_price(""), (None, None))
        self.assertEqual(parse_price("   "), (None, None))

    def test_monthly_equivalent(self) -> None:
        self.assertEqual(monthly_equivalent(1200.0, "month"), 1200.0)
        self.assertAlmostEqual(monthly_equivalent(250.0, "week") or 0.0, 250.0 * 52.0 / 12.0)
        self.assertIsNone(monthly_equivalent(395000.0, "sale"))
        self.assertIsNone(monthly_equivalent(None, None))

    def test_parse_price_fields(self) -> None:
        value, period, monthly = parse_price_fields("€250 per week")
        self.assertEqual(value, 250.0)
        self.assertEqual(period, "week")
        self.assertAlmostEqual(monthly or 0.0, 250.0 * 52.0 / 12.0)


if __name__ == "__main__":
    unittest.main()
