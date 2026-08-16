"""Unit tests for search identity helpers and storage behaviour."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from daft_monitor.config import SearchConfig
from daft_monitor.constants import EVENT_SEED
from daft_monitor.models import Listing, ListingEvent
from daft_monitor.search_identity import criteria_fingerprint, resolved_search_id
from daft_monitor.storage import Storage


def _search(**overrides: object) -> SearchConfig:
    base = dict(
        name="Dublin Sharing",
        search_type="SHARING",
        location=["Dublin", "Dublin City"],
        max_price=1400,
        room_type="double",
        notify=True,
    )
    base.update(overrides)
    return SearchConfig(**base)  # type: ignore[arg-type]


def _listing(listing_id: str, search_name: str = "Dublin Sharing", price: str = "€1,200 per month") -> Listing:
    return Listing(
        id=listing_id,
        title=f"Room {listing_id}",
        price=price,
        url=f"https://www.daft.ie/{listing_id}",
        location="Dublin",
        bedrooms="Double Room",
        image_url=None,
        search_name=search_name,
        first_seen=Listing.now_iso(),
    )


class SearchIdentityTests(unittest.TestCase):
    def test_resolved_id_defaults_to_name(self) -> None:
        self.assertEqual(resolved_search_id(_search()), "Dublin Sharing")
        self.assertEqual(resolved_search_id(_search(id="dublin-sharing")), "dublin-sharing")

    def test_fingerprint_stable_under_location_reorder(self) -> None:
        a = criteria_fingerprint(_search(location=["Dublin", "Kildare"]))
        b = criteria_fingerprint(_search(location=["Kildare", "Dublin"]))
        self.assertEqual(a, b)

    def test_fingerprint_ignores_notify_and_max_pages(self) -> None:
        a = criteria_fingerprint(_search(notify=True, max_pages=2, deep_scan=False, shallow_pages=2))
        b = criteria_fingerprint(_search(notify=False, max_pages=99, deep_scan=True, shallow_pages=5))
        self.assertEqual(a, b)

    def test_fingerprint_changes_when_max_price_changes(self) -> None:
        a = criteria_fingerprint(_search(max_price=1400))
        b = criteria_fingerprint(_search(max_price=2000))
        self.assertNotEqual(a, b)


class StorageStage1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(self._tmp.name)

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def test_schema_tables_exist(self) -> None:
        tables = {
            row[0] for row in self.storage.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        for required in (
            "listings",
            "listing_events",
            "searches",
            "listing_search_state",
            "search_runs",
            "app_meta",
        ):
            self.assertIn(required, tables)

    def test_insert_parses_price(self) -> None:
        listing = _listing("L1", price="€250 per week")
        self.storage.insert_listings([listing])
        row = self.storage.conn.execute(
            "SELECT price_value, price_period, price_monthly_eq FROM listings WHERE id='L1'"
        ).fetchone()
        self.assertEqual(row["price_value"], 250.0)
        self.assertEqual(row["price_period"], "week")
        self.assertAlmostEqual(row["price_monthly_eq"], 250.0 * 52.0 / 12.0)

    def test_update_listing_price_refreshes_raw_and_parsed(self) -> None:
        self.storage.insert_listings([_listing("L1", price="€1,000 per month")])
        self.storage.update_listing_price("L1", "€1,100 per month", Listing.now_iso())
        row = self.storage.conn.execute(
            "SELECT price, last_price, price_value, price_period FROM listings WHERE id='L1'"
        ).fetchone()
        self.assertEqual(row["price"], "€1,100 per month")
        self.assertEqual(row["last_price"], "€1,100 per month")
        self.assertEqual(row["price_value"], 1100.0)
        self.assertEqual(row["price_period"], "month")

    def test_register_search_and_seed_flag(self) -> None:
        search = _search(id="dublin-sharing")
        now = Listing.now_iso()
        search_id, changed = self.storage.register_search(search, now)
        self.assertEqual(search_id, "dublin-sharing")
        self.assertTrue(changed)
        self.assertTrue(self.storage.search_needs_seed(search_id))

        self.storage.insert_listings([_listing("L1")])
        self.storage.upsert_listing_search_state([("L1", search_id, now)])
        self.assertTrue(self.storage.search_is_seeding(search_id))
        fingerprint = self.storage.get_search_fingerprint(search_id)
        assert fingerprint is not None
        self.storage.clear_pending_seed(search_id, fingerprint)
        self.assertFalse(self.storage.search_is_seeding(search_id))

        # Criteria change flips fingerprint_changed.
        wider = _search(id="dublin-sharing", max_price=2000)
        _, changed_again = self.storage.register_search(wider, now)
        self.assertTrue(changed_again)

    def test_mark_removed_preserves_last_seen(self) -> None:
        listing = _listing("L1")
        listing.last_seen = "2026-01-01T00:00:00+00:00"
        self.storage.insert_listings([listing])
        self.storage.mark_listings_removed({"L1"}, "2026-02-01T00:00:00+00:00")
        row = self.storage.conn.execute(
            "SELECT is_active, last_seen, removed_at FROM listings WHERE id='L1'"
        ).fetchone()
        self.assertEqual(row["is_active"], 0)
        self.assertEqual(row["last_seen"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(row["removed_at"], "2026-02-01T00:00:00+00:00")

    def test_app_meta_roundtrip(self) -> None:
        self.storage.set_meta("lifecycle_v2_started_at", "2026-07-01T00:00:00+00:00")
        self.assertEqual(self.storage.get_meta("lifecycle_v2_started_at"), "2026-07-01T00:00:00+00:00")

    def test_seed_event_constant(self) -> None:
        self.assertEqual(EVENT_SEED, "seed")
        self.storage.insert_listings([_listing("L1")])
        self.storage.insert_event(ListingEvent(listing_id="L1", event_type=EVENT_SEED, timestamp=Listing.now_iso()))
        row = self.storage.conn.execute("SELECT event_type FROM listing_events WHERE listing_id='L1'").fetchone()
        self.assertEqual(row["event_type"], "seed")


class NotifyConfigTests(unittest.TestCase):
    def test_notify_defaults_true(self) -> None:
        self.assertTrue(_search().notify)
        self.assertFalse(_search(notify=False).notify)

    def test_load_config_notify_and_id(self) -> None:
        from daft_monitor.config import load_config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                """
