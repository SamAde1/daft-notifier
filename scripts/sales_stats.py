#!/usr/bin/env python3
"""
Analyse residential sale listings from the Daft monitor SQLite database.

Focus areas:
- Asking price snapshot (overall, by search pool, by county)
- Lifecycle events in a daily/weekly window (new, price changes, removals, relistings)
- Time-on-market for removed listings
- Price reductions and distance relationship
"""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daft_monitor.constants import COUNTY_MATCH_ORDER, EVENT_NEW, EVENT_PRICE_CHANGE, EVENT_RELISTED, EVENT_REMOVED
from daft_monitor.logging_setup import parse_bool


@dataclass(slots=True)
class SnapshotListing:
    listing_id: str
    search_name: str
    location: str
    bedrooms: str | None
    price_text: str
    distance_to_location: float | None


@dataclass(slots=True)
class EventRow:
    listing_id: str
    search_name: str
    event_type: str
    timestamp: str
    old_value: str | None
    new_value: str | None
    first_seen: str
    listing_price_text: str


def parse_sale_price(price_str: str | None) -> tuple[float | None, str]:
    """Parse a sale asking price string into an EUR float."""
    if not price_str:
        return None, "unparseable"
    normalized = str(price_str).strip()
    lowered = normalized.lower()
    if not normalized or "price on application" in lowered or lowered == "poa":
        return None, "unparseable"

    source = "guide_price" if lowered.startswith("from ") else "sale_price"
    match = re.search(r"(\d[\d,]*)", normalized)
    if not match:
        return None, "unparseable"
    try:
        return float(match.group(1).replace(",", "")), source
    except ValueError:
        return None, "unparseable"


def extract_county(location: str) -> str:
    lowered = location.lower()
    for county in COUNTY_MATCH_ORDER:
        if county.lower() in lowered:
            return county
    return "Other"


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _pool_name(search_name: str, combine_beds: bool) -> str:
    return "Combined 3/4 Bed" if combine_beds else search_name


