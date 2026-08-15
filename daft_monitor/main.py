from __future__ import annotations

import argparse
import logging
import os
import signal
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import FrameType
from typing import Any

from daft_monitor import __version__
from daft_monitor.config import AppConfig, load_config
from daft_monitor.config import SearchConfig
from daft_monitor.constants import (
    EVENT_NEW,
    EVENT_PRICE_CHANGE,
    EVENT_RELISTED,
    EVENT_REMOVED,
    EVENT_SEED,
)
from daft_monitor.distance import fetch_distances_batch_km
from daft_monitor.health import HealthServer
from daft_monitor.logging_setup import (
    LoggingRuntimeConfig,
    parse_bool,
    parse_environment,
    parse_log_level,
    setup_logging,
)
from daft_monitor.digest import build_digest, digest_is_due
from daft_monitor.lifecycle_v2 import hours_since
from daft_monitor.models import Listing, ListingEvent, MembershipTransition
from daft_monitor.notifiers import build_alert_notifiers, build_digest_notifiers, build_error_notifiers
from daft_monitor.notifiers.base import Notifier
from daft_monitor.search_identity import resolved_search_id
from daft_monitor.searcher import SearchRunResult, Searcher
from daft_monitor.storage import Storage
from daft_monitor.wide_event import WideEvent


LOGGER = logging.getLogger("daft_monitor")
_STOP_REQUESTED = False
_DEEP_SCAN_RR_META = "deep_scan_rr_index"
_BASELINE_RR_META = "baseline_rr_index"
_DEEP_SCAN_COOLDOWN_PREFIX = "deep_scan_retry_after:"


def _handle_shutdown_signal(signum: int, _: FrameType | None) -> None:
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    LOGGER.info("shutdown signal=%d status=stopping", signum)


def _register_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)


def _dispatch_error_notifications(
    error_notifiers: list[Notifier],
    environment: str,
    cycle_id: str,
    errors: list[dict[str, Any]],
    event: WideEvent,
) -> None:
    """Send a consolidated error notification for all errors accumulated in a cycle."""
    if not error_notifiers:
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    title = f"Daft Monitor Error — {environment.upper()}"

    body_lines = [
        f"Environment: {environment}",
        f"Cycle: {cycle_id}",
        f"Time: {timestamp}",
        f"Error count: {len(errors)}",
        "",
    ]
    for i, err in enumerate(errors, 1):
        body_lines.append(f"--- Error {i} ---")
        body_lines.append(f"Type: {err.get('message', 'Unknown')}")
        ctx = err.get("context", {})
        if isinstance(ctx, dict):
            for k, v in ctx.items():
                body_lines.append(f"  {k}: {v}")
        body_lines.append("")

    body = "\n".join(body_lines)

    for notifier in error_notifiers:
        try:
            notifier.send_error(title, body, event)
        except Exception:
            LOGGER.error("Failed to send error notification via %s", type(notifier).__name__, exc_info=True)


def _populate_distances_for_listings(config: AppConfig, listings: list[Listing], event: WideEvent) -> None:
    """Populate listing.distance_to_location for listings with coordinates."""
    if not config.distance_to_location:
        return
    if config.location_latitude is None or config.location_longitude is None:
        return
    with_coords = [
        (listing.id, listing.latitude, listing.longitude)
        for listing in listings
        if listing.latitude is not None and listing.longitude is not None
    ]
    if not with_coords:
        event.add_hop("distance_to_location", {"status": "skipped_no_coordinates", "listing_count": 0})
        return
    try:
        distances = fetch_distances_batch_km(
            origin_lat=config.location_latitude,
            origin_lng=config.location_longitude,
            destinations=[(lid, lat, lng) for lid, lat, lng in with_coords],
        )
        for listing in listings:
            listing.distance_to_location = distances.get(listing.id)
        event.add_hop(
            "distance_to_location",
            {
                "status": "ok",
                "candidate_count": len(with_coords),
                "distance_count": len(distances),
                "updated_count": len(distances),
            },
        )
    except Exception as exc:
        event.add_hop(
            "distance_to_location",
            {
                "status": "error",
                "candidate_count": len(with_coords),
                "error": str(exc),
            },
        )
        event.add_error("distance_to_location_failed", {"error": str(exc)})


def _record_listing_events(
    storage: Storage,
    listings: list[Listing],
    event_type: str,
    timestamp: str,
) -> int:
    events = [
        ListingEvent(
            listing_id=listing.id,
            event_type=event_type,
            timestamp=timestamp,
        )
        for listing in listings
    ]
    return storage.insert_events(events)


