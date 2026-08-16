#!/usr/bin/env python3
"""Check whether Dublin NEW_HOMES IDs already appear in RESIDENTIAL_SALE."""

from __future__ import annotations

import random
import time

import requests
from daftlistings import Daft

from daft_monitor.config import SearchConfig
from daft_monitor.lifecycle import PAGE_SIZE
from daft_monitor.listing_expand import expand_grouped_listings
from daft_monitor.searcher import Searcher
from daft_monitor.wide_event import WideEvent


def ids_from(search_type: str, location: str, max_pages: int, sleep: float) -> tuple[set[str], int, int]:
    searcher = Searcher()
    cfg = SearchConfig(
        name=f"{location} {search_type}",
        search_type=search_type,
        location=[location],
        sort_type="PUBLISH_DATE_DESC",
        notify=False,
        deep_scan=True,
        shallow_pages=2,
    )
    daft = searcher._configure_daft(cfg, WideEvent("overlap", True, 120, "dev"))
    ids: set[str] = set()
    pages = 0
    total = 0
    for page in range(max_pages):
        if page:
            time.sleep(random.uniform(sleep, sleep + 1.0))
        payload = daft._make_payload()
        payload.setdefault("paging", {})
        payload["paging"]["from"] = page * PAGE_SIZE
        response = requests.post(daft._ENDPOINT, headers=dict(Daft._HEADER), json=payload, timeout=60)
        if response.status_code != 200:
            raise SystemExit(f"{search_type} page {page} HTTP {response.status_code}")
        body = response.json()
        raw = body.get("listings") or []
        if page == 0:
            total = int((body.get("paging") or {}).get("totalResults") or 0)
        expanded, _failures = expand_grouped_listings(raw)
        for entry in expanded:
            listing = entry.get("listing") if isinstance(entry, dict) else None
            if isinstance(listing, dict) and listing.get("id") is not None:
                ids.add(str(listing["id"]))
        pages += 1
        print(f"  {search_type} page {page + 1}: +{len(raw)} raw, running unique={len(ids)}", flush=True)
        if len(raw) < PAGE_SIZE:
            break
    return ids, pages, total


def main() -> int:
    print("Collecting Dublin NEW_HOMES IDs ...", flush=True)
    new_ids, new_pages, new_total = ids_from("NEW_HOMES", "Dublin", 10, 1.2)
    print(f"NEW_HOMES: daft_total={new_total} pages={new_pages} expanded_ids={len(new_ids)}")

    print("Scanning Dublin RESIDENTIAL_SALE for those IDs ...", flush=True)
    sale_ids, sale_pages, sale_total = ids_from("RESIDENTIAL_SALE", "Dublin", 90, 1.2)
    overlap = new_ids & sale_ids
    missing = new_ids - sale_ids
    print()
    print(f"RESIDENTIAL_SALE: daft_total={sale_total} pages={sale_pages} expanded_ids={len(sale_ids)}")
    print(f"NEW_HOMES units also in sale: {len(overlap)} / {len(new_ids)}")
    print(f"NEW_HOMES units only in new-homes: {len(missing)}")
    if missing:
        print("sample missing IDs:", ", ".join(sorted(missing)[:12]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
