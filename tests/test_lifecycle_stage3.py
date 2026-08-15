"""Stage 3: completeness, grace removals, orphans, shallow-vs-deep behaviour.

Run with:
    python -m unittest tests.test_lifecycle_stage3
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from daft_monitor.config import AppConfig, SearchConfig
from daft_monitor.constants import EVENT_CONFIG_RETIRED, EVENT_REMOVED
from daft_monitor.lifecycle_v2 import (
    assess_deep_scan_complete,
    membership_eligible_for_removal,
)
from daft_monitor.main import _pick_deep_scan_search, _process_lifecycle
from daft_monitor.models import Listing
from daft_monitor.search_identity import resolved_search_id
from daft_monitor.searcher import SearchRunResult
from daft_monitor.storage import Storage
from daft_monitor.wide_event import WideEvent


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _search(**overrides: object) -> SearchConfig:
    base: dict[str, object] = {
        "name": "Obs Search",
        "search_type": "SHARING",
        "location": "Dublin",
        "notify": False,
        "deep_scan": True,
        "shallow_pages": 2,
        "id": "obs",
    }
    base.update(overrides)
    return SearchConfig(**base)  # type: ignore[arg-type]


def _listing(
    listing_id: str,
    *,
    search_name: str = "Obs Search",
    first_seen: str | None = None,
) -> Listing:
    ts = first_seen or Listing.now_iso()
    return Listing(
        id=listing_id,
        title=f"Listing {listing_id}",
        price="€1,000 per month",
        url=f"https://www.daft.ie/{listing_id}",
        location="Dublin",
        bedrooms="Double Room",
        image_url=None,
        search_name=search_name,
        first_seen=ts,
        last_seen=ts,
        last_price="€1,000 per month",
    )


def _app_config(searches: list[SearchConfig], **overrides: object) -> AppConfig:
    base: dict[str, object] = {
        "check_interval_minutes": 30,
        "data_dir": "./data",
        "distance_to_location": False,
        "location_name": "City Centre",
        "location_latitude": None,
        "location_longitude": None,
        "searches": searches,
        "notifiers": [],
        "removal_grace_hours": 48,
        "deep_scan_min_interval_hours": 24,
    }
    base.update(overrides)
    return AppConfig(**base)  # type: ignore[arg-type]


class CompletenessTests(unittest.TestCase):
    def test_complete_when_short_terminal_page(self) -> None:
        self.assertTrue(
            assess_deep_scan_complete(
                pages_fetched=3,
                last_page_size=10,
                page_errors=0,
                results_count=110,
                previous_complete_count=100,
                hit_max_pages_cap=False,
            )
        )

    def test_incomplete_on_page_errors(self) -> None:
        self.assertFalse(
            assess_deep_scan_complete(
                pages_fetched=2,
                last_page_size=10,
                page_errors=1,
                results_count=60,
                previous_complete_count=None,
                hit_max_pages_cap=False,
            )
        )

    def test_incomplete_when_capped_on_full_page(self) -> None:
        self.assertFalse(
            assess_deep_scan_complete(
                pages_fetched=40,
                last_page_size=50,
                page_errors=0,
                results_count=2000,
                previous_complete_count=1500,
                hit_max_pages_cap=True,
            )
        )

    def test_incomplete_anomaly_under_half_baseline(self) -> None:
        self.assertFalse(
            assess_deep_scan_complete(
                pages_fetched=1,
                last_page_size=20,
                page_errors=0,
                results_count=20,
                previous_complete_count=100,
                hit_max_pages_cap=False,
            )
        )

    def test_first_deep_scan_has_no_baseline(self) -> None:
        self.assertTrue(
            assess_deep_scan_complete(
                pages_fetched=1,
                last_page_size=5,
                page_errors=0,
                results_count=5,
                previous_complete_count=None,
                hit_max_pages_cap=False,
            )
        )


class GraceEligibilityTests(unittest.TestCase):
    def test_not_eligible_if_seen_after_deep_scan(self) -> None:
        deep = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        seen = deep + timedelta(hours=1)
        now = deep + timedelta(hours=100)
        self.assertFalse(
            membership_eligible_for_removal(
                membership_last_seen=_iso(seen),
                deep_scan_finished_at=_iso(deep),
                now_iso=_iso(now),
                grace_hours=48,
            )
        )

    def test_not_eligible_inside_grace(self) -> None:
        seen = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
        deep = seen + timedelta(hours=2)
        now = seen + timedelta(hours=24)
        self.assertFalse(
            membership_eligible_for_removal(
                membership_last_seen=_iso(seen),
                deep_scan_finished_at=_iso(deep),
                now_iso=_iso(now),
                grace_hours=48,
            )
        )

    def test_eligible_after_grace_and_complete_deep(self) -> None:
        seen = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
        deep = seen + timedelta(hours=2)
        now = seen + timedelta(hours=49)
        self.assertTrue(
            membership_eligible_for_removal(
                membership_last_seen=_iso(seen),
                deep_scan_finished_at=_iso(deep),
                now_iso=_iso(now),
                grace_hours=48,
            )
        )


class StorageLifecycleHelpersTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(self._tmp.name)
        self.search = _search()
        self.storage.register_search(self.search, Listing.now_iso())

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def test_orphan_deleted_search_membership_is_removable(self) -> None:
        now = Listing.now_iso()
        listing = _listing("L1")
        self.storage.insert_listings([listing])
        # Membership only under a search that is no longer configured.
        self.storage.conn.execute(
            """
            INSERT INTO searches (search_id, name, criteria_fingerprint, first_registered)
            VALUES ('deleted-old', 'Old', 'abc', ?)
            """,
            (now,),
        )
        self.storage.upsert_listing_search_state([("L1", "deleted-old", now)])
        configured = {resolved_search_id(self.search)}
        orphans = self.storage.listing_ids_orphaned_from_configured_searches({"L1"}, configured)
        self.assertEqual(orphans, {"L1"})

    def test_unmigrated_listing_not_globally_removed(self) -> None:
        listing = _listing("L2")
        self.storage.insert_listings([listing])
        configured = {resolved_search_id(self.search)}
        removable = self.storage.listing_ids_inactive_in_all_configured_searches(
            {"L2"}, configured
        )
        self.assertEqual(removable, set())

    def test_inactive_in_all_configured_is_removable(self) -> None:
        now = Listing.now_iso()
        listing = _listing("L3")
        self.storage.insert_listings([listing])
        sid = resolved_search_id(self.search)
        self.storage.upsert_listing_search_state([("L3", sid, now)])
        self.storage.mark_memberships_inactive(sid, {"L3"}, now)
        removable = self.storage.listing_ids_inactive_in_all_configured_searches(
            {"L3"}, {sid}
        )
        self.assertEqual(removable, {"L3"})


class ProcessLifecycleIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(self._tmp.name)

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def test_shallow_seen_listing_not_removed_for_deep_scan_search(self) -> None:
        """Missing from shallow must NOT remove deep_scan=true memberships."""
        search = _search(deep_scan=True)
        config = _app_config([search])
        now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        earlier = _iso(now - timedelta(days=3))
        sid = resolved_search_id(search)
        self.storage.register_search(search, earlier)

        kept = _listing("KEEP", first_seen=earlier)
        missing = _listing("MISS", first_seen=earlier)
        self.storage.insert_listings([kept, missing])
        self.storage.upsert_listing_search_state(
            [(kept.id, sid, earlier), (missing.id, sid, earlier)]
        )
        # Complete deep that finished after last_seen — but grace not applied
        # because shallow path must not inactivate; only grace path does, and
        # we simulate a fresh shallow where MISS is absent but KEEP is present.
        # Without a complete deep, nothing should be removed.
        event = WideEvent(cycle_id="t", is_seed_run=False, check_interval_minutes=30, environment="dev")
        run = SearchRunResult(
            listings=[kept],
            search_name=search.name,
            pages_fetched=1,
            results_count=1,
            complete=False,
            is_deep=False,
            error=None,
        )
        _process_lifecycle(
            self.storage,
            config,
            [kept],
            [run],
            {search.name: search},
            event,
        )
        active = self.storage.get_active_listing_ids()
        self.assertIn("KEEP", active)
        self.assertIn("MISS", active)
        memberships = self.storage.get_active_memberships(sid)
        self.assertEqual({m[0] for m in memberships}, {"KEEP", "MISS"})

    def test_grace_removal_after_complete_deep(self) -> None:
        search = _search(deep_scan=True)
        config = _app_config([search], removal_grace_hours=48)
        sid = resolved_search_id(search)
        seen_at = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
        deep_at = seen_at + timedelta(hours=3)
        # Freeze "now" inside process by using membership last_seen + evaluating
        # with real now — instead insert a complete deep run and set last_seen
        # far enough in the past relative to wall clock.
        wall_now = datetime.now(timezone.utc)
        last_seen = wall_now - timedelta(hours=60)
        deep_finished = last_seen + timedelta(hours=2)

        self.storage.register_search(search, _iso(last_seen))
        gone = _listing("GONE", first_seen=_iso(last_seen))
        self.storage.insert_listings([gone])
        self.storage.upsert_listing_search_state([(gone.id, sid, _iso(last_seen))])
        # Ensure membership last_seen is the old timestamp (upsert used last_seen).
        self.storage.conn.execute(
            "UPDATE listing_search_state SET last_seen = ? WHERE listing_id = ?",
            (_iso(last_seen), gone.id),
        )
        self.storage.conn.commit()
        self.storage.insert_search_run(
            search_id=sid,
            started_at=_iso(deep_finished),
            finished_at=_iso(deep_finished),
            pages_fetched=2,
            results_count=50,
            complete=True,
            is_deep=True,
            criteria_fingerprint=self.storage.get_search_fingerprint(sid),
        )

        event = WideEvent(cycle_id="t", is_seed_run=False, check_interval_minutes=30, environment="dev")
        run = SearchRunResult(
            listings=[],
            search_name=search.name,
            pages_fetched=1,
            results_count=0,
            complete=False,
            is_deep=False,
            error=None,
        )
        # First pass: authoritative absence starts the missing_since clock.
        _process_lifecycle(self.storage, config, [], [run], {search.name: search}, event)
        self.assertIn("GONE", self.storage.get_active_listing_ids())
        # Same run reused after grace: still no removal without a newer complete run.
        _process_lifecycle(self.storage, config, [], [run], {search.name: search}, event)
        self.assertIn("GONE", self.storage.get_active_listing_ids())

        newer_finished = deep_finished + timedelta(hours=49)
        newer_run_id = self.storage.insert_search_run(
            search_id=sid,
            started_at=_iso(newer_finished),
            finished_at=_iso(newer_finished),
            pages_fetched=2,
            results_count=50,
            complete=True,
            is_deep=True,
            criteria_fingerprint=self.storage.get_search_fingerprint(sid),
        )
        self.storage.insert_search_run_listings(newer_run_id, [])
        # New complete run confirms still missing after grace → remove.
        _process_lifecycle(self.storage, config, [], [run], {search.name: search}, event)

        self.assertNotIn("GONE", self.storage.get_active_listing_ids())
        events = self.storage.conn.execute(
            "SELECT event_type FROM listing_events WHERE listing_id = ?",
            ("GONE",),
        ).fetchall()
        self.assertIn(EVENT_REMOVED, {str(r[0]) for r in events})

    def test_legacy_missing_removes_for_deep_scan_false(self) -> None:
        search = _search(name="Legacy", id="legacy", deep_scan=False, notify=True)
        config = _app_config([search])
        sid = resolved_search_id(search)
        now = Listing.now_iso()
        self.storage.register_search(search, now)
        kept = _listing("K", search_name=search.name)
        miss = _listing("M", search_name=search.name)
        self.storage.insert_listings([kept, miss])
        self.storage.upsert_listing_search_state(
            [(kept.id, sid, now), (miss.id, sid, now)]
        )

        event = WideEvent(cycle_id="t", is_seed_run=False, check_interval_minutes=5, environment="dev")
        run = SearchRunResult(
            listings=[kept],
            search_name=search.name,
            pages_fetched=1,
            results_count=1,
            complete=False,
            is_deep=False,
            error=None,
        )
        _process_lifecycle(
            self.storage, config, [kept], [run], {search.name: search}, event
        )
        active = self.storage.get_active_listing_ids()
        self.assertIn("K", active)
        self.assertNotIn("M", active)

    def test_overlap_stays_active_if_any_configured_membership_active(self) -> None:
        a = _search(name="A", id="a", deep_scan=False)
        b = _search(name="B", id="b", deep_scan=False)
        config = _app_config([a, b])
        now = Listing.now_iso()
        self.storage.register_search(a, now)
        self.storage.register_search(b, now)
        listing = _listing("X", search_name=a.name)
        self.storage.insert_listings([listing])
        self.storage.upsert_listing_search_state(
            [("X", "a", now), ("X", "b", now)]
        )
        # Search A succeeds without X; B succeeds with X → still active via B.
        event = WideEvent(cycle_id="t", is_seed_run=False, check_interval_minutes=5, environment="dev")
        runs = [
            SearchRunResult(
                listings=[],
                search_name=a.name,
                pages_fetched=1,
                results_count=0,
                error=None,
            ),
            SearchRunResult(
                listings=[listing],
                search_name=b.name,
                pages_fetched=1,
                results_count=1,
                error=None,
            ),
        ]
        _process_lifecycle(
            self.storage,
            config,
            [listing],
            runs,
            {a.name: a, b.name: b},
            event,
        )
        self.assertIn("X", self.storage.get_active_listing_ids())
        active_a = {lid for lid, _ in self.storage.get_active_memberships("a")}
        active_b = {lid for lid, _ in self.storage.get_active_memberships("b")}
        self.assertNotIn("X", active_a)
        self.assertIn("X", active_b)

    def test_retired_search_memberships_do_not_create_removed_events(self) -> None:
        old = _search(name="Old Search", id="old-search", deep_scan=False)
        new = _search(name="New Search", id="new-search", deep_scan=False)
        old_id = resolved_search_id(old)
        now = Listing.now_iso()
        self.storage.register_search(old, now)
        listing = _listing("OLD", search_name=old.name)
        self.storage.insert_listings([listing])
        self.storage.upsert_listing_search_state([(listing.id, old_id, now)])

        # Moving to a new config should retire old memberships, not mark
        # listing as removed from market.
        from daft_monitor.main import _register_searches

        event = WideEvent(cycle_id="retire", is_seed_run=False, check_interval_minutes=5, environment="dev")
        _register_searches(self.storage, [new], now, event)

        removed_events = self.storage.conn.execute(
            "SELECT COUNT(*) FROM listing_events WHERE listing_id = ? AND event_type = ?",
            ("OLD", EVENT_REMOVED),
        ).fetchone()
        self.assertEqual(int(removed_events[0]), 0)

        retired_events = self.storage.conn.execute(
            "SELECT COUNT(*) FROM listing_search_events WHERE listing_id = ? AND event_type = ?",
            ("OLD", EVENT_CONFIG_RETIRED),
        ).fetchone()
        self.assertEqual(int(retired_events[0]), 1)

        listing_row = self.storage.conn.execute(
            "SELECT is_active FROM listings WHERE id = ?",
            ("OLD",),
        ).fetchone()
        self.assertEqual(int(listing_row[0]), 1)


class DeepScanPickerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(self._tmp.name)

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def test_round_robin_skips_recent_complete(self) -> None:
        s1 = _search(name="One", id="one", deep_scan=True)
        s2 = _search(name="Two", id="two", deep_scan=True)
        config = _app_config([s1, s2], deep_scan_min_interval_hours=24)
        now_dt = datetime.now(timezone.utc)
        now = _iso(now_dt)
        self.storage.register_search(s1, now)
        self.storage.register_search(s2, now)
        # Freshly registered searches are seeding; clear pending state so they
        # are eligible for the deep-scan rotation (seeding goes to baselines).
        for sid in ("one", "two"):
            fingerprint = self.storage.get_search_fingerprint(sid)
            assert fingerprint is not None
            self.storage.clear_pending_seed(sid, fingerprint)
        self.storage.insert_search_run(
            search_id="one",
            started_at=_iso(now_dt - timedelta(hours=1)),
            finished_at=_iso(now_dt - timedelta(hours=1)),
            pages_fetched=1,
            results_count=10,
            complete=True,
            is_deep=True,
            criteria_fingerprint=self.storage.get_search_fingerprint("one"),
        )
        chosen = _pick_deep_scan_search(self.storage, [s1, s2], config, now)
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(resolved_search_id(chosen), "two")

    def test_none_when_no_deep_scan_searches(self) -> None:
        s = _search(deep_scan=False)
        config = _app_config([s])
        chosen = _pick_deep_scan_search(self.storage, [s], config, Listing.now_iso())
        self.assertIsNone(chosen)


if __name__ == "__main__":
    unittest.main()