def _search_by_name(searches: list[SearchConfig]) -> dict[str, SearchConfig]:
    """Map search.name -> SearchConfig. Raises if names are not unique.

    Listings are tagged with search_name, so a duplicate name would silently
    overwrite the first entry and misclassify notify/seed decisions.
    """
    by_name: dict[str, SearchConfig] = {}
    for search in searches:
        if search.name in by_name:
            raise ValueError(
                f"Duplicate search name {search.name!r}. "
                "Each search name must be unique so listings can be mapped back to config."
            )
        by_name[search.name] = search
    return by_name


def _search_by_id(searches: list[SearchConfig]) -> dict[str, SearchConfig]:
    return {resolved_search_id(search): search for search in searches}


def _register_searches(
    storage: Storage,
    searches: list[SearchConfig],
    timestamp: str,
    event: WideEvent,
) -> dict[str, bool]:
    """Register each search and return {search_id: is_seeding}."""
    configured_ids = {resolved_search_id(search) for search in searches}
    retired_ids = storage.get_registered_search_ids() - configured_ids
    if retired_ids:
        retired = storage.retire_search_memberships(retired_ids, timestamp)
        LOGGER.info("retired search_ids=%s memberships=%d", sorted(retired_ids), retired)
        event.add_hop(
            "search_retirement",
            {
                "retired_search_ids": sorted(retired_ids),
                "retired_memberships": retired,
            },
        )

    seed_flags: dict[str, bool] = {}
    for search in searches:
        search_id, fingerprint_changed = storage.register_search(search, timestamp)
        is_seeding = storage.search_is_seeding(search_id)
        seed_flags[search_id] = is_seeding
        if is_seeding:
            reason = "pending_seed" if fingerprint_changed else "pending_baseline"
            LOGGER.info("search_id=%s seeding reason=%s", search_id, reason)
            event.add_hop(
                "search_seed_decision",
                {"search_id": search_id, "needs_seed": True, "reason": reason},
            )
    return seed_flags


def _memberships_from_runs(
    run_results: list[SearchRunResult],
    finished_at_by_search: dict[str, str],
    name_to_search: dict[str, SearchConfig],
) -> list[tuple[str, str, str, Listing]]:
    """Build membership rows with per-search finished_at timestamps."""
    rows: list[tuple[str, str, str, Listing]] = []
    for run in run_results:
        if run.error is not None:
            continue
        search = name_to_search.get(run.search_name)
        if search is None:
            continue
        search_id = resolved_search_id(search)
        timestamp = finished_at_by_search.get(search_id, Listing.now_iso())
        for listing in run.listings:
            rows.append((listing.id, search_id, timestamp, listing))
    return rows


def _dedupe_with_search_context(
    listings: list[Listing],
    name_to_search: dict[str, SearchConfig],
    seed_flags: dict[str, bool],
) -> tuple[list[Listing], set[str]]:
    """Deduplicate by listing id using *all* matching searches for routing.

    When the same listing appears under multiple searches, dict-last-wins
    dedupe alone would mis-route notify/seed based on whichever copy survived.
    Instead:
      - seed_ids: every matching search is seeding
      - representative: prefer a live+notify copy for search_name attribution
    """
    by_id: dict[str, list[Listing]] = {}
    for listing in listings:
        by_id.setdefault(listing.id, []).append(listing)

    representatives: list[Listing] = []
    seed_ids: set[str] = set()

    for listing_id, variants in by_id.items():
        matching: list[tuple[Listing, SearchConfig]] = []
        for variant in variants:
            search = name_to_search.get(variant.search_name)
            if search is not None:
                matching.append((variant, search))

        live_pairs = [
            (variant, search)
            for variant, search in matching
            if not seed_flags.get(resolved_search_id(search), False)
        ]

        if not matching:
            # Orphan listing (unknown search_name): treat as live + notify.
            representatives.append(variants[0])
            continue

        if not live_pairs:
            seed_ids.add(listing_id)
            representatives.append(variants[0])
            continue

        preferred = live_pairs[0][0]
        for variant, search in live_pairs:
            preferred = variant
            if search.notify:
                break
        representatives.append(preferred)

    return representatives, seed_ids


def _deep_scan_cooldown_key(search_id: str) -> str:
    return f"{_DEEP_SCAN_COOLDOWN_PREFIX}{search_id}"


