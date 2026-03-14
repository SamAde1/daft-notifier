from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


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

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()


def safe_listing_id(value: Any) -> str:
    return str(value).strip()

