"""Regression tests for the observation-mode hardening pass.

Covers: durable baseline seeding for non-deep searches, transactional
membership observations, removal snapshots loaded from the DB, episode
deduplication, missing_since grace flow, grouped-listing expansion failures,
run-kind tagging, and row-wise segment fallbacks.

Run with:
    python -m unittest tests.test_observation_hardening
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import pandas as pd

from daft_monitor.analytics import apply_segment
from daft_monitor.config import AppConfig, NotifierConfig, SearchConfig
from daft_monitor.constants import EVENT_NEW, EVENT_REMOVED, EVENT_SEED
from daft_monitor.listing_expand import expand_grouped_listings
from daft_monitor.main import _deep_scan_cooldown_key, _pick_baseline_search, _pick_deep_scan_search
from daft_monitor.models import Listing
from daft_monitor.notifiers.ntfy import NtfyNotifier
from daft_monitor.search_identity import resolved_search_id
from daft_monitor.storage import Storage


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


def _listing(listing_id: str, **overrides: object) -> Listing:
    base: dict[str, object] = {
        "id": listing_id,
        "title": f"Listing {listing_id}",
        "price": "€1,000 per month",
        "url": f"https://www.daft.ie/{listing_id}",
        "location": "Dublin",
        "bedrooms": "Double Room",
        "image_url": None,
        "search_name": "Obs Search",
        "first_seen": Listing.now_iso(),
    }
    base.update(overrides)
    return Listing(**base)  # type: ignore[arg-type]


class NotifierNameTests(unittest.TestCase):
    def test_ntfy_name_comes_from_config(self) -> None:
        cfg = NotifierConfig(
            name="ntfy-prod-digest",
            type="ntfy",
            role="digest",
            environments=["prod"],
            enabled=True,
            topic="some-topic",
        )
        notifier = NtfyNotifier(cfg)
        self.assertEqual(notifier.name, "ntfy-prod-digest")


class BaselinePickerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(self._tmp.name)

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def test_pending_non_deep_search_gets_baseline(self) -> None:
        search = _search(name="Legacy", id="legacy", deep_scan=False)
        self.storage.register_search(search, Listing.now_iso())
        chosen = _pick_baseline_search(self.storage, [search], Listing.now_iso())
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(resolved_search_id(chosen), "legacy")

    def test_no_baseline_once_pending_cleared(self) -> None:
        search = _search(name="Legacy", id="legacy", deep_scan=False)
        self.storage.register_search(search, Listing.now_iso())
        fingerprint = self.storage.get_search_fingerprint("legacy")
        assert fingerprint is not None
        self.storage.clear_pending_seed("legacy", fingerprint)
        self.assertIsNone(_pick_baseline_search(self.storage, [search], Listing.now_iso()))

    def test_baseline_round_robin_rotates_between_pending(self) -> None:
        a = _search(name="A", id="a", deep_scan=False)
        b = _search(name="B", id="b", deep_scan=False)
        now = Listing.now_iso()
        self.storage.register_search(a, now)
        self.storage.register_search(b, now)
        first = _pick_baseline_search(self.storage, [a, b], Listing.now_iso())
        second = _pick_baseline_search(self.storage, [a, b], Listing.now_iso())
        assert first is not None and second is not None
        self.assertNotEqual(resolved_search_id(first), resolved_search_id(second))

    def test_deep_picker_excludes_seeding_searches(self) -> None:
        search = _search(deep_scan=True)
        config = _app_config([search])
        self.storage.register_search(search, Listing.now_iso())
        chosen = _pick_deep_scan_search(self.storage, [search], config, Listing.now_iso())
        self.assertIsNone(chosen)

    def test_baseline_picker_skips_cooldown_search(self) -> None:
        search = _search(name="Legacy", id="legacy", deep_scan=False)
        self.storage.register_search(search, Listing.now_iso())
        cooldown = _iso(datetime.now(timezone.utc) + timedelta(minutes=30))
        self.storage.set_meta(_deep_scan_cooldown_key("legacy"), cooldown)
        chosen = _pick_baseline_search(self.storage, [search], Listing.now_iso())
        self.assertIsNone(chosen)

    def test_deep_picker_skips_cooldown_search(self) -> None:
        search = _search(name="Deep", id="deep", deep_scan=True)
        config = _app_config([search], deep_scan_min_interval_hours=0)
        now = Listing.now_iso()
        self.storage.register_search(search, now)
        fingerprint = self.storage.get_search_fingerprint("deep")
        assert fingerprint is not None
        self.storage.clear_pending_seed("deep", fingerprint)
        cooldown = _iso(datetime.now(timezone.utc) + timedelta(minutes=30))
        self.storage.set_meta(_deep_scan_cooldown_key("deep"), cooldown)
        chosen = _pick_deep_scan_search(self.storage, [search], config, Listing.now_iso())
        self.assertIsNone(chosen)


class ObservationTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(self._tmp.name)
        self.search = _search()
        self.search_id, _ = self.storage.register_search(self.search, Listing.now_iso())

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def test_created_records_new_event_and_episode_in_one_call(self) -> None:
        now = Listing.now_iso()
        listing = _listing("L1")
        self.storage.insert_listings([listing])
        transitions = self.storage.apply_search_observations(
            [("L1", self.search_id, now, listing)], {self.search_id: False}
        )
        self.assertEqual([t.transition for t in transitions], ["created"])
        events = self.storage.conn.execute(
            "SELECT event_type FROM listing_search_events WHERE listing_id = 'L1'"
        ).fetchall()
        self.assertEqual({str(r[0]) for r in events}, {EVENT_NEW})
        episodes = self.storage.conn.execute(
            "SELECT is_active, title FROM listing_search_episodes WHERE listing_id = 'L1'"
        ).fetchall()
        self.assertEqual(len(episodes), 1)
        self.assertEqual(int(episodes[0][0]), 1)
        self.assertEqual(str(episodes[0][1]), listing.title)

    def test_seeding_search_records_seed_events(self) -> None:
        now = Listing.now_iso()
        listing = _listing("L2")
        self.storage.insert_listings([listing])
        self.storage.apply_search_observations([("L2", self.search_id, now, listing)], {self.search_id: True})
        events = self.storage.conn.execute(
            "SELECT event_type FROM listing_search_events WHERE listing_id = 'L2'"
        ).fetchall()
        self.assertEqual({str(r[0]) for r in events}, {EVENT_SEED})

    def test_reactivation_opens_exactly_one_new_episode(self) -> None:
        now = Listing.now_iso()
        listing = _listing("L3")
        self.storage.insert_listings([listing])
        self.storage.apply_search_observations([("L3", self.search_id, now, listing)], {self.search_id: False})
        self.storage.mark_memberships_inactive(self.search_id, {"L3"}, now)
        later = _iso(datetime.now(timezone.utc) + timedelta(hours=1))
        transitions = self.storage.apply_search_observations(
            [("L3", self.search_id, later, listing)], {self.search_id: False}
        )
        self.assertEqual([t.transition for t in transitions], ["reactivated"])
        counts = self.storage.conn.execute(
            """
            SELECT is_active, COUNT(*) FROM listing_search_episodes
            WHERE listing_id = 'L3' GROUP BY is_active
            """
        ).fetchall()
        by_active = {int(r[0]): int(r[1]) for r in counts}
        self.assertEqual(by_active.get(0), 1)  # first episode closed
        self.assertEqual(by_active.get(1), 1)  # exactly one new open episode

    def test_open_episode_is_defensively_idempotent(self) -> None:
        now = Listing.now_iso()
        listing = _listing("L4")
        self.storage.insert_listings([listing])
        self.storage._open_episode("L4", self.search_id, now, listing)
        self.storage._open_episode("L4", self.search_id, now, listing)
        self.storage.conn.commit()
        rows = self.storage.conn.execute(
            """
            SELECT COUNT(*) FROM listing_search_episodes
            WHERE listing_id = 'L4' AND is_active = 1
            """
        ).fetchone()
        self.assertEqual(int(rows[0]), 1)

    def test_removal_uses_db_snapshot_when_listing_not_in_cycle(self) -> None:
        now = Listing.now_iso()
        listing = _listing("L5", title="Snapshot Title L5")
        self.storage.insert_listings([listing])
        self.storage.apply_search_observations([("L5", self.search_id, now, listing)], {self.search_id: False})
        # Removal with no listing object passed: snapshot must come from DB.
        self.storage.mark_memberships_inactive(self.search_id, {"L5"}, now)
        events = self.storage.conn.execute(
            """
            SELECT event_type, title FROM listing_search_events
            WHERE listing_id = 'L5' AND event_type = ?
            """,
            (EVENT_REMOVED,),
        ).fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(str(events[0][1]), "Snapshot Title L5")
        open_episodes = self.storage.conn.execute(
            """
            SELECT COUNT(*) FROM listing_search_episodes
            WHERE listing_id = 'L5' AND is_active = 1
            """
        ).fetchone()
        self.assertEqual(int(open_episodes[0]), 0)


class MissingSinceGraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(self._tmp.name)
        self.search = _search()
        self.search_id, _ = self.storage.register_search(self.search, Listing.now_iso())

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def test_first_absence_marks_missing_then_seen_clears(self) -> None:
        now = Listing.now_iso()
        listing = _listing("M1")
        self.storage.insert_listings([listing])
        self.storage.apply_search_observations([("M1", self.search_id, now, listing)], {self.search_id: False})
        run_id = self.storage.insert_search_run(
            search_id=self.search_id,
            started_at=now,
            finished_at=now,
            pages_fetched=2,
            results_count=10,
            complete=True,
            is_deep=True,
            criteria_fingerprint=self.storage.get_search_fingerprint(self.search_id),
        )
        self.storage.mark_memberships_missing(self.search_id, ["M1"], now, run_id)
        rows = self.storage.get_active_membership_rows(self.search_id)
        self.assertEqual(rows[0][2], now)  # missing_since set
        # Seen again → missing_since cleared.
        later = _iso(datetime.now(timezone.utc) + timedelta(hours=2))
        self.storage.apply_search_observations([("M1", self.search_id, later, listing)], {self.search_id: False})
        rows = self.storage.get_active_membership_rows(self.search_id)
        self.assertIsNone(rows[0][2])

    def test_removal_clears_missing_since(self) -> None:
        now = Listing.now_iso()
        listing = _listing("M2")
        self.storage.insert_listings([listing])
        self.storage.apply_search_observations([("M2", self.search_id, now, listing)], {self.search_id: False})
        run_id = self.storage.insert_search_run(
            search_id=self.search_id,
            started_at=now,
            finished_at=now,
            pages_fetched=2,
            results_count=10,
            complete=True,
            is_deep=True,
            criteria_fingerprint=self.storage.get_search_fingerprint(self.search_id),
        )
        self.storage.mark_memberships_missing(self.search_id, ["M2"], now, run_id)
        self.storage.mark_memberships_inactive(self.search_id, {"M2"}, now)
        row = self.storage.conn.execute(
            "SELECT missing_since, is_active FROM listing_search_state WHERE listing_id = 'M2'"
        ).fetchone()
        self.assertIsNone(row["missing_since"])
        self.assertEqual(int(row["is_active"]), 0)


class SearchRunListingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(self._tmp.name)
        self.search = _search()
        self.search_id, _ = self.storage.register_search(self.search, Listing.now_iso())

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def test_run_listings_round_trip_and_run_kind(self) -> None:
        now = Listing.now_iso()
        fingerprint = self.storage.get_search_fingerprint(self.search_id)
        run_id = self.storage.insert_search_run(
            search_id=self.search_id,
            started_at=now,
            finished_at=now,
            pages_fetched=3,
            results_count=2,
            complete=True,
            is_deep=True,
            criteria_fingerprint=fingerprint,
            run_kind="deep",
        )
        self.storage.insert_search_run_listings(run_id, ["A", "B", "A"])
        self.assertEqual(self.storage.get_run_listing_ids(run_id), {"A", "B"})
        latest = self.storage.get_latest_complete_deep_scan_run(self.search_id, fingerprint)
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest[0], run_id)

    def test_baseline_run_does_not_count_as_deep(self) -> None:
        now = Listing.now_iso()
        fingerprint = self.storage.get_search_fingerprint(self.search_id)
        self.storage.insert_search_run(
            search_id=self.search_id,
            started_at=now,
            finished_at=now,
            pages_fetched=3,
            results_count=2,
            complete=True,
            is_deep=False,
            criteria_fingerprint=fingerprint,
            run_kind="baseline",
        )
        self.assertIsNone(self.storage.get_latest_complete_deep_scan_run(self.search_id, fingerprint))


class AnalyticsEpochBootstrapTests(unittest.TestCase):
    def test_reopen_backfills_missing_active_episodes_even_when_epoch_exists(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            storage = Storage(tmp.name)
            now = Listing.now_iso()
            search = _search(name="Legacy", id="legacy", deep_scan=False)
            search_id, _ = storage.register_search(search, now)
            listing = _listing("BOOT", search_name=search.name)
            storage.insert_listings([listing])
            storage.upsert_listing_search_state([(listing.id, search_id, now)])
            # Simulate older partial migration state: epoch exists, no episodes.
            storage.conn.execute(
                "DELETE FROM listing_search_episodes WHERE listing_id = ?",
                (listing.id,),
            )
            storage.conn.execute(
                """
                INSERT INTO app_meta (key, value) VALUES ('analytics_v2_started_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (now,),
            )
            storage.conn.commit()
            storage.close()

            reopened = Storage(tmp.name)
            row = reopened.conn.execute(
                """
                SELECT COUNT(*)
                FROM listing_search_episodes
                WHERE listing_id = ? AND search_id = ? AND is_active = 1
                """,
                (listing.id, search_id),
            ).fetchone()
            self.assertEqual(int(row[0]), 1)
            reopened.close()
        finally:
            tmp.cleanup()