def _is_cooldown_active(storage: Storage, search_id: str, now_iso: str) -> bool:
    retry_at = storage.get_meta(_deep_scan_cooldown_key(search_id))
    if not retry_at:
        return False
    try:
        now_dt = datetime.fromisoformat(now_iso)
        retry_dt = datetime.fromisoformat(retry_at)
    except Exception:
        return False
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    if retry_dt.tzinfo is None:
        retry_dt = retry_dt.replace(tzinfo=timezone.utc)
    return now_dt < retry_dt


def _pick_baseline_search(
    storage: Storage,
    searches: list[SearchConfig],
    now: str,
) -> SearchConfig | None:
    """Round-robin pick at most one search whose baseline seeding is pending.

    Any pending search — deep_scan or not — gets a dedicated complete
    paginated baseline scan before it can alert. This is the durable re-seed
    path for non-deep searches and the uniform "new/changed criteria" path.
    """
    pending = [
        search
        for search in searches
        if (
            storage.search_is_seeding(resolved_search_id(search))
            and not _is_cooldown_active(storage, resolved_search_id(search), now)
        )
    ]
    if not pending:
        return None

    pending_ids = {resolved_search_id(search) for search in pending}
    try:
        start_idx = int(storage.get_meta(_BASELINE_RR_META) or "0")
    except ValueError:
        start_idx = 0
    n = len(pending)
    start_idx = start_idx % n

    chosen = pending[start_idx]
    storage.set_meta(_BASELINE_RR_META, str((start_idx + 1) % n))
    if resolved_search_id(chosen) not in pending_ids:  # pragma: no cover - defensive
        return None
    return chosen


def _pick_deep_scan_search(
    storage: Storage,
    searches: list[SearchConfig],
    config: AppConfig,
    now: str,
) -> SearchConfig | None:
    """Round-robin pick at most one deep_scan=true search that is past its interval.

    Seeding searches are excluded: they are handled by the dedicated baseline
    scan path and only join the deep-scan rotation once their baseline has
    completed.
    """
    eligible = [
        search
        for search in searches
        if (
            search.deep_scan
            and not storage.search_is_seeding(resolved_search_id(search))
            and not _is_cooldown_active(storage, resolved_search_id(search), now)
        )
    ]
    if not eligible:
        return None

    ready: list[SearchConfig] = []
    for search in eligible:
        search_id = resolved_search_id(search)
        latest = storage.get_latest_complete_deep_scan(
            search_id, storage.get_search_fingerprint(search_id)
        )
        if latest is None:
            ready.append(search)
            continue
        finished_at, _ = latest
        if hours_since(finished_at, now) >= config.deep_scan_min_interval_hours:
            ready.append(search)

    if not ready:
        return None

    ready_ids = {resolved_search_id(search) for search in ready}
    try:
        start_idx = int(storage.get_meta(_DEEP_SCAN_RR_META) or "0")
    except ValueError:
        start_idx = 0
    n = len(eligible)
    start_idx = start_idx % n if n else 0

    chosen: SearchConfig | None = None
    chosen_idx = start_idx
    for offset in range(n):
        idx = (start_idx + offset) % n
        candidate = eligible[idx]
        if resolved_search_id(candidate) in ready_ids:
            chosen = candidate
            chosen_idx = idx
            break

    if chosen is None:
        return None

    storage.set_meta(_DEEP_SCAN_RR_META, str((chosen_idx + 1) % n))
    return chosen


