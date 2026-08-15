"""Unit tests for daft_monitor.analytics (Stage 5).

Run with:
    python -m unittest tests.test_analytics
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from daft_monitor import analytics
from daft_monitor.config import SearchConfig
from daft_monitor.constants import EVENT_NEW, EVENT_REMOVED, EVENT_RELISTED, EVENT_SEED
from daft_monitor.models import Listing, ListingEvent
from daft_monitor.storage import Storage


def _search(**overrides: object) -> SearchConfig:
    base: dict[str, object] = dict(
        name="Dublin Sharing",
        search_type="SHARING",
        location=["Dublin", "Dublin City"],
        deep_scan=True,
    )
    base.update(overrides)
    return SearchConfig(**base)  # type: ignore[arg-type]


def _listing(
    listing_id: str,
    *,
    title: str = "Ensuite Double Room",
    location: str = "Dublin City",
    price: str = "€1,200 per month",
    search_name: str = "Dublin Sharing",
) -> Listing:
    return Listing(
        id=listing_id,
        title=title,
        price=price,
        url=f"https://www.daft.ie/{listing_id}",
        location=location,
        bedrooms="Double Room",
        image_url=None,
        search_name=search_name,
        first_seen=Listing.now_iso(),
    )


class AnalyticsLoadersTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name
        self.storage = Storage(self.data_dir)

    def tearDown(self) -> None:
        self.storage.conn.close()
        self._tmp.cleanup()

    def test_load_listings_and_events(self) -> None:
        now = Listing.now_iso()
        search_id, _ = self.storage.register_search(_search(), now)
        listing = _listing("l1")
        self.storage.insert_listings([listing])
        self.storage.insert_event(ListingEvent(listing_id="l1", event_type=EVENT_NEW, timestamp=now))
        self.storage.upsert_listing_search_state([("l1", search_id, now)])

        conn = analytics.connect(self.storage.db_path)
        try:
            listings_df = analytics.load_listings(conn)
            events_df = analytics.load_events(conn)
            memberships_df = analytics.load_memberships(conn)
        finally:
            conn.close()

        self.assertEqual(len(listings_df), 1)
        self.assertAlmostEqual(float(listings_df.iloc[0]["price_monthly_eq"]), 1200.0)
        self.assertEqual(len(events_df), 1)
        self.assertEqual(events_df.iloc[0]["event_type"], EVENT_NEW)
        self.assertEqual(len(memberships_df), 1)
        self.assertEqual(memberships_df.iloc[0]["search_id"], search_id)

    def test_lifecycle_v2_started_at_roundtrip(self) -> None:
        conn = analytics.connect(self.storage.db_path)
        try:
            self.assertIsNone(analytics.get_lifecycle_v2_started_at(conn))
        finally:
            conn.close()

        now = Listing.now_iso()
        self.storage.ensure_lifecycle_v2_started(now)

        conn = analytics.connect(self.storage.db_path)
        try:
            started_at = analytics.get_lifecycle_v2_started_at(conn)
        finally:
            conn.close()
        self.assertIsNotNone(started_at)


class TrustedLifecycleEventsTests(unittest.TestCase):
    def test_drops_removed_before_v2_but_keeps_new(self) -> None:
        v2_started_at = pd.Timestamp("2026-01-15T00:00:00Z")
        events_df = pd.DataFrame(
            [
                {"event_type": EVENT_REMOVED, "timestamp": pd.Timestamp("2026-01-01T00:00:00Z")},
                {"event_type": EVENT_REMOVED, "timestamp": pd.Timestamp("2026-02-01T00:00:00Z")},
                {"event_type": EVENT_NEW, "timestamp": pd.Timestamp("2026-01-01T00:00:00Z")},
            ]
        )
        result = analytics.trusted_lifecycle_events(events_df, v2_started_at)
        self.assertEqual(len(result), 2)
        self.assertNotIn(
            ("2026-01-01T00:00:00Z", EVENT_REMOVED),
            list(zip(result["timestamp"].astype(str), result["event_type"])),
        )

    def test_none_epoch_keeps_everything(self) -> None:
        events_df = pd.DataFrame(
            [{"event_type": EVENT_REMOVED, "timestamp": pd.Timestamp("2020-01-01T00:00:00Z")}]
        )
        result = analytics.trusted_lifecycle_events(events_df, None)
        self.assertEqual(len(result), 1)


class SegmentTests(unittest.TestCase):
    def test_ensuite_segment_filters_by_title_location_price(self) -> None:
        df = analytics._with_analysis_price(
            pd.DataFrame(
                [
                    {
                        "title": "Ensuite Double Room",
                        "location": "Dublin City",
                        "bedrooms": "Double Room",
                        "room_type": "double",
                        "facilities": '["ensuite"]',
                        "price_monthly_eq": 1300,
                        "price_period": "month",
                        "price_value": 1300,
                    },
                    {
                        "title": "Standard Double Room",
                        "location": "Dublin City",
                        "bedrooms": "Double Room",
                        "room_type": "double",
                        "price_monthly_eq": 1300,
                        "price_period": "month",
                        "price_value": 1300,
                    },
                    {
                        "title": "Ensuite Double Room",
                        "location": "Dublin City",
                        "bedrooms": "Double Room",
                        "room_type": "double",
                        "facilities": '["ensuite"]',
                        "price_monthly_eq": 1600,
                        "price_period": "month",
                        "price_value": 1600,
                    },
                    {
                        "title": "Ensuite Double Room",
                        "location": "Cork",
                        "bedrooms": "Double Room",
                        "room_type": "double",
                        "facilities": '["ensuite"]',
                        "price_monthly_eq": 1200,
                        "price_period": "month",
                        "price_value": 1200,
                    },
                ]
            )
        )
        matched = analytics.apply_segment(df, "dublin_sharing_ensuite_1400")
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched.iloc[0]["analysis_price"], 1300)

    def test_all_segment_passes_everything(self) -> None:
        df = pd.DataFrame([{"title": "x", "location": "y", "price_monthly_eq": 1}])
        self.assertEqual(len(analytics.apply_segment(df, "all")), 1)

    def test_unknown_segment_raises(self) -> None:
        df = pd.DataFrame([{"title": "x"}])
        with self.assertRaises(KeyError):
            analytics.apply_segment(df, "does-not-exist")


class PriceSummaryTests(unittest.TestCase):
    def test_summary_stats(self) -> None:
        values = pd.Series([1000, 1200, 1400, None, 1600])
        summary = analytics.price_summary(values)
        self.assertEqual(summary["count"], 4)
        self.assertEqual(summary["min"], 1000)
        self.assertEqual(summary["max"], 1600)
        self.assertEqual(summary["median"], 1300)

    def test_empty_returns_empty_dict(self) -> None:
        self.assertEqual(analytics.price_summary(pd.Series([None, None])), {})


class BinnedMediansTests(unittest.TestCase):
    def test_bins_by_distance(self) -> None:
        x = pd.Series([0.5, 1.5, 2.5, 3.5])
        y = pd.Series([100, 200, 300, 400])
        centers, medians = analytics.binned_medians(x, y, bin_size=2.0)
        self.assertEqual(centers, [1.0, 3.0])
        self.assertEqual(medians, [150.0, 350.0])

    def test_empty_input(self) -> None:
        centers, medians = analytics.binned_medians(pd.Series([]), pd.Series([]), bin_size=2.0)
        self.assertEqual(centers, [])
        self.assertEqual(medians, [])


class WeeklyVelocityTests(unittest.TestCase):
    def test_counts_supply_added_and_removed(self) -> None:
        events_df = pd.DataFrame(
            [
                {"event_type": EVENT_NEW, "timestamp": pd.Timestamp("2026-01-05T00:00:00Z")},
                {"event_type": EVENT_RELISTED, "timestamp": pd.Timestamp("2026-01-06T00:00:00Z")},
                {"event_type": EVENT_SEED, "timestamp": pd.Timestamp("2026-01-06T12:00:00Z")},
                {"event_type": EVENT_REMOVED, "timestamp": pd.Timestamp("2026-01-07T00:00:00Z")},
            ]
        )
        velocity = analytics.weekly_velocity(events_df)
        self.assertEqual(len(velocity), 1)
        row = velocity.iloc[0]
        self.assertEqual(row["supply_added"], 2)
        self.assertEqual(row["supply_removed"], 1)
        self.assertEqual(row["net_supply_change"], 1)
        self.assertEqual(row["baseline_seed"], 1)

    def test_empty_events(self) -> None:
        velocity = analytics.weekly_velocity(pd.DataFrame(columns=["event_type", "timestamp"]))
        self.assertTrue(velocity.empty)


class PostingTimePatternsTests(unittest.TestCase):
    def test_hours_and_days(self) -> None:
        events_df = pd.DataFrame(
            [
                {"event_type": EVENT_NEW, "timestamp": pd.Timestamp("2026-01-05T09:00:00Z")},  # Monday
                {"event_type": EVENT_RELISTED, "timestamp": pd.Timestamp("2026-01-05T09:30:00Z")},
                {"event_type": EVENT_REMOVED, "timestamp": pd.Timestamp("2026-01-06T10:00:00Z")},
            ]
        )
        hours, days = analytics.posting_time_patterns(events_df)
        self.assertEqual(hours.get(9), 2)
        self.assertEqual(days.get("Monday"), 2)


class TimeOnMarketTests(unittest.TestCase):
    def test_censors_actives_and_drops_pre_v2_removals(self) -> None:
        now = pd.Timestamp("2026-02-01T00:00:00Z")
        analytics_v2_started_at = pd.Timestamp("2026-01-15T00:00:00Z")
        episodes_df = pd.DataFrame(
            [
                {
                    "search_id": "s1", "is_active": 0,
                    "started_at": pd.Timestamp("2026-01-20T00:00:00Z"),
                    "ended_at": pd.Timestamp("2026-02-01T00:00:00Z"),
                },
                {
                    "search_id": "s1", "is_active": 1,
                    "started_at": pd.Timestamp("2026-01-16T00:00:00Z"),
                    "ended_at": pd.NaT,
                },
                {
                    "search_id": "s1", "is_active": 0,
                    "started_at": pd.Timestamp("2026-01-01T00:00:00Z"),
                    "ended_at": pd.Timestamp("2026-01-05T00:00:00Z"),
                },
            ]
        )
        result = analytics.time_on_market_frame(
            episodes_df, analytics_v2_started_at=analytics_v2_started_at, now=now
        )
        self.assertEqual(len(result), 2)
        observed_row = result[result["observed"]].iloc[0]
        self.assertAlmostEqual(observed_row["duration_days"], 12.0)
        censored_row = result[~result["observed"]].iloc[0]
        self.assertAlmostEqual(censored_row["duration_days"], 16.0)

    def test_filters_by_search_ids(self) -> None:
        now = pd.Timestamp("2026-02-01T00:00:00Z")
        episodes_df = pd.DataFrame(
            [
                {"search_id": "s1", "is_active": 1, "started_at": now, "ended_at": pd.NaT},
                {"search_id": "s2", "is_active": 1, "started_at": now, "ended_at": pd.NaT},
            ]
        )
        result = analytics.time_on_market_frame(
            episodes_df, analytics_v2_started_at=None, now=now, search_ids={"s1"}
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["search_id"], "s1")


class KaplanMeierTests(unittest.TestCase):
    def test_all_observed_reaches_zero(self) -> None:
        times, survival = analytics.kaplan_meier([1.0, 2.0, 3.0], [True, True, True])
        self.assertEqual(survival[0], 1.0)
        self.assertAlmostEqual(survival[-1], 0.0)
        self.assertEqual(times[0], 0.0)

    def test_censoring_keeps_survival_above_zero(self) -> None:
        times, survival = analytics.kaplan_meier([5.0, 10.0], [True, False])
        # One death out of two at risk, then censoring only -> survival floors at 0.5.
        self.assertAlmostEqual(survival[-1], 0.5)

    def test_empty_input(self) -> None:
        times, survival = analytics.kaplan_meier([], [])
        self.assertEqual(times, [0.0])
        self.assertEqual(survival, [1.0])


if __name__ == "__main__":
    unittest.main()
