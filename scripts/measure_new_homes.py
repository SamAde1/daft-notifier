#!/usr/bin/env python3
"""Probe NEW_HOMES coverage, expansion, overlap with residential sale, and request cost.

Usage:
    python scripts/measure_new_homes.py
"""

from __future__ import annotations

import argparse
import random
import time
from math import ceil

import requests
from daftlistings import Daft

from daft_monitor.config import SearchConfig
from daft_monitor.lifecycle import PAGE_SIZE
from daft_monitor.listing_expand import expand_grouped_listings
from daft_monitor.searcher import Searcher
from daft_monitor.wide_event import WideEvent

COUNTIES = ("Dublin", "Kildare", "Meath", "Wicklow")


def _event() -> WideEvent:
    return WideEvent("new-homes", is_seed_run=True, check_interval_minutes=120, environment="dev")


def _search(name: str, search_type: str, location: list[str]) -> SearchConfig:
    return SearchConfig(
        name=name,
        search_type=search_type,
        location=location,
        sort_type="PUBLISH_DATE_DESC",
        notify=False,
        deep_scan=True,
        shallow_pages=2,
    )


def _post(daft: Daft, page_from: int, timeout: int) -> tuple[int, dict]:
    payload = daft._make_payload()
    payload.setdefault("paging", {})
    payload["paging"]["from"] = page_from
    response = requests.post(daft._ENDPOINT, headers=dict(Daft._HEADER), json=payload, timeout=timeout)
    body = response.json() if response.status_code == 200 else {}
    return response.status_code, body


def _ids_and_groups(raw_listings: list[dict]) -> tuple[set[str], int, int, int]:
    expanded, failures = expand_grouped_listings(raw_listings)
    grouped = 0
    subunits = 0
    ids: set[str] = set()
    for entry in raw_listings:
        listing = entry.get("listing") if isinstance(entry, dict) else None
        if not isinstance(listing, dict):
            continue
        new_home = listing.get("newHome") or listing.get("prs")
        units = new_home.get("subUnits") if isinstance(new_home, dict) else None
        if isinstance(units, list) and units:
            grouped += 1
            subunits += len(units)
    for entry in expanded:
        listing = entry.get("listing") if isinstance(entry, dict) else None
        if isinstance(listing, dict) and listing.get("id") is not None:
            ids.add(str(listing["id"]))
    return ids, grouped, subunits, len(failures)


