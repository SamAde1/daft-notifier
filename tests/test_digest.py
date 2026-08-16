"""Unit tests for daft_monitor.digest."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daft_monitor.config import SearchConfig
from daft_monitor.constants import (
    EVENT_NEW,
    EVENT_REMOVED,
)
from daft_monitor.digest import build_digest, digest_is_due, extract_county, parse_digest_day
from daft_monitor.models import Listing
from daft_monitor.storage import Storage


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _search(**overrides: object) -> SearchConfig:
    base: dict[str, object] = dict(
        name="Dublin Sharing",
        search_type="SHARING",
        location=["Dublin", "Dublin City"],
        deep_scan=True,
    )
    base.update(overrides)
    return SearchConfig(**base)  # type: ignore[arg-type]


def _listing(listing_id: str, price: str = "€1,200 per month", location: str = "Dublin City") -> Listing:
    return Listing(
        id=listing_id,
        title=f"Ensuite Double Room {listing_id}",
        price=price,
        url=f"https://www.daft.ie/{listing_id}",
        location=location,
        bedrooms="Double Room",
        image_url=None,
        search_name="Dublin Sharing",
        first_seen=Listing.now_iso(),
    )


def _membership_event(
    storage: Storage,
    *,
    listing_id: str,
    search_id: str,
    event_type: str,
    timestamp: str,
    listing: Listing,
    old_value: str | None = None,
    new_value: str | None = None,
) -> None:
    storage._insert_membership_event(
        listing_id=listing_id,
        search_id=search_id,
        event_type=event_type,
        timestamp=timestamp,
        listing=listing,
        old_value=old_value,
        new_value=new_value,
    )
    storage.conn.commit()


class ParseDigestDayTests(unittest.TestCase):
    def test_names(self) -> None:
        self.assertEqual(parse_digest_day("sunday"), 6)
        self.assertEqual(parse_digest_day("sun"), 6)
        self.assertEqual(parse_digest_day("Monday"), 0)
        self.assertEqual(parse_digest_day("tue"), 1)

    def test_int_passthrough(self) -> None:
        self.assertEqual(parse_digest_day(0), 0)
        self.assertEqual(parse_digest_day(6), 6)

    def test_none_disables(self) -> None:
        self.assertIsNone(parse_digest_day(None))

    def test_invalid_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_digest_day("funday")
        with self.assertRaises(ValueError):
            parse_digest_day(7)


class DigestIsDueTests(unittest.TestCase):
    # Sunday 2026-08-09 is a real Sunday.
    SUNDAY_10AM = datetime(2026, 8, 9, 10, 0, tzinfo=timezone.utc)
    SUNDAY_8AM = datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc)
    MONDAY_10AM = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
    ANALYTICS_STARTED = _iso(datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc))

    def test_due_on_digest_day_after_hour_never_sent(self) -> None:
        self.assertTrue(
            digest_is_due(
                now=self.SUNDAY_10AM,
                digest_day=6,
                digest_hour=9,
                last_sent_at_iso=None,
                analytics_v2_started_at_iso=self.ANALYTICS_STARTED,
            )
        )

    def test_due_before_hour_when_never_sent(self) -> None:
        # Catch-up: never delivered, and last week's slot was missed too.
        self.assertTrue(
            digest_is_due(
                now=self.SUNDAY_8AM,
                digest_day=6,
                digest_hour=9,
                last_sent_at_iso=None,
                analytics_v2_started_at_iso=self.ANALYTICS_STARTED,
            )
        )

    def test_not_due_without_full_week_of_analytics(self) -> None:
        recent = _iso(self.SUNDAY_10AM - timedelta(days=3))
        self.assertFalse(
            digest_is_due(
                now=self.SUNDAY_10AM,
                digest_day=6,
                digest_hour=9,
                last_sent_at_iso=None,
                analytics_v2_started_at_iso=recent,
            )
        )

    def test_catchup_monday_after_missed_sunday(self) -> None:
        # App was down through Sunday 9am; last send was the previous week.
        last_sent = _iso(datetime(2026, 8, 2, 9, 5, tzinfo=timezone.utc))
        self.assertTrue(digest_is_due(now=self.MONDAY_10AM, digest_day=6, digest_hour=9, last_sent_at_iso=last_sent))

    def test_not_due_monday_after_sunday_send(self) -> None:
        last_sent = _iso(datetime(2026, 8, 9, 9, 5, tzinfo=timezone.utc))
        self.assertFalse(digest_is_due(now=self.MONDAY_10AM, digest_day=6, digest_hour=9, last_sent_at_iso=last_sent))

    def test_not_due_already_sent_today(self) -> None:
        last_sent = _iso(self.SUNDAY_10AM - timedelta(hours=1))  # sent at 9am same day
        self.assertFalse(digest_is_due(now=self.SUNDAY_10AM, digest_day=6, digest_hour=9, last_sent_at_iso=last_sent))

    def test_due_again_next_week(self) -> None:
        last_sent = _iso(self.SUNDAY_10AM - timedelta(days=7))
        self.assertTrue(digest_is_due(now=self.SUNDAY_10AM, digest_day=6, digest_hour=9, last_sent_at_iso=last_sent))


class BuildDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name
        self.storage = Storage(self.data_dir)
        # Storage init stamps the analytics epoch at creation time; push it
        # back so synthetic historical events are inside the trusted window.
        epoch = _iso(datetime.now(timezone.utc) - timedelta(days=90))
        self.storage.set_meta("analytics_v2_started_at", epoch)

    def tearDown(self) -> None:
        self.storage.conn.close()
        self._tmp.cleanup()

    def test_returns_none_when_db_missing(self) -> None:
        shutil_data_dir = str(Path(self.data_dir).parent / "no-such-dir")
        self.assertIsNone(build_digest(data_dir=shutil_data_dir))

    def test_counts_window_events_and_prices(self) -> None:
        now = datetime.now(timezone.utc)
        search_id, _ = self.storage.register_search(_search(), _iso(now))

        l1 = _listing("l1", "€1,200 per month")
        l2 = _listing("l2", "€1,400 per month")
        self.storage.insert_listings([l1, l2])
        self.storage.upsert_listing_search_state([("l1", search_id, _iso(now)), ("l2", search_id, _iso(now))])
        _membership_event(
            self.storage,
            listing_id="l1",
            search_id=search_id,
            event_type=EVENT_NEW,
            timestamp=_iso(now - timedelta(days=1)),
            listing=l1,
        )

        result = build_digest(data_dir=self.data_dir, window_days=7)
        assert result is not None
        self.assertEqual(result.new_count, 1)
        self.assertTrue(result.had_active_listings)
        self.assertIn("median=EUR1,300", result.text)
        self.assertIn("Dublin Sharing", result.text)

    def test_excludes_removed_before_lifecycle_v2(self) -> None:
        now = datetime.now(timezone.utc)
        search_id, _ = self.storage.register_search(_search(), _iso(now))
        listing = _listing("l1")
        self.storage.insert_listings([listing])
        self.storage.upsert_listing_search_state([("l1", search_id, _iso(now))])

        v2_start = now - timedelta(days=10)
        self.storage.ensure_lifecycle_v2_started(_iso(v2_start))

        _membership_event(
            self.storage,
            listing_id="l1",
            search_id=search_id,
            event_type=EVENT_REMOVED,
            timestamp=_iso(now - timedelta(days=20)),
            listing=listing,
        )
        _membership_event(
            self.storage,
            listing_id="l1",
            search_id=search_id,
            event_type=EVENT_REMOVED,
            timestamp=_iso(now - timedelta(days=1)),
            listing=listing,
        )

        result = build_digest(data_dir=self.data_dir, window_days=30)
        assert result is not None
        self.assertEqual(result.removed_count, 1)
        self.assertIn("pre-lifecycle-v2 removal/relist events excluded", result.text)

    def test_tightness_direction_loosening(self) -> None:
        now = datetime.now(timezone.utc)
        search_id, _ = self.storage.register_search(_search(), _iso(now))
        l1 = _listing("l1")
        new_listings = [_listing(f"n{i}") for i in range(3)]
        self.storage.insert_listings([l1] + new_listings)
        self.storage.upsert_listing_search_state(
            [(l1.id, search_id, _iso(now))] + [(listing.id, search_id, _iso(now)) for listing in new_listings]
        )
        for listing in new_listings:
            _membership_event(
                self.storage,
                listing_id=listing.id,
                search_id=search_id,
                event_type=EVENT_NEW,
                timestamp=_iso(now - timedelta(days=1)),
                listing=listing,
            )
        _membership_event(
            self.storage,
            listing_id="l1",
            search_id=search_id,
            event_type=EVENT_REMOVED,
            timestamp=_iso(now - timedelta(days=1)),
            listing=l1,
        )
        result = build_digest(data_dir=self.data_dir, window_days=7)
        assert result is not None
        self.assertIn("loosening (+2 net new listings this window)", result.text)

    def test_biggest_price_cuts_included(self) -> None:
        now = datetime.now(timezone.utc)
        search_id, _ = self.storage.register_search(_search(), _iso(now))
        listing = _listing("l1")
        self.storage.insert_listings([listing])
        self.storage.upsert_listing_search_state([("l1", search_id, _iso(now))])
        self.storage.record_membership_price_changes(
            listing,
            old_price_raw="€1,400 per month",
            new_price_raw="€1,200 per month",
            timestamp=_iso(now - timedelta(days=1)),
        )
        result = build_digest(data_dir=self.data_dir, window_days=7)
        assert result is not None
        self.assertEqual(result.price_change_count, 1)
        self.assertIn("BIGGEST PRICE CUTS", result.text)
        self.assertIn("EUR1,400 -> EUR1,200", result.text)

    def test_price_increases_counted_but_not_listed_as_cuts(self) -> None:
        now = datetime.now(timezone.utc)
        search_id, _ = self.storage.register_search(_search(), _iso(now))
        listing = _listing("l1")
        self.storage.insert_listings([listing])
        self.storage.upsert_listing_search_state([("l1", search_id, _iso(now))])
        self.storage.record_membership_price_changes(
            listing,
            old_price_raw="€1,200 per month",
            new_price_raw="€1,400 per month",
            timestamp=_iso(now - timedelta(days=1)),
        )
        result = build_digest(data_dir=self.data_dir, window_days=7)
        assert result is not None
        self.assertEqual(result.price_change_count, 1)
        self.assertNotIn("BIGGEST PRICE CUTS", result.text)

    def test_price_change_deduped_across_multi_search_memberships(self) -> None:
        now = datetime.now(timezone.utc)
        search_a, _ = self.storage.register_search(_search(), _iso(now))
        search_b, _ = self.storage.register_search(
            _search(name="Dublin Rent", id="dublin-rent", search_type="RESIDENTIAL_RENT"),
            _iso(now),
        )
        listing = _listing("l1")
        self.storage.insert_listings([listing])
        self.storage.upsert_listing_search_state([("l1", search_a, _iso(now)), ("l1", search_b, _iso(now))])
        self.storage.record_membership_price_changes(
            listing,
            old_price_raw="€1,400 per month",
            new_price_raw="€1,200 per month",
            timestamp=_iso(now - timedelta(days=1)),
        )
        result = build_digest(data_dir=self.data_dir, window_days=7)
        assert result is not None
        self.assertEqual(result.price_change_count, 1)


class ExtractCountyTests(unittest.TestCase):
    def test_matches_county(self) -> None:
        self.assertEqual(extract_county("Ranelagh, Dublin 6"), "Dublin")
        self.assertEqual(extract_county("Naas, Kildare"), "Kildare")

    def test_falls_back_to_other(self) -> None:
        self.assertEqual(extract_county("Nowheresville"), "Other")


if __name__ == "__main__":
    unittest.main()
