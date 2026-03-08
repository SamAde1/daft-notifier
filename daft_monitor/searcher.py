from __future__ import annotations

import contextlib
import io
import time
from enum import Enum
from typing import Any, TypeVar
from unittest.mock import patch

from daftlistings import (
    AddedSince,
    Ber,
    Daft,
    Distance,
    Facility,
    MiscFilter,
    PropertyType,
    SearchType,
    SortType,
    SuitableFor,
)
import daftlistings.daft as daft_module

from daft_monitor.config import SearchConfig
from daft_monitor.models import Listing, safe_listing_id
from daft_monitor.wide_event import WideEvent


EnumT = TypeVar("EnumT", bound=Enum)
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) Gecko/20100101 Firefox/146.0"


def _enum_from_str(enum_cls: type[EnumT], value: str) -> EnumT:
    normalized = value.strip().upper()
    if normalized in enum_cls.__members__:
        return enum_cls[normalized]
    for item in enum_cls:
        if str(item.value).strip().lower() == value.strip().lower():
            return item
    raise ValueError(f"Invalid {enum_cls.__name__} value '{value}'.")


def _extract_location(raw: dict[str, Any]) -> str:
    if "displayAddress" in raw and raw["displayAddress"]:
        return str(raw["displayAddress"])
    if "seoFriendlyPath" in raw and raw["seoFriendlyPath"]:
        return str(raw["seoFriendlyPath"])
    return "Unknown"


def _extract_image_url(raw: dict[str, Any]) -> str | None:
    media = raw.get("media")
    if not isinstance(media, dict):
        return None
    images = media.get("images")
    if not isinstance(images, list) or not images:
        return None
    first = images[0]
    if not isinstance(first, dict):
        return None
    # Keep this resilient to minor API shape changes.
    if isinstance(first.get("url"), str):
        return first["url"]
    if isinstance(first.get("sizes"), list) and first["sizes"]:
        size0 = first["sizes"][0]
        if isinstance(size0, dict) and isinstance(size0.get("url"), str):
            return size0["url"]
    return None


def _map_listing(result: Any, search_name: str) -> Listing:
    raw = result.as_dict()
    bedrooms = None
    try:
        bedrooms = str(result.bedrooms)
    except Exception:
        bedrooms = None

    return Listing(
        id=safe_listing_id(result.id),
        title=str(result.title),
        price=str(result.price),
        url=str(result.daft_link),
        location=_extract_location(raw),
        bedrooms=bedrooms,
        image_url=_extract_image_url(raw),
        search_name=search_name,
        first_seen=Listing.now_iso(),
    )


