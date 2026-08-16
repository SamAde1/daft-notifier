"""Example observation configs must load and stay internally consistent."""

from __future__ import annotations

import unittest
from pathlib import Path

from daft_monitor.config import load_config
from daft_monitor.search_identity import resolved_search_id

ROOT = Path(__file__).resolve().parent.parent


class ExampleConfigTests(unittest.TestCase):
    def test_rent_example_is_four_county_observation(self) -> None:
        cfg = load_config(str(ROOT / "config.example.yaml"))
        self.assertEqual(cfg.check_interval_minutes, 30)
        self.assertEqual(cfg.deep_scan_max_pages, 120)
        self.assertIsNone(cfg.digest_day)
        self.assertEqual(len(cfg.searches), 8)
        ids = [resolved_search_id(search) for search in cfg.searches]
        self.assertEqual(len(ids), len(set(ids)))
        for search in cfg.searches:
            self.assertFalse(search.notify)
            self.assertTrue(search.deep_scan)
            self.assertEqual(search.shallow_pages, 2)
            self.assertIsNone(search.property_type)
            self.assertIsNone(search.min_price)
            self.assertIsNone(search.max_price)
        counties = {
            tuple(search.location) if isinstance(search.location, list) else (search.location,)
            for search in cfg.searches
        }
        self.assertEqual(counties, {("Dublin",), ("Kildare",), ("Meath",), ("Wicklow",)})
        error_prod = [n for n in cfg.notifiers if n.role == "errors" and "prod" in n.environments]
        alert_prod = [n for n in cfg.notifiers if n.role == "alerts" and "prod" in n.environments]
        self.assertTrue(error_prod and error_prod[0].enabled)
        self.assertTrue(alert_prod and not alert_prod[0].enabled)

    def test_sales_example_is_four_county_observation(self) -> None:
        cfg = load_config(str(ROOT / "config.sales.example.yaml"))
        self.assertEqual(cfg.check_interval_minutes, 120)
        self.assertEqual(cfg.deep_scan_max_pages, 120)
        self.assertEqual(len(cfg.searches), 4)
        for search in cfg.searches:
            self.assertEqual(search.search_type, "RESIDENTIAL_SALE")
            self.assertFalse(search.notify)
            self.assertTrue(search.deep_scan)
            self.assertIsNone(search.property_type)
