from __future__ import annotations

import contextlib
import io
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from math import ceil
from typing import Any, TypeVar
from unittest.mock import patch

import daftlistings.daft as daft_module
import requests
from daftlistings import (
    AddedSince,
    Ber,
    Daft,
    Distance,
    Facility,
    Listing as DaftListing,
    MiscFilter,
    PropertyType,
    SearchType,
    SortType,
    SuitableFor,
)

from daft_monitor.config import AppConfig, SearchConfig
from daft_monitor.constants import DEFAULT_USER_AGENT
from daft_monitor.lifecycle_v2 import PAGE_SIZE, assess_deep_scan_complete
from daft_monitor.listing_expand import expand_grouped_listings
from daft_monitor.models import Listing, safe_listing_id
from daft_monitor.wide_event import WideEvent


EnumT = TypeVar("EnumT", bound=Enum)


@dataclass(slots=True)
class SearchRunResult:
    """Outcome of one shallow or deep search execution."""

    listings: list[Listing]
    search_name: str
    pages_fetched: int = 0
    results_count: int = 0
    complete: bool = False
    is_deep: bool = False
    error: str | None = None
    last_page_size: int = 0
    page_errors: int = 0
    hit_max_pages_cap: bool = False
    http: list[dict[str, Any]] = field(default_factory=list)
    mapping_failures: int = 0
    retry_after_seconds: int | None = None
    # "shallow" | "deep" | "baseline". Only complete deep runs authorize
    # removals; only complete baseline/deep runs clear pending seeding.
    run_kind: str = "shallow"


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
    if isinstance(first.get("url"), str):
        return first["url"]
    if isinstance(first.get("sizes"), list) and first["sizes"]:
        size0 = first["sizes"][0]
        if isinstance(size0, dict) and isinstance(size0.get("url"), str):
            return size0["url"]
    return None


def _extract_coordinates(result: Any) -> tuple[float | None, float | None]:
    try:
        return float(result.latitude), float(result.longitude)
    except Exception:
        return None, None


def _extract_room_type(raw: dict[str, Any]) -> str | None:
    room_type = raw.get("roomType")
    if room_type:
        return str(room_type).strip().lower()
    bedrooms = str(raw.get("numBedrooms") or "").lower()
    if "double" in bedrooms:
        return "double"
    if "single" in bedrooms:
        return "single"
    if "twin" in bedrooms:
        return "twin"
    return None


def _extract_facilities(raw: dict[str, Any]) -> list[str] | None:
    found: set[str] = set()
    for key in ("labels", "facilities"):
        values = raw.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, str):
                found.add(item.strip().lower())
            elif isinstance(item, dict):
                label = item.get("name") or item.get("label")
                if label:
                    found.add(str(label).strip().lower())
    overview = raw.get("propertyOverview")
    if isinstance(overview, list):
        for item in overview:
            if isinstance(item, dict) and item.get("label"):
                found.add(str(item["label"]).strip().lower())
    return sorted(found) if found else None


def _map_listing(result: Any, search_name: str) -> Listing:
    raw = result.as_dict()
    bedrooms = None
    try:
        bedrooms = str(result.bedrooms)
    except Exception:
        bedrooms = None

    lat, lng = _extract_coordinates(result)

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
        latitude=lat,
        longitude=lng,
        room_type=_extract_room_type(raw),
        facilities=_extract_facilities(raw),
    )