def _process_lifecycle(
    storage: Storage,
    config: AppConfig,
    deduped: list[Listing],
    run_results: list[SearchRunResult],
    name_to_search: dict[str, SearchConfig],
    event: WideEvent,
) -> None:
    """Price/last_seen/relist + Stage 3 truthful removals (legacy vs deep_scan)."""
    now = Listing.now_iso()
    current_by_id = {listing.id: listing for listing in deduped}
    current_ids = set(current_by_id.keys())
    active_ids = storage.get_active_listing_ids()
    still_present_ids = active_ids & current_ids

    existing_still_present = storage.get_listings_by_ids(still_present_ids)
    price_changes = 0
    last_price_backfills = 0
    for existing in existing_still_present:
        current = current_by_id.get(existing.id)
        if current is None:
            continue

        if existing.last_price is None:
            storage.update_listing_price(existing.id, current.price, now, bedrooms=current.bedrooms)
            last_price_backfills += 1
            continue

        if current.price != existing.last_price:
            storage.update_listing_price(existing.id, current.price, now, bedrooms=current.bedrooms)
            storage.insert_event(
                ListingEvent(
                    listing_id=existing.id,
                    event_type=EVENT_PRICE_CHANGE,
                    timestamp=now,
                    old_value=existing.last_price,
                    new_value=current.price,
                )
            )
            membership_price_changes = storage.record_membership_price_changes(
                current,
                old_price_raw=existing.last_price,
                new_price_raw=current.price,
                timestamp=now,
            )
            if membership_price_changes:
                event.add_field("lifecycle_membership_price_changes", membership_price_changes)
            price_changes += 1

    last_seen_updates = storage.update_last_seen(still_present_ids, now)

    inactive_candidates = current_ids - active_ids
    existing_inactive = storage.get_listings_by_ids(inactive_candidates)
    relisted_ids = {listing.id for listing in existing_inactive if not listing.is_active}
    relisted_listings = [current_by_id[listing_id] for listing_id in sorted(relisted_ids)]
    relistings = 0
    if relisted_ids:
        storage.mark_listings_active(relisted_ids, now)
        relistings = _record_listing_events(storage, relisted_listings, EVENT_RELISTED, now)

    configured_ids = {resolved_search_id(search) for search in config.searches}
    membership_inactivations = 0
    legacy_removal_searches = 0
    grace_checks = 0

    seen_by_search: dict[str, set[str]] = {}
    ok_by_search: dict[str, bool] = {}
    for run in run_results:
        search = name_to_search.get(run.search_name)
        if search is None:
            continue
        search_id = resolved_search_id(search)
        seen_by_search[search_id] = {listing.id for listing in run.listings}
        ok_by_search[search_id] = run.error is None

    for search in config.searches:
        search_id = resolved_search_id(search)
        if search.deep_scan:
            # Grace path: only the latest COMPLETE deep scan is authoritative.
            # First authoritative absence starts `missing_since`; removal is
            # only allowed once that absence has aged past the grace window.
            grace_checks += 1
            fingerprint = storage.get_search_fingerprint(search_id)
            latest = storage.get_latest_complete_deep_scan_run(search_id, fingerprint)
            if latest is None:
                continue
            authoritative_run_id, finished_at, _ = latest
            authoritative_seen = storage.get_run_listing_ids(authoritative_run_id)
            to_mark_missing: list[str] = []
            to_inactivate: set[str] = set()
            for (
                listing_id,
                _last_seen,
                missing_since,
                first_missing_run_id,
            ) in storage.get_active_membership_rows(search_id):
                if listing_id in authoritative_seen:
                    continue  # present in the authoritative run
                if missing_since is None:
                    to_mark_missing.append(listing_id)
                    continue
                confirmed_by_newer_scan = (
                    first_missing_run_id is not None
                    and authoritative_run_id != first_missing_run_id
                )
                grace_elapsed_at_scan = (
                    hours_since(missing_since, finished_at)
                    >= float(config.removal_grace_hours)
                )
                if confirmed_by_newer_scan and grace_elapsed_at_scan:
                    to_inactivate.add(listing_id)
            if to_mark_missing:
                storage.mark_memberships_missing(
                    search_id, to_mark_missing, finished_at, authoritative_run_id
                )
            if to_inactivate:
                membership_inactivations += storage.mark_memberships_inactive(
                    search_id, to_inactivate, now
                )
        else:
            # Legacy: missing from a successful fetch ⇒ membership inactive.
            if not ok_by_search.get(search_id, False):
                continue
            legacy_removal_searches += 1
            seen_ids = seen_by_search.get(search_id, set())
            active_memberships = {lid for lid, _ in storage.get_active_memberships(search_id)}
            missing = active_memberships - seen_ids
            if missing:
                membership_inactivations += storage.mark_memberships_inactive(
                    search_id, missing, now
                )

    active_after = storage.get_active_listing_ids()
    globally_inactive = storage.listing_ids_inactive_in_all_configured_searches(
        active_after, configured_ids
    )
    removable_ids = globally_inactive

    removals = 0
    if removable_ids:
        removable_rows = storage.get_listings_by_ids(removable_ids)
        # Only mark rows that are still active (filter was on active set).
        storage.mark_listings_removed(removable_ids, now)
        removals = _record_listing_events(storage, removable_rows, EVENT_REMOVED, now)

    event.add_field("lifecycle_price_changes", price_changes)
    event.add_field("lifecycle_removals", removals)
    event.add_field("lifecycle_relistings", relistings)
    event.add_field("lifecycle_last_seen_updates", last_seen_updates)
    event.add_field("lifecycle_backfilled_last_price", last_price_backfills)
    event.add_field("lifecycle_membership_inactivations", membership_inactivations)
    event.add_hop(
        "lifecycle",
        {
            "price_change_count": price_changes,
            "removal_count": removals,
            "relisting_count": relistings,
            "last_seen_updates": last_seen_updates,
            "last_price_backfills": last_price_backfills,
            "membership_inactivations": membership_inactivations,
            "legacy_removal_searches": legacy_removal_searches,
            "grace_checks": grace_checks,
            "orphan_removals": 0,
        },
    )


