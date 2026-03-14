#!/usr/bin/env python3
"""
One-time backfill: populate latitude/longitude for existing listings in the DB.

Runs all searches from config.yaml, matches results by ID against DB rows
that have NULL coordinates, and updates them in place.

Usage:
    python scripts/backfill_coordinates.py
    python scripts/backfill_coordinates.py --config path/to/config.yaml
    python scripts/backfill_coordinates.py --dry-run
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Allow running from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daft_monitor.config import load_config  # noqa: E402
from daft_monitor.searcher import Searcher  # noqa: E402
from daft_monitor.wide_event import WideEvent  # noqa: E402


def _ids_missing_coords(db_path: Path) -> set[str]:
    """Return listing IDs where latitude or longitude is NULL."""
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT id FROM listings WHERE latitude IS NULL OR longitude IS NULL"
    ).fetchall()
    conn.close()
    return {str(r[0]) for r in rows}


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill lat/lng for existing listings.")
    parser.add_argument("--config", default="config.yaml", help="Path to config file.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be updated without writing.")
    args = parser.parse_args()

    config = load_config(args.config)
    data_dir = Path(config.data_dir)
    db_path = data_dir / "listings.db"
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return

    # Ensure the migration has run so columns exist.
    conn = sqlite3.connect(str(db_path))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(listings)").fetchall()}
    if "latitude" not in columns:
        conn.execute("ALTER TABLE listings ADD COLUMN latitude REAL")
    if "longitude" not in columns:
        conn.execute("ALTER TABLE listings ADD COLUMN longitude REAL")
    conn.commit()
    conn.close()

    missing = _ids_missing_coords(db_path)
    if not missing:
        print("All listings already have coordinates. Nothing to do.")
        return

    print(f"Found {len(missing)} listing(s) missing coordinates.")
    print(f"Running {len(config.searches)} search(es) to fetch coordinate data...\n")

    searcher = Searcher()
    event = WideEvent(
        cycle_id="backfill-coords",
        is_seed_run=False,
        check_interval_minutes=0,
        environment="dev",
    )

    all_results = searcher.run_all(config.searches, event)

    updated = 0
    not_found: set[str] = set(missing)

    if not args.dry_run:
        conn = sqlite3.connect(str(db_path))

    for listing in all_results:
        if listing.id not in missing:
            continue
        if listing.latitude is None or listing.longitude is None:
            continue

        not_found.discard(listing.id)
        if args.dry_run:
            print(f"  [DRY RUN] Would update {listing.id}: ({listing.latitude}, {listing.longitude})")
        else:
            conn.execute(
                "UPDATE listings SET latitude = ?, longitude = ? WHERE id = ?",
                (listing.latitude, listing.longitude, listing.id),
            )
        updated += 1

    if not args.dry_run:
        conn.commit()
        conn.close()

    print(f"\nUpdated: {updated}")
    print(f"Still missing (not in current search results): {len(not_found)}")
    if not_found:
        print("  These listings may have been removed from Daft or are outside current search scope.")


if __name__ == "__main__":
    main()
