"""Stable identity helpers for configured searches.

Each search in config.yaml gets:
  - a search_id (optional `id` key, otherwise the human-readable `name`)
  - a criteria_fingerprint (hash of the filters that define which listings match)

When the fingerprint changes (e.g. you raise max_price), the next run
silently seeds newly included listings instead of treating them as alerts.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from daft_monitor.config import SearchConfig

# Fields that define *which* listings a search returns.
# Operational knobs (notify, max_pages, deep_scan, shallow_pages) are excluded
# so tweaking them does not look like a criteria change.
_CRITERIA_FIELDS = (
    "search_type",
    "location",
    "distance",
    "suitable_for",
    "facilities",
    "misc_filters",
    "added_since",
    "min_ber",
    "max_ber",
    "min_price",
    "max_price",
    "min_beds",
    "max_beds",
    "min_baths",
    "max_baths",
    "owner_occupied",
    "min_tenants",
    "max_tenants",
    "min_lease",
    "max_lease",
    "min_floor_size",
    "max_floor_size",
    "property_type",
    "room_type",
    "custom_filters",
)


def resolved_search_id(search: SearchConfig) -> str:
    """Return the stable id for a search (config `id`, else `name`)."""
    if search.id and search.id.strip():
        return search.id.strip()
    return search.name.strip()


def criteria_fingerprint(search: SearchConfig) -> str:
    """SHA-256 of criteria fields with canonical JSON key ordering."""
    payload: dict[str, Any] = {}
    for field in _CRITERIA_FIELDS:
        value = getattr(search, field, None)
        payload[field] = _canonicalize(value)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonicalize(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        # Sort so ["Dublin","Kildare"] and ["Kildare","Dublin"] fingerprint the same.
        return [_canonicalize(v) for v in sorted(value, key=lambda x: str(x))]
    if isinstance(value, dict):
        return {str(k): _canonicalize(value[k]) for k in sorted(value.keys(), key=str)}
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    return str(value)
