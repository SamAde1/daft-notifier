from __future__ import annotations

import time
from typing import Iterable, TypeVar

import requests

from daft_monitor.constants import OSRM_TABLE_URL

_T = TypeVar("_T")


def _chunked(items: list[_T], size: int) -> Iterable[list[_T]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _parse_distances(payload: dict, expected_count: int) -> list[float | None]:
    distances = payload.get("distances")
    if not isinstance(distances, list) or not distances or not isinstance(distances[0], list):
        raise ValueError("Invalid OSRM response payload: missing distances matrix.")
    row = distances[0]
    if len(row) < expected_count + 1:
        raise ValueError("Invalid OSRM response payload: distances matrix size mismatch.")
    result: list[float | None] = []
    for meters in row[1 : expected_count + 1]:
        if meters is None:
            result.append(None)
        else:
            result.append(float(meters) / 1000.0)  # meters -> km
    return result


def fetch_distances_batch_km(
    origin_lat: float,
    origin_lng: float,
    destinations: list[tuple[str, float, float]],
    *,
    timeout_seconds: int = 25,
    max_batch_size: int = 100,
    delay_seconds: float = 0.0,
) -> dict[str, float]:
    """Fetch driving distance from one origin to many destination points using OSRM table API.

    destinations format: [(listing_id, latitude, longitude), ...]
    Returns: {listing_id: distance_km}

    delay_seconds sleeps between batch requests (not before the first) to
    throttle public OSRM usage during overnight backfills.
    """
    if not destinations:
        return {}

    results: dict[str, float] = {}
    headers = {"Accept": "application/json"}

    chunks = list(_chunked(destinations, max_batch_size))
    for index, chunk in enumerate(chunks):
        if index > 0 and delay_seconds > 0:
            time.sleep(delay_seconds)

        coords = [f"{origin_lng},{origin_lat}"]
        ids: list[str] = []
        for listing_id, lat, lng in chunk:
            ids.append(listing_id)
            coords.append(f"{lng},{lat}")  # OSRM uses longitude,latitude order
        url = f"{OSRM_TABLE_URL}{';'.join(coords)}?sources=0&annotations=distance"

        response = requests.get(url, headers=headers, timeout=timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        distances_km = _parse_distances(payload, expected_count=len(chunk))

        for listing_id, distance_km in zip(ids, distances_km):
            if distance_km is not None:
                results[listing_id] = distance_km

    return results
