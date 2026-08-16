#!/usr/bin/env python3
"""Measure Daft capacity for planned observation searches.

Default mode fetches only page 1 (`totalResults` estimate). `--walk` mode runs
the monitor's deep-search pagination until terminal page/cap so you can size
`deep_scan_max_pages` from actual fetched pages.

Usage:
    python scripts/measure_search_capacity.py
    python scripts/measure_search_capacity.py --sleep 2.5
    python scripts/measure_search_capacity.py --walk --max-pages 200
"""

from __future__ import annotations

import argparse
import json
import time
from math import ceil
from pathlib import Path

import requests
from daftlistings import Daft

from daft_monitor.config import SearchConfig
from daft_monitor.lifecycle import PAGE_SIZE
from daft_monitor.searcher import Searcher
from daft_monitor.wide_event import WideEvent

COUNTIES = ("Dublin", "Kildare", "Meath", "Wicklow")
SEARCH_TYPES = ("SHARING", "RESIDENTIAL_RENT", "RESIDENTIAL_SALE")
DUBLIN_VARIANTS: list[tuple[str, list[str]]] = [
    ("Dublin", ["Dublin"]),
    ("Dublin City", ["Dublin City"]),
    ("Dublin+City", ["Dublin", "Dublin City"]),
]


def _event() -> WideEvent:
    return WideEvent("capacity", is_seed_run=True, check_interval_minutes=30, environment="dev")


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


def measure_one_page0(searcher: Searcher, search_cfg: SearchConfig, timeout: int) -> dict[str, object]:
    daft = searcher._configure_daft(search_cfg, _event())
    payload = daft._make_payload()
    headers = dict(Daft._HEADER)
    started = time.perf_counter()
    response = requests.post(daft._ENDPOINT, headers=headers, json=payload, timeout=timeout)
    elapsed = time.perf_counter() - started
    row: dict[str, object] = {
        "name": search_cfg.name,
        "search_type": search_cfg.search_type,
        "location": search_cfg.location,
        "http_status": response.status_code,
        "elapsed_s": round(elapsed, 2),
    }
    if response.status_code != 200:
        row["error"] = response.text[:180].replace("\n", " ")
        return row
    body = response.json()
    listings = body.get("listings") or []
    paging = body.get("paging") or {}
    total = int(paging.get("totalResults") or 0)
    row["page0_count"] = len(listings)
    row["total_results"] = total
    row["pages_needed"] = ceil(total / PAGE_SIZE) if total else 0
    return row


def measure_one_walk(searcher: Searcher, search_cfg: SearchConfig, max_pages: int) -> dict[str, object]:
    started = time.perf_counter()
    result = searcher.run_search(
        search_cfg,
        _event(),
        is_deep=True,
        max_pages=max_pages,
        previous_complete_count=None,
        page_jitter_seconds=(0.0, 0.0),
    )
    elapsed = time.perf_counter() - started
    return {
        "name": search_cfg.name,
        "search_type": search_cfg.search_type,
        "location": search_cfg.location,
        "mode": "walk",
        "elapsed_s": round(elapsed, 2),
        "results_count": result.results_count,
        "pages_fetched": result.pages_fetched,
        "last_page_size": result.last_page_size,
        "complete": result.complete,
        "error": result.error,
        "mapping_failures": result.mapping_failures,
        "retry_after_seconds": result.retry_after_seconds,
    }


def recommended_cap_from_pages(pages: list[int], headroom: float) -> int:
    largest = max(pages) if pages else 0
    padded = ceil(largest * (1.0 + headroom)) + 2 if largest else 0
    # Keep at least 120 unless measured terminal-page demand is higher.
    padded = max(120, padded)
    return padded


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure Daft observation-search capacity.")
    parser.add_argument("--sleep", type=float, default=2.0, help="Seconds between queries.")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument(
        "--headroom", type=float, default=0.25, help="Extra pages as a fraction of the largest measured page count."
    )
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--walk", action="store_true", help="Walk paginated deep search to terminal page/cap.")
    parser.add_argument("--max-pages", type=int, default=200, help="Hard page cap when --walk is enabled.")
    args = parser.parse_args()

    searcher = Searcher()
    queries: list[SearchConfig] = []
    for search_type in SEARCH_TYPES:
        for label, location in DUBLIN_VARIANTS:
            queries.append(_search(f"{label} {search_type}", search_type, location))
        for county in COUNTIES[1:]:
            queries.append(_search(f"{county} {search_type}", search_type, [county]))

    rows: list[dict[str, object]] = []
    for index, search_cfg in enumerate(queries):
        print(f"[{index + 1}/{len(queries)}] {search_cfg.name} ...", flush=True)
        try:
            row = (
                measure_one_walk(searcher, search_cfg, args.max_pages)
                if args.walk
                else measure_one_page0(searcher, search_cfg, args.timeout)
            )
        except Exception as exc:  # noqa: BLE001 — operational script, report and continue
            row = {
                "name": search_cfg.name,
                "search_type": search_cfg.search_type,
                "location": search_cfg.location,
                "error": str(exc),
            }
        rows.append(row)
        if index < len(queries) - 1:
            time.sleep(args.sleep)

    print()
    if args.walk:
        print(f"{'search':<36} {'results':>8} {'pages':>6} {'last':>6} {'ok':>4}")
        print("-" * 72)
    else:
        print(f"{'search':<36} {'http':>4} {'total':>8} {'pages':>6} {'page0':>6}")
        print("-" * 66)
    measured_pages: list[int] = []
    for row in rows:
        if args.walk:
            results = row.get("results_count")
            pages = row.get("pages_fetched")
            last_page = row.get("last_page_size")
            complete = row.get("complete")
            print(
                f"{str(row['name']):<36} {str(results if results is not None else '-'):>8} "
                f"{str(pages if pages is not None else '-'):>6} "
                f"{str(last_page if last_page is not None else '-'):>6} "
                f"{'yes' if complete else 'no':>4}"
            )
            if isinstance(pages, int):
                measured_pages.append(pages)
        else:
            total = row.get("total_results")
            pages = row.get("pages_needed")
            page0 = row.get("page0_count")
            status = row.get("http_status", "err")
            print(
                f"{str(row['name']):<36} {str(status):>4} "
                f"{str(total if total is not None else '-'):>8} "
                f"{str(pages if pages is not None else '-'):>6} "
                f"{str(page0 if page0 is not None else '-'):>6}"
            )
            if isinstance(pages, int):
                measured_pages.append(pages)
        if row.get("error"):
            print(f"  error: {row['error']}")

    cap = recommended_cap_from_pages(measured_pages, args.headroom)
    largest = max(measured_pages) if measured_pages else 0
    print()
    if args.walk:
        print(f"Largest measured page depth: {largest} pages")
    else:
        print(f"Largest estimated page depth from page0 totals: {largest} pages")
    print(f"Recommended deep_scan_max_pages: {cap}")
    print("Dublin shape check: compare Dublin vs Dublin City vs Dublin+City totals above.")
    print("If Dublin+City is not meaningfully larger than Dublin, use location: [Dublin] only.")

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(
                {
                    "mode": "walk" if args.walk else "page0",
                    "rows": rows,
                    "recommended_deep_scan_max_pages": cap,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Wrote {args.json_out}")
    if args.walk:
        return 0 if all(not row.get("error") and row.get("complete") for row in rows) else 1
    return 0 if all(row.get("http_status") == 200 for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