class Searcher:
    @staticmethod
    def _prepare_client_headers(daft: Daft) -> None:
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "version": "0",
            "Origin": "https://www.daft.ie",
            "Referer": "https://www.daft.ie/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "pragma": "no-cache",
            "expires": "0",
            "cache-control": "no-cache, no-store",
        }
        # Prefer the library's public API when available.
        if hasattr(daft, "set_headers"):
            daft.set_headers(headers)
            return
        # Fallback for older daftlistings versions.
        Daft._HEADER.update(headers)

    def run_search(self, search_cfg: SearchConfig, event: WideEvent) -> list[Listing]:
        daft = Daft()
        self._prepare_client_headers(daft)
        daft.set_search_type(_enum_from_str(SearchType, search_cfg.search_type))
        if search_cfg.distance:
            daft.set_location(search_cfg.location, distance=_enum_from_str(Distance, search_cfg.distance))
        else:
            daft.set_location(search_cfg.location)

        if search_cfg.property_type:
            daft.set_property_type(_enum_from_str(PropertyType, search_cfg.property_type))
        if search_cfg.sort_type:
            daft.set_sort_type(_enum_from_str(SortType, search_cfg.sort_type))
        if search_cfg.added_since:
            daft.set_added_since(_enum_from_str(AddedSince, search_cfg.added_since))
        if search_cfg.min_ber:
            daft.set_min_ber(_enum_from_str(Ber, search_cfg.min_ber))
        if search_cfg.max_ber:
            daft.set_max_ber(_enum_from_str(Ber, search_cfg.max_ber))
        if search_cfg.suitable_for:
            for suitable in search_cfg.suitable_for:
                daft.set_suitability(_enum_from_str(SuitableFor, suitable))
        if search_cfg.facilities:
            for facility in search_cfg.facilities:
                daft.set_facility(_enum_from_str(Facility, facility))
        if search_cfg.misc_filters:
            parsed_misc_filters = [_enum_from_str(MiscFilter, f) for f in search_cfg.misc_filters]
            # daftlistings exports MiscFilter but has no public setter method.
            event.add_hop(
                "search_misc_filter",
                {
                    "search_name": search_cfg.name,
                    "requested_count": len(parsed_misc_filters),
                    "requested_filters": [m.name for m in parsed_misc_filters],
                    "status": "ignored_no_library_setter",
                },
            )
        # room_type: inject directly via the library's internal _add_filter.
        # The Daft API supports this as a filter (e.g. "double", "single", "twin", "shared")
        # but the daftlistings library has no public setter for it.
        if search_cfg.room_type:
            daft._add_filter("roomType", search_cfg.room_type)
            event.add_hop(
                "custom_filter",
                {
                    "search_name": search_cfg.name,
                    "filter": "roomType",
                    "value": search_cfg.room_type,
                },
            )

        # custom_filters: inject arbitrary name/value filters the library doesn't expose.
        if search_cfg.custom_filters:
            for filter_name, filter_value in search_cfg.custom_filters.items():
                if isinstance(filter_value, list):
                    for val in filter_value:
                        daft._add_filter(filter_name, val)
                else:
                    daft._add_filter(filter_name, filter_value)
            event.add_hop(
                "custom_filters",
                {
                    "search_name": search_cfg.name,
                    "filters": search_cfg.custom_filters,
                },
            )

        if search_cfg.min_price is not None:
            daft.set_min_price(search_cfg.min_price)
        if search_cfg.max_price is not None:
            daft.set_max_price(search_cfg.max_price)
        if search_cfg.min_beds is not None:
            daft.set_min_beds(search_cfg.min_beds)
        if search_cfg.max_beds is not None:
            daft.set_max_beds(search_cfg.max_beds)
        if search_cfg.min_baths is not None:
            daft.set_min_baths(search_cfg.min_baths)
        if search_cfg.max_baths is not None:
            daft.set_max_baths(search_cfg.max_baths)
        if search_cfg.owner_occupied is not None:
            daft.set_owner_occupied(search_cfg.owner_occupied)
        if search_cfg.min_tenants is not None:
            daft.set_min_tenants(search_cfg.min_tenants)
        if search_cfg.max_tenants is not None:
            daft.set_max_tenants(search_cfg.max_tenants)
        if search_cfg.min_lease is not None:
            daft.set_min_lease(search_cfg.min_lease)
        if search_cfg.max_lease is not None:
            daft.set_max_lease(search_cfg.max_lease)
        if search_cfg.min_floor_size is not None:
            daft.set_min_floor_size(search_cfg.min_floor_size)
        if search_cfg.max_floor_size is not None:
            daft.set_max_floor_size(search_cfg.max_floor_size)

        results = None
        attempt_errors: list[str] = []
        attempt_contexts: list[dict[str, Any]] = []
        for attempt in range(1, 3):
            context: dict[str, Any] = {"attempt": attempt}
            original_post = daft_module.requests.post

            def _capture_post(*args: Any, **kwargs: Any) -> Any:
                response = original_post(*args, **kwargs)
                context["http_status"] = response.status_code
                context["content_type"] = response.headers.get("content-type")
                context["response_url"] = response.url
                preview = response.text[:220].replace("\n", " ").strip()
                context["body_preview"] = preview
                return response

            try:
                with patch("daftlistings.daft.requests.post", side_effect=_capture_post):
                    with contextlib.redirect_stdout(io.StringIO()):
                        results = daft.search(max_pages=search_cfg.max_pages)
                context["status"] = "ok"
                attempt_contexts.append(context)
                break
            except Exception as exc:
                attempt_errors.append(f"attempt_{attempt}:{exc}")
                context["status"] = "error"
                context["error"] = str(exc)
                attempt_contexts.append(context)
                if attempt < 2:
                    time.sleep(1.0)

        if results is None:
            event.add_hop(
                "daft_search_http",
                {
                    "search_name": search_cfg.name,
                    "attempts": attempt_contexts,
                },
            )
            raise RuntimeError("daft.search failed after retries: " + " | ".join(attempt_errors))

        mapped = [_map_listing(result, search_cfg.name) for result in results]
        event.add_hop(
            "daft_search",
            {
                "search_name": search_cfg.name,
                "fetched_count": len(mapped),
                "attempt_count": len(attempt_errors) + 1,
                "http": attempt_contexts,
            },
        )
        event.add_search(search_cfg.name)
        event.increment("total_listings_fetched", len(mapped))
        return mapped

    def run_all(self, searches: list[SearchConfig], event: WideEvent) -> list[Listing]:
        all_listings: list[Listing] = []
        for search_cfg in searches:
            try:
                all_listings.extend(self.run_search(search_cfg, event))
            except Exception as exc:
                event.add_error(
                    "search_failed",
                    {
                        "search_name": search_cfg.name,
                        "error": str(exc),
                    },
                )
                event.add_hop(
                    "daft_search",
                    {
                        "search_name": search_cfg.name,
                        "status": "error",
                    },
                )
        return all_listings