def _run_cycle(config: AppConfig, storage: Storage, searcher: Searcher, environment: str) -> None:
    cycle_id = str(uuid.uuid4())
    now = Listing.now_iso()
    name_to_search = _search_by_name(config.searches)
    is_global_seed = storage.is_first_run()

    event = WideEvent(
        cycle_id=cycle_id,
        is_seed_run=is_global_seed,
        check_interval_minutes=config.check_interval_minutes,
        environment=environment,
    )
    if storage.ensure_lifecycle_v2_started(now):
        event.add_hop("lifecycle_v2", {"started_at": now})
    if storage.ensure_analytics_v2_started(now):
        event.add_hop("analytics_v2", {"started_at": now})

    seed_flags = _register_searches(storage, config.searches, now, event)
    if any(seed_flags.values()):
        event.add_field("is_seed_run", True)

    baseline_search = _pick_baseline_search(storage, config.searches, now)
    if baseline_search is not None:
        event.add_hop(
            "baseline_scan_selected",
            {
                "search_id": resolved_search_id(baseline_search),
                "search_name": baseline_search.name,
            },
        )
    deep_search = None
    if baseline_search is None:
        deep_search = _pick_deep_scan_search(storage, config.searches, config, now)
        if deep_search is not None:
            event.add_hop(
                "deep_scan_selected",
                {
                    "search_id": resolved_search_id(deep_search),
                    "search_name": deep_search.name,
                },
            )

    previous_complete_counts: dict[str, int] = {}
    for search in config.searches:
        if not search.deep_scan:
            continue
        search_id = resolved_search_id(search)
        fingerprint = storage.get_search_fingerprint(search_id)
        count = storage.get_previous_complete_deep_results_count(search_id, fingerprint)
        if count is not None:
            previous_complete_counts[search_id] = count

    try:
        run_results = searcher.run_all(
            config.searches,
            event,
            deep_search=deep_search,
            baseline_search=baseline_search,
            app_config=config,
            previous_complete_counts=previous_complete_counts,
        )

        finished_at_by_search: dict[str, str] = {}
        seed_clear_candidates: list[tuple[str, str]] = []
        for run in run_results:
            search = name_to_search.get(run.search_name)
            if search is None:
                continue
            search_id = resolved_search_id(search)
            run_finished = Listing.now_iso()
            finished_at_by_search[search_id] = run_finished
            fingerprint = storage.get_search_fingerprint(search_id)
            search_run_id = storage.insert_search_run(
                search_id=search_id,
                started_at=now,
                finished_at=run_finished,
                pages_fetched=run.pages_fetched,
                results_count=run.results_count,
                complete=run.complete,
                is_deep=run.is_deep,
                error=run.error,
                criteria_fingerprint=fingerprint,
                run_kind=run.run_kind,
            )
            storage.insert_search_run_listings(
                search_run_id, (listing.id for listing in run.listings)
            )
            if run.error in {"aborted_http_403", "aborted_http_429"}:
                retry_after = run.retry_after_seconds or 3600
                cooldown_until = (
                    datetime.now(timezone.utc) + timedelta(seconds=retry_after)
                ).isoformat()
                storage.set_meta(_deep_scan_cooldown_key(search_id), cooldown_until)
                event.add_hop(
                    "search_cooldown_set",
                    {
                        "search_id": search_id,
                        "retry_after_seconds": retry_after,
                        "cooldown_until": cooldown_until,
                        "run_kind": run.run_kind,
                        "error": run.error,
                    },
                )
            # Pending seeding is cleared only after this cycle's events have
            # been recorded as seeds; baselines and deep scans both qualify.
            if (
                run.run_kind in {"baseline", "deep"}
                and run.complete
                and run.error is None
                and fingerprint
            ):
                seed_clear_candidates.append((search_id, fingerprint))

        listings = [listing for run in run_results for listing in run.listings]
        successful_searches = [
            run.search_name for run in run_results if run.error is None
        ]
        event.add_field("successful_search_count", len(successful_searches))

        memberships = _memberships_from_runs(run_results, finished_at_by_search, name_to_search)
        id_to_search = _search_by_id(config.searches)

        deduped, seed_ids = _dedupe_with_search_context(
            listings, name_to_search, seed_flags
        )
        event.add_field("deduped_listings_count", len(deduped))
        event.add_hop("dedupe", {"input_count": len(listings), "output_count": len(deduped)})
        event.add_field("lifecycle_price_changes", 0)
        event.add_field("lifecycle_removals", 0)
        event.add_field("lifecycle_relistings", 0)
        event.add_field("lifecycle_last_seen_updates", 0)
        event.add_field("lifecycle_backfilled_last_price", 0)

        # --- Seed path: insert before membership upserts for FK safety ---
        seed_listings = [listing for listing in deduped if listing.id in seed_ids]
        if seed_listings:
            new_seed = storage.filter_new_listings(seed_listings)
            if new_seed:
                inserted = storage.insert_listings(new_seed)
                seed_events = _record_listing_events(storage, new_seed, EVENT_SEED, now)
                event.add_field("seed_inserted_count", inserted)
                event.add_hop(
                    "storage_seed",
                    {
                        "inserted_count": inserted,
                        "seed_events_count": seed_events,
                        "listing_count": len(new_seed),
                    },
                )
            else:
                event.add_hop("storage_seed", {"inserted_count": 0, "listing_count": 0})

        # --- Live path: insert before membership upserts for FK safety ---
        live_candidates = [listing for listing in deduped if listing.id not in seed_ids]
        new_listings = storage.filter_new_listings(live_candidates)
        event.add_field("new_listings_count", len(new_listings))
        event.add_hop(
            "storage_diff",
            {"candidate_count": len(live_candidates), "new_count": len(new_listings)},
        )

        if new_listings:
            _populate_distances_for_listings(config, new_listings, event)

            alert_notifiers = build_alert_notifiers(config, environment)
            listings_to_alert = [
                listing for listing in new_listings if listing.id in alert_listing_ids
            ]
            for listing in listings_to_alert:
                for notifier in alert_notifiers:
                    ok = notifier.send(listing, event)
                    if ok:
                        event.increment("notifications_sent", 1)
                    else:
                        event.increment("notification_errors", 1)
            skipped = len(new_listings) - len(listings_to_alert)
            if skipped:
                event.increment("notifications_skipped_notify_false", skipped)

            inserted = storage.insert_listings(new_listings)
            new_events = _record_listing_events(storage, new_listings, EVENT_NEW, now)
            event.add_hop("storage_insert", {"inserted_count": inserted, "new_events_count": new_events})

        # Membership transitions: state upsert + episode + event in one
        # transaction per cycle, classified against the pre-run seed flags.
        membership_transitions: list[MembershipTransition] = []
        if memberships:
            membership_transitions = storage.apply_search_observations(memberships, seed_flags)
            event.add_hop(
                "listing_search_state",
                {
                    "membership_upserts": len(memberships),
                    "membership_events": sum(
                        1 for t in membership_transitions if t.transition != "unchanged"
                    ),
                    "created": sum(1 for t in membership_transitions if t.transition == "created"),
                    "reactivated": sum(1 for t in membership_transitions if t.transition == "reactivated"),
                },
            )

        # Seed events for this cycle are recorded; pending state may now clear.
        for search_id, fingerprint in seed_clear_candidates:
            storage.clear_pending_seed(search_id, fingerprint)

        alert_listing_ids: set[str] = set()
        for transition in membership_transitions:
            if transition.transition not in {"created", "reactivated"}:
                continue
            search = id_to_search.get(transition.search_id)
            if search is None or not search.notify:
                continue
            if seed_flags.get(transition.search_id, False):
                continue
            alert_listing_ids.add(transition.listing_id)

        if live_candidates:
            alert_notifiers = build_alert_notifiers(config, environment)
            listings_to_alert = [
                listing for listing in live_candidates if listing.id in alert_listing_ids
            ]
            seen_ids: set[str] = set()
            deduped_alerts: list[Listing] = []
            for listing in listings_to_alert:
                if listing.id in seen_ids:
                    continue
                seen_ids.add(listing.id)
                deduped_alerts.append(listing)
            for listing in deduped_alerts:
                for notifier in alert_notifiers:
                    ok = notifier.send(listing, event)
                    if ok:
                        event.increment("notifications_sent", 1)
                    else:
                        event.increment("notification_errors", 1)
            skipped = len(live_candidates) - len(deduped_alerts)
            if skipped:
                event.increment("notifications_skipped_notify_false", skipped)

        # Lifecycle: skip only on a pure first-run seed (empty DB, every search seeding).
        if is_global_seed and all(seed_flags.values()):
            event.add_hop("lifecycle", {"status": "skipped_global_seed"})
        else:
            _process_lifecycle(
                storage, config, deduped, run_results, name_to_search, event
            )
    except Exception as exc:
        event.add_error("cycle_failed", {"error": str(exc)})
    finally:
        event.emit(LOGGER)
        errors = event.payload.get("errors", [])
        if errors:
            error_notifiers = build_error_notifiers(config, environment)
            _dispatch_error_notifications(error_notifiers, environment, cycle_id, errors, event)

    _maybe_send_digest(config, storage, environment)