def _fmt_eur(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"EUR{value:,.0f}"


def _bed_slug(search_name: str) -> str:
    low = search_name.lower()
    if "3-bed" in low or "3 bed" in low:
        return "3bed"
    if "4-bed" in low or "4 bed" in low:
        return "4bed"
    return re.sub(r"[^a-z0-9]+", "_", low).strip("_")[:30]


def _sort_counties_for_output(counties: list[str] | set[str]) -> list[str]:
    without_other = sorted([county for county in counties if county != "Other"])
    if "Other" in counties:
        without_other.append("Other")
    return without_other


def _summary_prices(values: list[float]) -> dict[str, float]:
    values_sorted = sorted(values)
    if len(values_sorted) == 1:
        only = values_sorted[0]
        return {
            "median": only,
            "mean": only,
            "min": only,
            "max": only,
            "p10": only,
            "p25": only,
            "p75": only,
            "p90": only,
        }

    deciles = statistics.quantiles(values_sorted, n=10, method="inclusive")
    quartiles = statistics.quantiles(values_sorted, n=4, method="inclusive")
    return {
        "median": statistics.median(values_sorted),
        "mean": statistics.mean(values_sorted),
        "min": min(values_sorted),
        "max": max(values_sorted),
        "p10": deciles[0],
        "p25": quartiles[0],
        "p75": quartiles[2],
        "p90": deciles[8],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyse residential sale listing data from listings.db")
    parser.add_argument("--data-dir", default="./data-sales", help="Data directory containing listings.db.")
    parser.add_argument("--window", type=int, default=7, help="Window in days (1 for daily, 7 for weekly).")
    parser.add_argument("--budget", type=float, default=450000, help="Budget ceiling in EUR.")
    parser.add_argument(
        "--combine-beds",
        action="store_true",
        help="Combine 3-bed and 4-bed searches into one pool.",
    )
    parser.add_argument(
        "--generate-image",
        nargs="?",
        const=True,
        default=False,
        type=parse_bool,
        help="Generate a PNG chart in reports/ (default: false).",
    )
    return parser.parse_args()


def _load_active_listings(conn: sqlite3.Connection) -> list[SnapshotListing]:
    rows = conn.execute(
        """
        SELECT id, search_name, location, bedrooms, COALESCE(last_price, price) AS price_text, distance_to_location
        FROM listings
        WHERE is_active = 1
        """
    ).fetchall()
    return [
        SnapshotListing(
            listing_id=str(row["id"]),
            search_name=str(row["search_name"]),
            location=str(row["location"]),
            bedrooms=str(row["bedrooms"]) if row["bedrooms"] is not None else None,
            price_text=str(row["price_text"]),
            distance_to_location=float(row["distance_to_location"])
            if row["distance_to_location"] is not None
            else None,
        )
        for row in rows
    ]


def _load_window_events(conn: sqlite3.Connection, cutoff_iso: str) -> list[EventRow]:
    rows = conn.execute(
        """
        SELECT
            e.listing_id,
            e.event_type,
            e.timestamp,
            e.old_value,
            e.new_value,
            l.search_name,
            l.first_seen,
            COALESCE(l.last_price, l.price) AS listing_price_text
        FROM listing_events e
        JOIN listings l ON l.id = e.listing_id
        WHERE e.timestamp >= ?
        ORDER BY e.timestamp ASC
        """,
        (cutoff_iso,),
    ).fetchall()
    return [
        EventRow(
            listing_id=str(row["listing_id"]),
            search_name=str(row["search_name"]),
            event_type=str(row["event_type"]),
            timestamp=str(row["timestamp"]),
            old_value=str(row["old_value"]) if row["old_value"] is not None else None,
            new_value=str(row["new_value"]) if row["new_value"] is not None else None,
            first_seen=str(row["first_seen"]),
            listing_price_text=str(row["listing_price_text"]),
        )
        for row in rows
    ]


def run_sales_stats(
    *,
    data_dir: str,
    window_days: int,
    budget: float,
    combine_beds: bool,
    generate_image: bool,
) -> None:
    db_path = Path(data_dir) / "listings.db"
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return

    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        active_listings = _load_active_listings(conn)
        window_events = _load_window_events(conn, cutoff_iso)
    finally:
        conn.close()

    if not active_listings:
        print("No active listings found.")
        print(f"Window events found (last {window_days} day(s)): {len(window_events)}")
        return

    pool_prices: dict[str, list[float]] = defaultdict(list)
    pool_budget_hits: dict[str, int] = defaultdict(int)
    pool_counties: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    distance_and_price: list[tuple[float, float]] = []
    parse_sources: dict[str, int] = defaultdict(int)
    regional_prices: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    regional_distances: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

    for listing in active_listings:
        pool = _pool_name(listing.search_name, combine_beds)
        price, source = parse_sale_price(listing.price_text)
        parse_sources[source] += 1
        if price is None:
            continue

        county = extract_county(listing.location)
        pool_prices[pool].append(price)
        if price <= budget:
            pool_budget_hits[pool] += 1
        pool_counties[pool][county].append(price)
        regional_prices[listing.search_name][county].append(price)
        if listing.distance_to_location is not None:
            distance_and_price.append((listing.distance_to_location, price))
            regional_distances[listing.search_name][county].append(listing.distance_to_location)

    event_counts_by_pool: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    new_prices_by_pool: dict[str, list[float]] = defaultdict(list)
    reduction_amounts_by_pool: dict[str, list[float]] = defaultdict(list)
    reduction_perc_by_pool: dict[str, list[float]] = defaultdict(list)
    removed_days_on_market_by_pool: dict[str, list[float]] = defaultdict(list)

    for row in window_events:
        pool = _pool_name(row.search_name, combine_beds)
        event_counts_by_pool[pool][row.event_type] += 1

        if row.event_type == EVENT_NEW:
            new_price, _ = parse_sale_price(row.listing_price_text)
            if new_price is not None:
                new_prices_by_pool[pool].append(new_price)

        if row.event_type == EVENT_PRICE_CHANGE:
            old_price, _ = parse_sale_price(row.old_value)
            new_price, _ = parse_sale_price(row.new_value)
            if old_price is not None and new_price is not None:
                delta = new_price - old_price
                if delta < 0:
                    reduction = abs(delta)
                    reduction_amounts_by_pool[pool].append(reduction)
                    if old_price > 0:
                        reduction_perc_by_pool[pool].append((reduction / old_price) * 100.0)

        if row.event_type == EVENT_REMOVED:
            try:
                first_seen = parse_iso(row.first_seen)
                removed_at = parse_iso(row.timestamp)
                days_on_market = (removed_at - first_seen).total_seconds() / 86400.0
                if days_on_market >= 0:
                    removed_days_on_market_by_pool[pool].append(days_on_market)
            except Exception:
                continue

    print("=" * 72)
    print(f"SALE MARKET SNAPSHOT (active listings) | budget={_fmt_eur(budget)}")
    print("=" * 72)
    total_active = sum(len(v) for v in pool_prices.values())
    print(f"Active listings with parseable price: {total_active}")
    print(
        f"Price parse source counts: sale_price={parse_sources.get('sale_price', 0)}, "
        f"guide_price={parse_sources.get('guide_price', 0)}, unparseable={parse_sources.get('unparseable', 0)}"
    )
    print()

    for pool in sorted(pool_prices.keys()):
        prices = pool_prices[pool]
        if not prices:
            continue
        summary = _summary_prices(prices)
        budget_hits = pool_budget_hits.get(pool, 0)
        budget_share = (budget_hits / len(prices)) * 100.0 if prices else 0.0
        print(f"[{pool}]")
        print(f"  Listings: {len(prices)}")
        print(
            f"  Median/Mean: {_fmt_eur(summary['median'])} / {_fmt_eur(summary['mean'])} | "
            f"Min/Max: {_fmt_eur(summary['min'])} / {_fmt_eur(summary['max'])}"
        )
        print(
            "  P10/P25/P75/P90: "
            f"{_fmt_eur(summary['p10'])} / {_fmt_eur(summary['p25'])} / "
            f"{_fmt_eur(summary['p75'])} / {_fmt_eur(summary['p90'])}"
        )
        print(f"  <= budget: {budget_hits}/{len(prices)} ({budget_share:.1f}%)")
        print("  By county:")
        for county in _sort_counties_for_output(set(pool_counties[pool].keys())):
            county_prices = pool_counties[pool].get(county, [])
            if not county_prices:
                continue
            print(f"    - {county}: n={len(county_prices)}, median={_fmt_eur(statistics.median(county_prices))}")
        print()

    print("=" * 72)
    print(f"LIFECYCLE WINDOW METRICS (last {window_days} day(s))")
    print("=" * 72)

    for pool in sorted(pool_prices.keys()):
        counts = event_counts_by_pool.get(pool, {})
        new_count = counts.get(EVENT_NEW, 0)
        price_change_count = counts.get(EVENT_PRICE_CHANGE, 0)
        removed_count = counts.get(EVENT_REMOVED, 0)
        relisted_count = counts.get(EVENT_RELISTED, 0)

        reductions = reduction_amounts_by_pool.get(pool, [])
        reduction_pcts = reduction_perc_by_pool.get(pool, [])
        dom_values = removed_days_on_market_by_pool.get(pool, [])
        window_new_prices = new_prices_by_pool.get(pool, [])

        print(f"[{pool}]")
        print(
            f"  New: {new_count} | Price changes: {price_change_count} | "
            f"Removed: {removed_count} | Relisted: {relisted_count}"
        )
        if window_new_prices:
            print(f"  New listing median asking: {_fmt_eur(statistics.median(window_new_prices))}")
        else:
            print("  New listing median asking: n/a")

        if reductions:
            print(
                f"  Typical reduction: median {_fmt_eur(statistics.median(reductions))}, "
                f"mean {_fmt_eur(statistics.mean(reductions))}, "
                f"largest {_fmt_eur(max(reductions))}"
            )
        else:
            print("  Typical reduction: n/a (no reductions in window)")

        if reduction_pcts:
            print(f"  Typical reduction %: median {statistics.median(reduction_pcts):.2f}%")
        else:
            print("  Typical reduction %: n/a")

        if dom_values:
            print(
                f"  Time on market (removed): median {statistics.median(dom_values):.1f} days, "
                f"mean {statistics.mean(dom_values):.1f} days"
            )
        else:
            print("  Time on market (removed): n/a")
        print()

    if distance_and_price:
        dist_values = [d for d, _ in distance_and_price]
        price_values = [p for _, p in distance_and_price]
        print("=" * 72)
        print("DISTANCE RELATIONSHIP")
        print("=" * 72)
        print(
            f"Distance pairs: {len(distance_and_price)} | "
            f"Median distance: {statistics.median(dist_values):.2f} km | "
            f"Mean distance: {statistics.mean(dist_values):.2f} km"
        )
        if len(distance_and_price) > 1:
            try:
                corr = statistics.correlation(dist_values, price_values)
                if not math.isnan(corr):
                    print(f"Price-distance correlation (r): {corr:.3f}")
            except Exception:
                pass
        print()

    if generate_image:
        from sales_charts import render_regional_chart

        report_dir = Path(__file__).resolve().parent.parent / "reports"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        budget_slug = f"{int(round(budget / 1000.0))}k"

        for sname in sorted(regional_prices):
            slug = _bed_slug(sname)
            regional_path = report_dir / f"sales_regional_{slug}_{budget_slug}_{ts}.png"
            try:
                render_regional_chart(
                    search_name=sname,
                    county_prices=dict(regional_prices[sname]),
                    county_distances=dict(regional_distances.get(sname, {})),
                    budget=budget,
                    output_path=regional_path,
                )
                print(f"Saved regional chart: {regional_path}")
            except Exception as exc:
                print(f"Regional chart generation skipped ({sname}): {exc}")


def main() -> None:
    args = parse_args()
    run_sales_stats(
        data_dir=args.data_dir,
        window_days=args.window,
        budget=args.budget,
        combine_beds=args.combine_beds,
        generate_image=args.generate_image,
    )


if __name__ == "__main__":
    main()
