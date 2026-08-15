from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

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
    # Parsed price fields (filled at insert / price-change time).
    price_value: float | None = None
    price_period: str | None = None
    price_monthly_eq: float | None = None
    removed_at: str | None = None
    room_type: str | None = None
    facilities: list[str] | None = None

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


MembershipTransitionKind = Literal["created", "reactivated", "unchanged"]


@dataclass(slots=True)
class MembershipTransition:
    listing_id: str
    search_id: str
    transition: MembershipTransitionKind
    listing: Listing
    timestamp: str


def safe_listing_id(value: Any) -> str:
    return str(value).strip()