def _digest_last_sent_meta(environment: str, notifier_name: str) -> str:
    return f"digest_last_sent_at:{environment}:{notifier_name}"


def _maybe_send_digest(config: AppConfig, storage: Storage, environment: str) -> None:
    """Send the weekly market digest if due. Failures never terminate the monitor."""
    if config.digest_day is None:
        return

    digest_notifiers = build_digest_notifiers(config, environment)
    if not digest_notifiers:
        return

    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(config.digest_timezone)
        now = datetime.now(tz)
        due_notifiers = []
        for notifier in digest_notifiers:
            last_sent_at = storage.get_meta(_digest_last_sent_meta(environment, notifier.name))
            analytics_started_at = storage.get_meta("analytics_v2_started_at")
            if digest_is_due(
                now=now,
                digest_day=config.digest_day,
                digest_hour=config.digest_hour,
                last_sent_at_iso=last_sent_at,
                analytics_v2_started_at_iso=analytics_started_at,
            ):
                due_notifiers.append(notifier)
        if not due_notifiers:
            return

        event = WideEvent(
            cycle_id=str(uuid.uuid4()),
            is_seed_run=False,
            check_interval_minutes=config.check_interval_minutes,
            environment=environment,
        )

        result = build_digest(
            data_dir=config.data_dir,
            window_days=7,
            timezone_name=config.digest_timezone,
        )
        if result is None:
            LOGGER.warning("digest: database not found, skipping send")
            return

        today = now.date().isoformat()
        title = f"Daft Market Digest - Weekly - {today}"

        for notifier in due_notifiers:
            try:
                ok = notifier.send_digest(title, result.text, event)
                if ok:
                    storage.set_meta(
                        _digest_last_sent_meta(environment, notifier.name),
                        now.isoformat(),
                    )
                    LOGGER.info(
                        "digest sent via %s new=%d removed=%d relisted=%d price_changes=%d",
                        notifier.name,
                        result.new_count,
                        result.removed_count,
                        result.relisted_count,
                        result.price_change_count,
                    )
                else:
                    LOGGER.warning("digest: notifier %s failed, will retry later", notifier.name)
            except Exception:
                LOGGER.error("digest: notifier %s raised", notifier.name, exc_info=True)

        event.emit(LOGGER)
    except Exception:
        LOGGER.error("digest: unexpected failure, continuing monitor loop", exc_info=True)


