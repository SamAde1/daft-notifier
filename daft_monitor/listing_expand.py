"""Expand grouped Daft listings (prs/newHome subUnits) into individual rows.

Mirrors daftlistings.daft.Daft.search() post-processing so shallow and deep
paths count the same units. Malformed rows are reported as failures instead of
being silently dropped, so scans that lost listings can never be marked
complete (and therefore can never authorize removals).
"""

from __future__ import annotations

import copy
from typing import Any


def expand_grouped_listings(
    raw_listings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (expanded listing dicts, expansion failure records)."""
    expanded: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index, entry in enumerate(raw_listings):
        listing = entry.get("listing") if isinstance(entry, dict) else None
        if not isinstance(listing, dict):
            failures.append({"index": index, "reason": "missing_listing_object"})
            continue
        listing_id = listing.get("id")

        if "newHome" in listing and isinstance(listing.get("newHome"), dict):
            new_home = listing["newHome"]
            if "subUnits" in new_home:
                listing = dict(listing)
                listing["prs"] = listing.pop("newHome")
                entry = dict(entry)
                entry["listing"] = listing

        listing = entry.get("listing", {})
        prs = listing.get("prs") if isinstance(listing, dict) else None
        sub_units = prs.get("subUnits") if isinstance(prs, dict) else None
        if isinstance(sub_units, list) and sub_units:
            for unit_index, unit in enumerate(sub_units):
                if not isinstance(unit, dict):
                    failures.append(
                        {
                            "index": index,
                            "subunit": unit_index,
                            "listing_id": listing_id,
                            "reason": "invalid_subunit",
                        }
                    )
                    continue
                copy_entry = copy.deepcopy(entry)
                copy_listing = copy_entry["listing"]
                for key, value in unit.items():
                    copy_listing[key] = value
                if copy_listing.get("propertyType") == "Studio":
                    copy_listing["numBedrooms"] = "1 bed"
                expanded.append(copy_entry)
            continue

        if isinstance(listing, dict) and listing.get("propertyType") == "Studio":
            entry = copy.deepcopy(entry)
            entry["listing"]["numBedrooms"] = "1 bed"
        expanded.append(entry)
    return expanded, failures
