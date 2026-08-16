"""Unit tests for OSRM distance batching / throttle helpers.

Run with:
    python -m unittest tests.test_distance_backfill
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from daft_monitor.distance import fetch_distances_batch_km


class FetchDistancesThrottleTests(unittest.TestCase):
    def test_delay_between_batches_not_before_first(self) -> None:
        destinations = [(f"id{i}", 53.3 + i * 0.001, -6.2) for i in range(3)]

        def _response_for_batch(batch_size: int) -> MagicMock:
            response = MagicMock()
            response.raise_for_status = MagicMock()
            # Origin + batch destinations.
            response.json.return_value = {"distances": [[0.0] + [1000.0 * (i + 1) for i in range(batch_size)]]}
            return response

        responses = [_response_for_batch(2), _response_for_batch(1)]

        with patch("daft_monitor.distance.requests.get", side_effect=responses) as get_mock:
            with patch("daft_monitor.distance.time.sleep") as sleep_mock:
                result = fetch_distances_batch_km(
                    53.35,
                    -6.26,
                    destinations,
                    max_batch_size=2,
                    delay_seconds=1.0,
                )

        self.assertEqual(len(result), 3)
        self.assertEqual(get_mock.call_count, 2)
        sleep_mock.assert_called_once_with(1.0)


if __name__ == "__main__":
    unittest.main()