def run(config_path: str | None = None, run_once: bool = False) -> None:
    run_with_logging(
        config_path=config_path,
        run_once=run_once,
        environment=os.environ.get("DAFT_MONITOR_ENVIRONMENT", "dev"),
        log_level=os.environ.get("DAFT_MONITOR_LOG_LEVEL", "info"),
        write_logs=parse_bool(os.environ.get("DAFT_MONITOR_WRITE_LOGS", "true")),
        log_dir=os.environ.get("DAFT_MONITOR_LOG_DIR", "./logs"),
    )


def _send_startup_tests(config: AppConfig, environment: str) -> None:
    """Send a test alert and test error on startup so we know notifications work."""
    event = WideEvent(
        cycle_id="startup-test",
        is_seed_run=False,
        check_interval_minutes=config.check_interval_minutes,
        environment=environment,
    )

    # --- Test alert ---
    alert_notifiers = build_alert_notifiers(config, environment)
    if alert_notifiers:
        test_listing = Listing(
            id="STARTUP-TEST",
            title=f"[STARTUP TEST] Daft Monitor — {environment.upper()}",
            price="N/A",
            url="https://www.daft.ie",
            location="Startup test notification",
            bedrooms="N/A",
            image_url=None,
            search_name="startup-test",
            first_seen=Listing.now_iso(),
            latitude=None,
            longitude=None,
        )
        for n in alert_notifiers:
            ok = n.send(test_listing, event)
            LOGGER.info("startup test alert via %s -> sent=%s", type(n).__name__, ok)
    else:
        LOGGER.warning("no alert notifiers for environment=%s — skipping startup test alert", environment)

    # --- Test error ---
    error_notifiers = build_error_notifiers(config, environment)
    if error_notifiers:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        title = f"[STARTUP TEST] Daft Monitor Error — {environment.upper()}"
        body = (
            f"Environment: {environment}\n"
            f"Time: {timestamp}\n"
            f"This is a startup test error notification.\n"
            f"If you see this, error notifications are working correctly."
        )
        for n in error_notifiers:
            ok = n.send_error(title, body, event)
            LOGGER.info("startup test error via %s -> sent=%s", type(n).__name__, ok)
    else:
        LOGGER.warning("no error notifiers for environment=%s — skipping startup test error", environment)


