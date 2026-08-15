"""Stage 6: in-app weekly digest (hardened per-search membership aggregation)."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daft_monitor.constants import (
    COUNTY_MATCH_ORDER,
    EVENT_NEW,
    EVENT_PRICE_CHANGE,
    EVENT_RELISTED,
    EVENT_REMOVED,
    EVENT_SEED,
)


def _fmt_eur(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"EUR{value:,.0f}"


def extract_county(location: str) -> str:
    lowered = location.lower()
    for county in COUNTY_MATCH_ORDER:
        if county.lower() in lowered:
            return county
    return "Other"


@dataclass(slots=True)
class DigestResult:
    text: str
    window_days: int
    had_active_listings: bool
    new_count: int
    removed_count: int
    relisted_count: int
    price_change_count: int


def _effective_snapshot_price(
    price_value: float | None,
    price_period: str | None,
    price_monthly_eq: float | None,
) -> float | None:
    if price_period == "sale":
        return float(price_value) if price_value is not None else None
    if price_monthly_eq is not None:
        return float(price_monthly_eq)
    if price_value is not None:
        return float(price_value)
    return None


def _get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row is not None else None


def _fetch_active_membership_prices(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            ls.listing_id AS listing_id,
            s.name AS search_name,
            l.location,
            l.price_value,
            l.price_period,
            l.price_monthly_eq
        FROM listing_search_state ls
        JOIN searches s ON s.search_id = ls.search_id
        JOIN listings l ON l.id = ls.listing_id
        WHERE ls.is_active = 1
        """
    ).fetchall()