check_interval_minutes: 30
data_dir: "./data"
distance_to_location: false
searches:
  - name: "Obs"
    id: "obs-1"
    search_type: "SHARING"
    location: "Dublin"
    notify: false
notifications: {}
""",
                encoding="utf-8",
            )
            cfg = load_config(str(path))
            self.assertEqual(cfg.searches[0].id, "obs-1")
            self.assertFalse(cfg.searches[0].notify)

    def test_load_config_rejects_duplicate_names(self) -> None:
        from daft_monitor.config import load_config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                """
check_interval_minutes: 30
data_dir: "./data"
distance_to_location: false
searches:
  - name: "Same"
    search_type: "SHARING"
    location: "Dublin"
    notify: true
  - name: "Same"
    search_type: "SHARING"
    location: "Kildare"
    notify: false
notifications: {}
""",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                load_config(str(path))
            self.assertIn("duplicates", str(ctx.exception).lower())

    def test_load_config_rejects_duplicate_search_ids(self) -> None:
        from daft_monitor.config import load_config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                """
check_interval_minutes: 30
data_dir: "./data"
distance_to_location: false
searches:
  - name: "A"
    id: "shared-id"
    search_type: "SHARING"
    location: "Dublin"
  - name: "B"
    id: "shared-id"
    search_type: "SHARING"
    location: "Kildare"
notifications: {}
""",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                load_config(str(path))
            self.assertIn("search_id", str(ctx.exception))

    def test_search_by_name_rejects_duplicates(self) -> None:
        from daft_monitor.main import _search_by_name

        with self.assertRaises(ValueError) as ctx:
            _search_by_name([_search(name="Dup"), _search(name="Dup", notify=False)])
        self.assertIn("Duplicate search name", str(ctx.exception))


class OverlapDedupeTests(unittest.TestCase):
    def test_notify_true_wins_when_notify_false_copy_is_last(self) -> None:
        from daft_monitor.main import _dedupe_with_search_context

        silent = _search(name="Silent Obs", id="silent", notify=False)
        alerting = _search(name="Alert Search", id="alert", notify=True)
        name_to_search = {silent.name: silent, alerting.name: alerting}
        seed_flags = {resolved_search_id(silent): False, resolved_search_id(alerting): False}

        # Last copy is notify=false — old logic would suppress alerts.
        listings = [
            _listing("L1", search_name=alerting.name),
            _listing("L1", search_name=silent.name),
        ]
        deduped, seed_ids = _dedupe_with_search_context(listings, name_to_search, seed_flags)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].search_name, alerting.name)
        self.assertNotIn("L1", seed_ids)

    def test_seed_only_when_all_matching_searches_are_seeding(self) -> None:
        from daft_monitor.main import _dedupe_with_search_context

        seeding = _search(name="New Broad", id="broad", notify=False)
        live = _search(name="Live Narrow", id="narrow", notify=True)
        name_to_search = {seeding.name: seeding, live.name: live}
        seed_flags = {resolved_search_id(seeding): True, resolved_search_id(live): False}

        listings = [
            _listing("L1", search_name=seeding.name),
            _listing("L1", search_name=live.name),
        ]
        deduped, seed_ids = _dedupe_with_search_context(listings, name_to_search, seed_flags)
        self.assertEqual(len(deduped), 1)
        self.assertNotIn("L1", seed_ids)
        self.assertEqual(deduped[0].search_name, live.name)

    def test_all_seeding_matches_go_to_seed(self) -> None:
        from daft_monitor.main import _dedupe_with_search_context

        a = _search(name="Seed A", id="a", notify=False)
        b = _search(name="Seed B", id="b", notify=True)
        name_to_search = {a.name: a, b.name: b}
        seed_flags = {resolved_search_id(a): True, resolved_search_id(b): True}

        listings = [
            _listing("L1", search_name=a.name),
            _listing("L1", search_name=b.name),
        ]
        _, seed_ids = _dedupe_with_search_context(listings, name_to_search, seed_flags)
        self.assertIn("L1", seed_ids)


if __name__ == "__main__":
    unittest.main()
