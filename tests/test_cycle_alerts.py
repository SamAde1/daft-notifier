"""End-to-end _run_cycle coverage for live new-listing and relist alerts.

The previous alert-path rewrite referenced alert_listing_ids before assignment,
which aborted any cycle that saw a live new listing. These tests exercise the
full cycle orchestration with a mocked Searcher.

Run with:
    python -m unittest tests.test_cycle_alerts
"""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from daft_monitor.config import AppConfig, NotifierConfig, SearchConfig
from daft_monitor.constants import EVENT_NEW, EVENT_RELISTED
from daft_monitor.main import _run_cycle
from daft_monitor.models import Listing
from daft_monitor.searcher import SearchRunResult
from daft_monitor.storage import Storage
from daft_monitor.wide_event import WideEvent


def _search(**overrides: object) -> SearchConfig:
    base: dict[str, object] = {
        "name": "Alert Search",
        "search_type": "SHARING",
        "location": "Dublin",
        "notify": True,
        "deep_scan": False,
        "id": "alert-search",
    }
    base.update(overrides)
    return SearchConfig(**base)  # type: ignore[arg-type]


def _listing(listing_id: str, search_name: str = "Alert Search") -> Listing:
    now = Listing.now_iso()
    return Listing(
        id=listing_id,
        title=f"Listing {listing_id}",
        price="€1,200 per month",
        url=f"https://www.daft.ie/{listing_id}",
        location="Dublin",
        bedrooms="Double Room",
        image_url=None,
        search_name=search_name,
        first_seen=now,
        last_seen=now,
        last_price="€1,200 per month",
    )


def _app_config(data_dir: str, searches: list[SearchConfig]) -> AppConfig:
    return AppConfig(
        check_interval_minutes=30,
        data_dir=data_dir,
        distance_to_location=False,
        location_name="City Centre",
        location_latitude=None,
        location_longitude=None,
        searches=searches,
        notifiers=[
            NotifierConfig(
                name="recording-alerts",
                type="ntfy",
                role="alerts",
                environments=["dev"],
                enabled=True,
                topic="unused",
            )
        ],
        removal_grace_hours=48,
        deep_scan_min_interval_hours=24,
    )


def _run(listings: list[Listing], *, complete: bool, run_kind: str) -> SearchRunResult:
    return SearchRunResult(
        listings=listings,
        search_name="Alert Search",
        pages_fetched=1,
        results_count=len(listings),
        complete=complete,
        is_deep=False,
        error=None,
        run_kind=run_kind,
    )


class RecordingNotifier:
    name = "recording-alerts"

    def __init__(self) -> None:
        self.sent: list[Listing] = []

    def send(self, listing: Listing, event: WideEvent) -> bool:
        self.sent.append(listing)
        return True

    def send_error(self, error_title: str, error_body: str, event: WideEvent) -> bool:
        return True

    def send_digest(self, digest_title: str, digest_body: str, event: WideEvent) -> bool:
        return True


class FakeSearcher:
    def __init__(self, results: list[list[SearchRunResult]]) -> None:
        self._results = results
        self.calls = 0

    def run_all(self, *args: object, **kwargs: object) -> list[SearchRunResult]:
        result = self._results[self.calls]
        self.calls += 1
        return result


class CycleAlertIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(self._tmp.name)
        self.search = _search()
        self.config = _app_config(self._tmp.name, [self.search])
        self.recorder = RecordingNotifier()
        self.payloads: list[dict[str, object]] = []

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def _run_cycle(self, searcher: FakeSearcher) -> dict[str, object]:
        def capture_emit(event: WideEvent, _logger: object) -> None:
            self.payloads.append(event.finalize())

        with (
            patch("daft_monitor.main.build_alert_notifiers", return_value=[self.recorder]),
            patch("daft_monitor.main.build_error_notifiers", return_value=[]),
            patch.object(WideEvent, "emit", capture_emit),
        ):
            _run_cycle(self.config, self.storage, searcher, "dev")  # type: ignore[arg-type]
        return self.payloads[-1]

    def test_live_new_listing_is_inserted_and_alerted(self) -> None:
        seed = _listing("SEED-1")
        new = _listing("LIVE-1")
        searcher = FakeSearcher(
            [
                [_run([seed], complete=True, run_kind="baseline")],
                [_run([seed, new], complete=False, run_kind="shallow")],
            ]
        )

        first = self._run_cycle(searcher)
        self.assertEqual(first["status"], "ok")
        self.assertFalse(first["errors"])
        self.assertTrue(self.storage.listing_exists("SEED-1"))
        self.assertEqual(self.recorder.sent, [])

        second = self._run_cycle(searcher)
        self.assertEqual(second["status"], "ok")
        self.assertFalse(second["errors"])
        self.assertTrue(self.storage.listing_exists("LIVE-1"))
        self.assertEqual([listing.id for listing in self.recorder.sent], ["LIVE-1"])
        self.assertEqual(second["notifications_sent"], 1)
        self.assertEqual(second["new_listings_count"], 1)
        events = {
            str(row["event_type"])
            for row in self.storage.conn.execute("SELECT event_type FROM listing_events WHERE listing_id = 'LIVE-1'")
        }
        self.assertIn(EVENT_NEW, events)

    def test_relisted_listing_is_alerted_without_new_insert(self) -> None:
        seed = _listing("SEED-1")
        gone = _listing("GONE-1")
        searcher = FakeSearcher(
            [
                [_run([seed, gone], complete=True, run_kind="baseline")],
                [_run([seed], complete=False, run_kind="shallow")],
                [_run([seed, gone], complete=False, run_kind="shallow")],
            ]
        )

        self._run_cycle(searcher)
        removed = self._run_cycle(searcher)
        self.assertEqual(removed["status"], "ok")
        self.assertNotIn("GONE-1", self.storage.get_active_listing_ids())
        self.recorder.sent.clear()

        relisted = self._run_cycle(searcher)
        self.assertEqual(relisted["status"], "ok")
        self.assertFalse(relisted["errors"])
        self.assertIn("GONE-1", self.storage.get_active_listing_ids())
        self.assertEqual([listing.id for listing in self.recorder.sent], ["GONE-1"])
        self.assertEqual(relisted["new_listings_count"], 0)
        events = {
            str(row["event_type"])
            for row in self.storage.conn.execute("SELECT event_type FROM listing_events WHERE listing_id = 'GONE-1'")
        }
        self.assertIn(EVENT_RELISTED, events)


if __name__ == "__main__":
    unittest.main()
