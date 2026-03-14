from __future__ import annotations

from typing import Iterable

import requests


OSRM_TABLE_URL = "http://router.project-osrm.org/table/v1/driving/"


def _chunked[T](items: list[T], size: int) -> Iterable[list[T]]:
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
) -> dict[str, float]:
    """Fetch driving distance from one origin to many destination points using OSRM table API.

    destinations format: [(listing_id, latitude, longitude), ...]
    Returns: {listing_id: distance_km}
    """
    if not destinations:
        return {}

    results: dict[str, float] = {}
    headers = {"Accept": "application/json"}

    for chunk in _chunked(destinations, max_batch_size):
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