class ExpansionFailureTests(unittest.TestCase):
    def test_malformed_listing_object_is_a_failure(self) -> None:
        expanded, failures = expand_grouped_listings([{"not_listing": {}}])
        self.assertEqual(expanded, [])
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["reason"], "missing_listing_object")

    def test_invalid_subunit_is_a_failure_but_valid_ones_expand(self) -> None:
        raw = [
            {
                "listing": {
                    "id": 123,
                    "prs": {
                        "subUnits": [
                            {"id": 124, "price": "€1,000"},
                            "not-a-dict",
                        ]
                    },
                }
            }
        ]
        expanded, failures = expand_grouped_listings(raw)
        self.assertEqual(len(expanded), 1)
        self.assertEqual(expanded[0]["listing"]["id"], 124)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["reason"], "invalid_subunit")

    def test_studio_normalization_does_not_mutate_input(self) -> None:
        raw = [{"listing": {"id": 1, "propertyType": "Studio"}}]
        snapshot = copy.deepcopy(raw)
        expanded, failures = expand_grouped_listings(raw)
        self.assertEqual(failures, [])
        self.assertEqual(raw, snapshot)
        self.assertEqual(expanded[0]["listing"]["numBedrooms"], "1 bed")

    def test_grouped_expansion_does_not_mutate_input(self) -> None:
        raw = [
            {
                "listing": {
                    "id": 1,
                    "newHome": {"subUnits": [{"id": 2}, {"id": 3}]},
                }
            }
        ]
        snapshot = copy.deepcopy(raw)
        expanded, failures = expand_grouped_listings(raw)
        self.assertEqual(failures, [])
        self.assertEqual(len(expanded), 2)
        self.assertEqual(raw, snapshot)


class SegmentFallbackTests(unittest.TestCase):
    def test_room_and_facility_fallbacks_are_row_wise(self) -> None:
        df = pd.DataFrame(
            [
                # Structured fields present and matching.
                {
                    "location": "Dublin",
                    "room_type": "double",
                    "facilities": '["ensuite"]',
                    "title": "Room",
                    "bedrooms": None,
                    "analysis_price": 1000.0,
                },
                # Structured room_type says single; title says ensuite but the
                # row is NOT double → excluded even though title matches.
                {
                    "location": "Dublin",
                    "room_type": "single",
                    "facilities": None,
                    "title": "Ensuite Single Room Dublin",
                    "bedrooms": None,
                    "analysis_price": 900.0,
                },
                # No structured data → per-row fallback to text fields.
                {
                    "location": "Dublin",
                    "room_type": None,
                    "facilities": None,
                    "title": "Ensuite Double Room",
                    "bedrooms": "Double Room",
                    "analysis_price": 1100.0,
                },
            ]
        )
        out = apply_segment(df, "dublin_sharing_ensuite_1400")
        self.assertEqual(len(out), 2)
        self.assertNotIn("Ensuite Single Room Dublin", set(out["title"]))


if __name__ == "__main__":
    unittest.main()