def effective_shallow_pages(search_cfg: SearchConfig) -> int | None:
    """Pages for regular cycles: shallow_pages, else max_pages, else library default (all)."""
    if search_cfg.shallow_pages is not None:
        return search_cfg.shallow_pages
    return search_cfg.max_pages


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
        if hasattr(daft, "set_headers"):
            daft.set_headers(headers)
            return
        Daft._HEADER.update(headers)

    def _configure_daft(self, search_cfg: SearchConfig, event: WideEvent) -> Daft:
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
            event.add_hop(
                "search_misc_filter",
                {
                    "search_name": search_cfg.name,
                    "requested_count": len(parsed_misc_filters),
                    "requested_filters": [m.name for m in parsed_misc_filters],
                    "status": "ignored_no_library_setter",
                },
            )
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
        return daft

    def run_search(
        self,
        search_cfg: SearchConfig,
        event: WideEvent,
        *,
        is_deep: bool = False,
        max_pages: int | None = None,
        previous_complete_count: int | None = None,
        page_jitter_seconds: tuple[float, float] = (1.0, 3.0),
    ) -> SearchRunResult:
        """Run one search. Deep scans paginate with jitter and completeness checks."""
        daft = self._configure_daft(search_cfg, event)
        if is_deep:
            return self._run_paged_search(
                daft,
                search_cfg,
                event,
                is_deep=True,
                max_pages=max_pages,
                previous_complete_count=previous_complete_count,
                page_jitter_seconds=page_jitter_seconds,
            )

        # Shallow / legacy: use library search with page limit.
        page_limit = max_pages if max_pages is not None else effective_shallow_pages(search_cfg)
        return self._run_library_search(daft, search_cfg, event, max_pages=page_limit)

    def _run_library_search(
        self,
        daft: Daft,
        search_cfg: SearchConfig,
        event: WideEvent,
        *,
        max_pages: int | None,
    ) -> SearchRunResult:
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
                        results = daft.search(max_pages=max_pages)
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
            error = "daft.search failed after retries: " + " | ".join(attempt_errors)
            event.add_error("search_failed", {"search_name": search_cfg.name, "error": error})
            return SearchRunResult(
                listings=[],
                search_name=search_cfg.name,
                error=error,
                is_deep=False,
                complete=False,
                http=attempt_contexts,
            )

        mapped = [_map_listing(result, search_cfg.name) for result in results]
        pages_fetched = 1 if not mapped else max(1, ceil(len(mapped) / PAGE_SIZE))
        event.add_hop(
            "daft_search",
            {
                "search_name": search_cfg.name,
                "fetched_count": len(mapped),
                "pages_fetched_estimate": pages_fetched,
                "attempt_count": len(attempt_errors) + 1,
                "is_deep": False,
                "http": attempt_contexts,
            },
        )
        event.add_search(search_cfg.name)
        event.increment("total_listings_fetched", len(mapped))
        return SearchRunResult(
            listings=mapped,
            search_name=search_cfg.name,
            pages_fetched=pages_fetched,
            results_count=len(mapped),
            complete=False,  # shallow never authorizes removals
            is_deep=False,
            last_page_size=len(mapped) % PAGE_SIZE or (PAGE_SIZE if mapped else 0),
            http=attempt_contexts,
        )

    def _run_paged_search(
        self,
        daft: Daft,
        search_cfg: SearchConfig,
        event: WideEvent,
        *,
        is_deep: bool,
        max_pages: int | None,
        previous_complete_count: int | None,
        page_jitter_seconds: tuple[float, float],
    ) -> SearchRunResult:
        payload = daft._make_payload()
        headers = dict(Daft._HEADER)
        http_log: list[dict[str, Any]] = []
        raw_listings: list[dict[str, Any]] = []
        pages_fetched = 0
        last_page_size = 0
        page_errors = 0
        hit_cap = False
        error: str | None = None
        retry_after_seconds: int | None = None

        try:
            # First page
            response = requests.post(daft._ENDPOINT, headers=headers, json=payload, timeout=60)
            http_log.append(
                {
                    "page": 0,
                    "http_status": response.status_code,
                    "status": "ok" if response.status_code == 200 else "error",
                }
            )
            if response.status_code in {403, 429}:
                error = f"aborted_http_{response.status_code}"
                page_errors += 1
                with contextlib.suppress(Exception):
                    retry_after_seconds = int(response.headers.get("Retry-After", "0")) or None
            elif response.status_code != 200:
                error = f"http_{response.status_code}"
                page_errors += 1
            else:
                body = response.json()
                page_listings = body.get("listings") or []
                raw_listings.extend(page_listings)
                pages_fetched = 1
                last_page_size = len(page_listings)
                total_results = int(body.get("paging", {}).get("totalResults") or 0)
                total_pages = max(1, ceil(total_results / PAGE_SIZE)) if total_results else 1
                limit = min(max_pages, total_pages) if max_pages is not None else total_pages
                if max_pages is not None and total_pages > max_pages:
                    hit_cap = True

                for page in range(1, limit):
                    low, high = page_jitter_seconds
                    time.sleep(random.uniform(low, high))
                    payload["paging"]["from"] = page * PAGE_SIZE
                    response = requests.post(daft._ENDPOINT, headers=headers, json=payload, timeout=60)
                    http_log.append(
                        {
                            "page": page,
                            "http_status": response.status_code,
                            "status": "ok" if response.status_code == 200 else "error",
                        }
                    )
                    if response.status_code in {403, 429}:
                        error = f"aborted_http_{response.status_code}"
                        page_errors += 1
                        with contextlib.suppress(Exception):
                            retry_after_seconds = int(response.headers.get("Retry-After", "0")) or None
                        break
                    if response.status_code != 200:
                        error = f"http_{response.status_code}"
                        page_errors += 1
                        break
                    page_listings = response.json().get("listings") or []
                    raw_listings.extend(page_listings)
                    pages_fetched += 1
                    last_page_size = len(page_listings)
                    if last_page_size < PAGE_SIZE:
                        hit_cap = False  # reached a natural terminal page within the cap
                        break
        except Exception as exc:
            error = str(exc)
            page_errors += 1

        # Prove termination when the last fetched page is full.
        if error is None and pages_fetched > 0 and last_page_size >= PAGE_SIZE:
            try:
                payload["paging"]["from"] = pages_fetched * PAGE_SIZE
                response = requests.post(daft._ENDPOINT, headers=headers, json=payload, timeout=60)
                http_log.append(
                    {
                        "page": pages_fetched,
                        "http_status": response.status_code,
                        "status": "confirmation",
                    }
                )
                if response.status_code == 200:
                    confirm_listings = response.json().get("listings") or []
                    last_page_size = len(confirm_listings)
                    if confirm_listings:
                        raw_listings.extend(confirm_listings)
                        pages_fetched += 1
                else:
                    page_errors += 1
                    error = f"confirmation_http_{response.status_code}"
            except Exception as exc:
                page_errors += 1
                error = str(exc)

        expanded_raw, expansion_failures = expand_grouped_listings(raw_listings)
        mapped: list[Listing] = []
        mapping_failures = len(expansion_failures)
        if expansion_failures:
            event.add_hop(
                "listing_expansion_failures",
                {
                    "search_name": search_cfg.name,
                    "failure_count": len(expansion_failures),
                    "failures": expansion_failures[:10],
                },
            )
        for raw in expanded_raw:
            try:
                mapped.append(_map_listing(DaftListing(raw), search_cfg.name))
            except Exception:
                mapping_failures += 1

        if mapping_failures:
            page_errors += mapping_failures

        complete = False
        if is_deep and error is None and mapping_failures == 0:
            complete = assess_deep_scan_complete(
                pages_fetched=pages_fetched,
                last_page_size=last_page_size,
                page_errors=page_errors,
                results_count=len(mapped),
                previous_complete_count=previous_complete_count,
                hit_max_pages_cap=hit_cap and last_page_size >= PAGE_SIZE,
            )

        event.add_hop(
            "daft_search",
            {
                "search_name": search_cfg.name,
                "fetched_count": len(mapped),
                "pages_fetched": pages_fetched,
                "is_deep": is_deep,
                "complete": complete,
                "error": error,
                "http": http_log,
                "retry_after_seconds": retry_after_seconds,
            },
        )
        event.add_search(search_cfg.name)
        event.increment("total_listings_fetched", len(mapped))
        if error:
            event.add_error(
                "search_failed" if not is_deep else "deep_scan_failed",
                {"search_name": search_cfg.name, "error": error},
            )

        return SearchRunResult(
            listings=mapped,
            search_name=search_cfg.name,
            pages_fetched=pages_fetched,
            results_count=len(mapped),
            complete=complete,
            is_deep=is_deep,
            error=error,
            last_page_size=last_page_size,
            page_errors=page_errors,
            hit_max_pages_cap=hit_cap and last_page_size >= PAGE_SIZE,
            http=http_log,
            mapping_failures=mapping_failures,
            retry_after_seconds=retry_after_seconds,
        )

    def run_all(
        self,
        searches: list[SearchConfig],
        event: WideEvent,
        *,
        deep_search: SearchConfig | None = None,
        baseline_search: SearchConfig | None = None,
        app_config: AppConfig | None = None,
        previous_complete_counts: dict[str, int] | None = None,
    ) -> list[SearchRunResult]:
        """Run shallow searches for all, optionally replacing one with a deep
        scan and one with a baseline (seeding) scan."""
        from daft_monitor.search_identity import resolved_search_id

        results: list[SearchRunResult] = []
        previous_complete_counts = previous_complete_counts or {}
        deep_id = resolved_search_id(deep_search) if deep_search is not None else None
        baseline_id = resolved_search_id(baseline_search) if baseline_search is not None else None
        max_deep_pages = app_config.deep_scan_max_pages if app_config else None
        jitter = app_config.deep_scan_page_jitter_seconds if app_config else (1.0, 3.0)

        for search_cfg in searches:
            search_id = resolved_search_id(search_cfg)
            try:
                if deep_id is not None and search_id == deep_id:
                    run = self.run_search(
                        search_cfg,
                        event,
                        is_deep=True,
                        max_pages=max_deep_pages,
                        previous_complete_count=previous_complete_counts.get(search_id),
                        page_jitter_seconds=jitter,
                    )
                    run.run_kind = "deep"
                elif baseline_id is not None and search_id == baseline_id:
                    # Baselines paginate fully like a deep scan but never
                    # authorize removals; they only establish the seed stock.
                    run = self.run_search(
                        search_cfg,
                        event,
                        is_deep=True,
                        max_pages=max_deep_pages,
                        previous_complete_count=None,
                        page_jitter_seconds=jitter,
                    )
                    run.run_kind = "baseline"
                    run.is_deep = False
                else:
                    run = self.run_search(
                        search_cfg,
                        event,
                        is_deep=False,
                        max_pages=effective_shallow_pages(search_cfg),
                    )
                results.append(run)
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
                results.append(
                    SearchRunResult(
                        listings=[],
                        search_name=search_cfg.name,
                        error=str(exc),
                        is_deep=(deep_id == search_id),
                        complete=False,
                    )
                )
        return results