def run_with_logging(
    *,
    config_path: str | None = None,
    run_once: bool = False,
    environment: str = "dev",
    log_level: str = "info",
    write_logs: bool = True,
    log_dir: str = "./logs",
) -> None:
    runtime_logging = LoggingRuntimeConfig(
        environment=parse_environment(environment),
        log_level=parse_log_level(log_level),
        write_logs=write_logs,
        log_dir=log_dir,
    )
    setup_logging(runtime_logging)
    _register_signal_handlers()

    LOGGER.info(
        "startup version=%s environment=%s log_level=%s write_logs=%s log_dir=%s",
        __version__,
        runtime_logging.environment,
        runtime_logging.log_level,
        runtime_logging.write_logs,
        runtime_logging.log_dir,
    )

    health_port = int(os.environ.get("DAFT_MONITOR_HEALTH_PORT", "8080"))
    health_server = HealthServer(port=health_port)
    health_server.start()

    config = load_config(config_path)

    # Send startup test notifications so we know the notifiers are healthy.
    startup_tests_enabled = parse_bool(os.environ.get("DAFT_MONITOR_STARTUP_TEST_NOTIFICATIONS", "true"))
    if startup_tests_enabled:
        _send_startup_tests(config, runtime_logging.environment)
    else:
        LOGGER.info("startup test notifications disabled via DAFT_MONITOR_STARTUP_TEST_NOTIFICATIONS")

    storage = Storage(config.data_dir)
    searcher = Searcher()
    sleep_seconds = config.check_interval_minutes * 60

    try:
        while not _STOP_REQUESTED:
            _run_cycle(config, storage, searcher, runtime_logging.environment)
            if run_once:
                break
            if _STOP_REQUESTED:
                break
            _interruptible_sleep(sleep_seconds, config.check_interval_minutes)
    finally:
        LOGGER.info("shutdown complete")
        health_server.stop()
        storage.close()


def _interruptible_sleep(total_seconds: int, interval_minutes: int) -> None:
    """Sleep in small chunks so signals are handled promptly.

    Logs a 'waiting' message at INFO level with the next run time,
    and a heartbeat at DEBUG level every 60 seconds.
    """
    next_run_local = datetime.now() + timedelta(seconds=total_seconds)
    LOGGER.info(
        "cycle complete — waiting %d min until next run at %s",
        interval_minutes,
        next_run_local.strftime("%H:%M:%S"),
    )
    elapsed = 0
    chunk = 10  # check every 10 seconds for shutdown signal
    while elapsed < total_seconds:
        if _STOP_REQUESTED:
            return
        time.sleep(min(chunk, total_seconds - elapsed))
        elapsed += chunk
        if elapsed % 60 == 0 and elapsed < total_seconds:
            remaining = total_seconds - elapsed
            LOGGER.debug(
                "heartbeat — waiting, %d:%02d remaining",
                remaining // 60,
                remaining % 60,
            )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor Daft listings and send notifications.")
    parser.add_argument(
        "--config", dest="config_path",
        default=os.environ.get("DAFT_MONITOR_CONFIG"),
        help="Path to config.yaml",
    )
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    parser.add_argument(
        "--environment",
        default=os.environ.get("DAFT_MONITOR_ENVIRONMENT", "dev"),
        choices=["dev", "prod"],
        help="Runtime environment label (env: DAFT_MONITOR_ENVIRONMENT).",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("DAFT_MONITOR_LOG_LEVEL", "info"),
        choices=["debug", "info", "error"],
        help="Log verbosity (env: DAFT_MONITOR_LOG_LEVEL).",
    )
    parser.add_argument(
        "--write-logs",
        default=os.environ.get("DAFT_MONITOR_WRITE_LOGS", "true"),
        help="Write logs to files (true/false) (env: DAFT_MONITOR_WRITE_LOGS).",
    )
    parser.add_argument(
        "--log-dir",
        default=os.environ.get("DAFT_MONITOR_LOG_DIR", "./logs"),
        help="Directory for log files (env: DAFT_MONITOR_LOG_DIR).",
    )
    return parser.parse_args()