def _fetch_membership_events(conn: sqlite3.Connection, cutoff_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            e.listing_id,
            e.event_type,
            e.timestamp,
            e.title,
            e.price_value,
            e.price_period,
            e.price_monthly_eq,
            e.old_value,
            e.new_value,
            s.name AS search_name
        FROM listing_search_events e
        JOIN searches s ON s.search_id = e.search_id
        WHERE e.timestamp >= ?
        ORDER BY e.timestamp DESC
        """,
        (cutoff_iso,),
    ).fetchall()


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def build_digest(
    *,
    data_dir: str,
    window_days: int = 7,
    top_price_cuts: int = 3,
    timezone_name: str | None = None,
) -> DigestResult | None:
    db_path = Path(data_dir) / "listings.db"
    if not db_path.exists():
        return None

    if timezone_name:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(timezone_name)
        now_local = datetime.now(tz)
        cutoff = (now_local - timedelta(days=window_days)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    # Compare in UTC: event timestamps are stored as UTC ISO strings and the
    # window filter is a string comparison.
    cutoff_iso = cutoff.astimezone(timezone.utc).isoformat()

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        v2_started_at = _get_meta(conn, "lifecycle_v2_started_at")
        analytics_v2_started_at = _get_meta(conn, "analytics_v2_started_at")
        active_rows = _fetch_active_membership_prices(conn)
        event_rows = _fetch_membership_events(conn, cutoff_iso)
    finally:
        conn.close()

    pool_prices: dict[str, list[float]] = defaultdict(list)
    county_prices: dict[str, list[float]] = defaultdict(list)
    county_seen: dict[str, set[str]] = defaultdict(set)
    total_active_priced = 0

    for row in active_rows:
        price = _effective_snapshot_price(
            row["price_value"], row["price_period"], row["price_monthly_eq"]
        )
        if price is None:
            continue
        total_active_priced += 1
        pool = str(row["search_name"])
        pool_prices[pool].append(price)
        county = extract_county(str(row["location"]))
        # Dedupe county medians by listing: a listing matched by two broad
        # searches would otherwise double-weight the county sample.
        if str(row["listing_id"]) not in county_seen[county]:
            county_seen[county].add(str(row["listing_id"]))
            county_prices[county].append(price)

    counts: dict[str, int] = defaultdict(int)
    pool_new_counts: dict[str, int] = defaultdict(int)
    pool_removed_counts: dict[str, int] = defaultdict(int)
    pool_seed_counts: dict[str, int] = defaultdict(int)
    cut_rows: list[tuple[float, float, float, str]] = []
    seen_price_changes: set[tuple[str, str, str]] = set()
    skipped_legacy = 0

    from daft_monitor.price_parser import parse_price_fields

    for row in event_rows:
        event_type = str(row["event_type"])
        timestamp = str(row["timestamp"])
        if analytics_v2_started_at is not None and timestamp < analytics_v2_started_at:
            # Membership events only exist post-migration; this filter guards
            # against any backfilled/legacy rows predating the trusted epoch.
            skipped_legacy += 1
            continue
        if (
            event_type in {EVENT_REMOVED, EVENT_RELISTED}
            and v2_started_at is not None
            and timestamp < v2_started_at
        ):
            skipped_legacy += 1
            continue

        pool = str(row["search_name"])
        if event_type == EVENT_SEED:
            counts[EVENT_SEED] += 1
            pool_seed_counts[pool] += 1
        elif event_type == EVENT_NEW:
            counts[EVENT_NEW] += 1
            pool_new_counts[pool] += 1
        elif event_type == EVENT_RELISTED:
            counts[EVENT_RELISTED] += 1
            pool_new_counts[pool] += 1
        elif event_type == EVENT_REMOVED:
            counts[EVENT_REMOVED] += 1
            pool_removed_counts[pool] += 1
        elif event_type == EVENT_PRICE_CHANGE:
            old_raw = str(row["old_value"]) if row["old_value"] else ""
            new_raw = str(row["new_value"]) if row["new_value"] else ""
            key = (str(row["listing_id"]), old_raw, new_raw)
            if key in seen_price_changes:
                continue  # one listing counted once despite multi-search membership
            seen_price_changes.add(key)
            counts[EVENT_PRICE_CHANGE] += 1
            old_value, old_period, old_monthly = parse_price_fields(old_raw or None)
            new_value, new_period, new_monthly = parse_price_fields(new_raw or None)
            old_price = _effective_snapshot_price(old_value, old_period, old_monthly)
            new_price = _effective_snapshot_price(new_value, new_period, new_monthly)
            if old_price is None or new_price is None or new_price >= old_price:
                continue
            reduction = old_price - new_price
            cut_rows.append((reduction, old_price, new_price, str(row["title"])))

    cut_rows.sort(key=lambda item: item[0], reverse=True)
    top_cuts = cut_rows[:top_price_cuts]

    supply_added = counts[EVENT_NEW] + counts[EVENT_RELISTED]
    supply_removed = counts[EVENT_REMOVED]
    net_supply = supply_added - supply_removed
    baseline_seed = counts[EVENT_SEED]

    lines: list[str] = []
    label = "Daily" if window_days == 1 else "Weekly"
    lines.append(f"Market Digest - {label} (last {window_days} day(s), since {cutoff.date().isoformat()})")
    lines.append("")
    lines.append("ACTIVITY")
    lines.append(
        f"  New: {supply_added} | Removed: {supply_removed} | "
        f"Relisted: {counts[EVENT_RELISTED]} | Price changes: {counts[EVENT_PRICE_CHANGE]}"
    )
    if baseline_seed:
        lines.append(f"  Baseline seed inventory (excluded from new supply): {baseline_seed}")
    if skipped_legacy:
        lines.append(f"  ({skipped_legacy} pre-lifecycle-v2 removal/relist events excluded as unreliable)")
    pools = sorted(set(pool_new_counts) | set(pool_removed_counts) | set(pool_seed_counts))
    if pools:
        lines.append("  By search:")
        for pool in pools:
            lines.append(
                f"    - {pool}: new={pool_new_counts.get(pool, 0)}, "
                f"removed={pool_removed_counts.get(pool, 0)}, seed={pool_seed_counts.get(pool, 0)}"
            )

    lines.append("")
    lines.append("ACTIVE SNAPSHOT")
    if total_active_priced == 0:
        lines.append("  No active memberships with a parseable price.")
    else:
        lines.append(f"  Active memberships with parsed price: {total_active_priced}")
        for pool in sorted(pool_prices):
            prices = pool_prices[pool]
            lines.append(f"    - {pool}: n={len(prices)}, median={_fmt_eur(_median(prices))}")
        if county_prices:
            lines.append("  Median asking by county:")
            counties = sorted(
                [county for county in county_prices if county != "Other"],
                key=lambda county: -len(county_prices[county]),
            )
            if "Other" in county_prices:
                counties.append("Other")
            for county in counties:
                values = county_prices.get(county, [])
                if not values:
                    continue
                lines.append(f"    - {county}: n={len(values)}, median={_fmt_eur(_median(values))}")

    if top_cuts:
        lines.append("")
        lines.append("BIGGEST PRICE CUTS")
        for reduction, old_price, new_price, title in top_cuts:
            title_short = title[:48] + ("..." if len(title) > 48 else "")
            pct = (reduction / old_price * 100.0) if old_price else 0.0
            lines.append(
                f"  - {title_short}: {_fmt_eur(old_price)} -> {_fmt_eur(new_price)} "
                f"(-{_fmt_eur(reduction)}, {pct:.1f}%)"
            )

    lines.append("")
    lines.append("TIGHTNESS")
    if net_supply > 0:
        direction = f"loosening (+{net_supply} net new listings this window)"
    elif net_supply < 0:
        direction = f"tightening ({net_supply} net listings lost this window)"
    else:
        direction = "steady (new == removed)"
    lines.append(f"  Supply direction: {direction}")

    text = "\n".join(lines)
    return DigestResult(
        text=text,
        window_days=window_days,
        had_active_listings=total_active_priced > 0,
        new_count=supply_added,
        removed_count=supply_removed,
        relisted_count=counts[EVENT_RELISTED],
        price_change_count=counts[EVENT_PRICE_CHANGE],
    )


_DAY_NAME_TO_WEEKDAY = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def parse_digest_day(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise ValueError(f"digest_day int must be 0-6 (Mon-Sun), got {value}")
    lowered = str(value).strip().lower()
    if lowered in _DAY_NAME_TO_WEEKDAY:
        return _DAY_NAME_TO_WEEKDAY[lowered]
    raise ValueError(f"digest_day must be a day name or 0-6, got {value!r}")


def latest_scheduled_occurrence(
    *, now: datetime, digest_day: int, digest_hour: int
) -> datetime:
    """The most recent scheduled digest time at or before `now`."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    days_back = (now.weekday() - digest_day) % 7
    candidate = (now - timedelta(days=days_back)).replace(
        hour=digest_hour, minute=0, second=0, microsecond=0
    )
    if candidate > now:
        candidate -= timedelta(days=7)
    return candidate


def digest_is_due(
    *,
    now: datetime,
    digest_day: int,
    digest_hour: int,
    last_sent_at_iso: str | None,
    analytics_v2_started_at_iso: str | None = None,
) -> bool:
    """Due when the latest scheduled occurrence has not been delivered yet.

    Catch-up semantics: if the app was down through the scheduled time, the
    digest is sent at the next cycle after restart (last sent is older than
    the latest scheduled occurrence).
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    scheduled = latest_scheduled_occurrence(
        now=now, digest_day=digest_day, digest_hour=digest_hour
    )
    if last_sent_at_iso is None:
        if analytics_v2_started_at_iso is None:
            return False
        analytics_started = datetime.fromisoformat(analytics_v2_started_at_iso)
        if analytics_started.tzinfo is None:
            analytics_started = analytics_started.replace(tzinfo=timezone.utc)
        has_full_window = now >= analytics_started + timedelta(days=7)
        return has_full_window and now >= scheduled
    last_sent = datetime.fromisoformat(last_sent_at_iso)
    if last_sent.tzinfo is None:
        last_sent = last_sent.replace(tzinfo=timezone.utc)
    return last_sent < scheduled
