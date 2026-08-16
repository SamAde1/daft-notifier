"""Deep-scan completeness and grace-period removal helpers."""

from __future__ import annotations

from datetime import datetime, timezone

PAGE_SIZE = 50  # daftlistings default _PAGE_SZ


def assess_deep_scan_complete(
    *,
    pages_fetched: int,
    last_page_size: int,
    page_errors: int,
    results_count: int,
    previous_complete_count: int | None,
    hit_max_pages_cap: bool,
    page_size: int = PAGE_SIZE,
) -> bool:
    """Return True only when a deep scan may authorize removals.

    Requires: zero page errors, a terminal short/empty page, not truncated by
    the safety cap, and (if a prior baseline exists) results >= 50% of that
    baseline. A bad scan can only delay removals, never falsify them.
    """
    if page_errors > 0:
        return False
    if hit_max_pages_cap:
        return False
    if pages_fetched < 1:
        return False
    # Terminal page: fewer listings than a full page (includes empty last page).
    if last_page_size >= page_size:
        return False
    if previous_complete_count is not None and previous_complete_count > 0:
        if results_count < 0.5 * previous_complete_count:
            return False
    return True


def parse_iso(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp)


def hours_since(earlier_iso: str, later_iso: str) -> float:
    earlier = parse_iso(earlier_iso)
    later = parse_iso(later_iso)
    if earlier.tzinfo is None:
        earlier = earlier.replace(tzinfo=timezone.utc)
    if later.tzinfo is None:
        later = later.replace(tzinfo=timezone.utc)
    return (later - earlier).total_seconds() / 3600.0


def membership_eligible_for_removal(
    *,
    membership_last_seen: str,
    deep_scan_finished_at: str,
    now_iso: str,
    grace_hours: float,
) -> bool:
    """True when a complete deep scan finished after last_seen and grace elapsed."""
    # Deep scan must have finished strictly after the membership last_seen.
    if hours_since(membership_last_seen, deep_scan_finished_at) <= 0:
        return False
    return hours_since(membership_last_seen, now_iso) > grace_hours