def paginate(
    searcher: Searcher,
    search_cfg: SearchConfig,
    *,
    timeout: int,
    sleep: float,
    max_pages: int,
) -> dict:
    daft = searcher._configure_daft(search_cfg, _event())
    status, body = _post(daft, 0, timeout)
    if status != 200:
        return {"name": search_cfg.name, "http_status": status, "error": str(body)[:180]}
    raw = list(body.get("listings") or [])
    total = int((body.get("paging") or {}).get("totalResults") or 0)
    pages = 1
    last_size = len(raw)
    pages_needed = ceil(total / PAGE_SIZE) if total else 1
    limit = min(max_pages, pages_needed)
    for page in range(1, limit):
        time.sleep(random.uniform(max(0.5, sleep - 0.5), sleep + 0.5))
        status, body = _post(daft, page * PAGE_SIZE, timeout)
        if status != 200:
            return {
                "name": search_cfg.name,
                "http_status": status,
                "total_results": total,
                "pages_fetched": pages,
                "error": f"page_{page}_http_{status}",
            }
        page_listings = body.get("listings") or []
        raw.extend(page_listings)
        pages += 1
        last_size = len(page_listings)
        if last_size < PAGE_SIZE:
            break
    ids, grouped, subunits, failures = _ids_and_groups(raw)
    return {
        "name": search_cfg.name,
        "search_type": search_cfg.search_type,
        "location": search_cfg.location,
        "http_status": 200,
        "total_results": total,
        "pages_needed": pages_needed,
        "pages_fetched": pages,
        "raw_rows": len(raw),
        "expanded_ids": len(ids),
        "grouped_wrappers": grouped,
        "subunit_count": subunits,
        "expansion_failures": failures,
        "ids": ids,
        "complete": last_size < PAGE_SIZE and pages >= pages_needed or last_size < PAGE_SIZE,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure NEW_HOMES viability.")
    parser.add_argument("--sleep", type=float, default=1.5)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--sale-overlap-pages", type=int, default=8, help="Sale pages fetched for Dublin overlap.")
    args = parser.parse_args()

    searcher = Searcher()
    new_home_rows = []
    sale_rows = []

    print("=== NEW_HOMES first-page totals ===", flush=True)
    for county in COUNTIES:
        cfg = _search(f"{county} NEW_HOMES", "NEW_HOMES", [county])
        print(f"paginating {cfg.name} ...", flush=True)
        row = paginate(searcher, cfg, timeout=args.timeout, sleep=args.sleep, max_pages=40)
        new_home_rows.append(row)
        time.sleep(args.sleep)

    print("Dublin City NEW_HOMES ...", flush=True)
    dublin_city = paginate(
        searcher,
        _search("Dublin City NEW_HOMES", "NEW_HOMES", ["Dublin City"]),
        timeout=args.timeout,
        sleep=args.sleep,
        max_pages=40,
    )
    new_home_rows.append(dublin_city)

    print("\n=== RESIDENTIAL_SALE overlap samples ===", flush=True)
    for county in COUNTIES:
        max_pages = 40 if county != "Dublin" else args.sale_overlap_pages
        cfg = _search(f"{county} RESIDENTIAL_SALE", "RESIDENTIAL_SALE", [county])
        print(f"paginating {cfg.name} (max {max_pages} pages) ...", flush=True)
        row = paginate(searcher, cfg, timeout=args.timeout, sleep=args.sleep, max_pages=max_pages)
        sale_rows.append(row)
        time.sleep(args.sleep)

    print()
    print(f"{'search':<28} {'http':>4} {'daft':>6} {'pages':>5} {'raw':>5} {'units':>6} {'groups':>6} {'subs':>5}")
    print("-" * 78)
    for row in new_home_rows:
        print(
            f"{str(row.get('name')):<28} {str(row.get('http_status')):>4} "
            f"{str(row.get('total_results', '-')):>6} {str(row.get('pages_fetched', '-')):>5} "
            f"{str(row.get('raw_rows', '-')):>5} {str(row.get('expanded_ids', '-')):>6} "
            f"{str(row.get('grouped_wrappers', '-')):>6} {str(row.get('subunit_count', '-')):>5}"
        )
        if row.get("error"):
            print(f"  error: {row['error']}")

    print()
    print("Overlap with RESIDENTIAL_SALE (IDs after grouped-unit expansion):")
    sale_by_county = {str(row["location"][0]): row for row in sale_rows if row.get("http_status") == 200}
    for row in new_home_rows:
        if row.get("http_status") != 200 or "City" in str(row.get("name")):
            continue
        county = row["location"][0]
        sale = sale_by_county.get(county)
        if not sale:
            print(f"  {county}: no sale sample")
            continue
        overlap = row["ids"] & sale["ids"]
        print(
            f"  {county}: {len(overlap)} shared IDs of {len(row['ids'])} new-home units "
            f"vs {len(sale['ids'])} sale units sampled "
            f"(sale Daft total={sale.get('total_results')}, pages={sale.get('pages_fetched')})"
        )

    extra_pages = sum(int(row.get("pages_needed") or 0) for row in new_home_rows if "City" not in str(row.get("name")))
    extra_shallow = 4 * 2
    print()
    print("Request-cost sketch if 4 NEW_HOMES searches join the sales container:")
    print(f"  extra shallow requests per 2-hour cycle: {extra_shallow}")
    print(f"  extra deep-scan pages per day (one search/day each): {extra_pages}")
    print(f"  extra deep-scan time at ~2s/page: ~{extra_pages * 2 / 60:.1f} minutes/day")
    return 0 if all(row.get("http_status") == 200 for row in new_home_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
