from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from daft_monitor.constants import EVENT_NEW, EVENT_PRICE_CHANGE, EVENT_RELISTED, EVENT_REMOVED


@dataclass(slots=True)
class Listing:
    id: str
    title: str
    price: str
    url: str
    location: str
    bedrooms: str | None
    image_url: str | None
    search_name: str
    first_seen: str
    latitude: float | None = None
    longitude: float | None = None
    distance_to_location: float | None = None
    last_seen: str | None = None
    is_active: bool = True
    last_price: str | None = None

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class ListingEvent:
    listing_id: str
    event_type: str
    timestamp: str
    old_value: str | None = None
    new_value: str | None = None
    metadata: str | None = None


def safe_listing_id(value: Any) -> str:
    return str(value).strip()

