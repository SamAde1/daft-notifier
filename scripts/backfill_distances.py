#!/usr/bin/env python3
"""Backfill distance_to_location using OSRM for listings that already have coordinates."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daft_monitor.config import load_config  # noqa: E402
from daft_monitor.distance import fetch_distances_batch_km  # noqa: E402
from daft_monitor.storage import Storage  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill listing distance_to_location with OSRM.")
    parser.add_argument("--config", default="config.yaml", help="Path to config file.")
    parser.add_argument("--dry-run", action="store_true", help="Calculate but do not write to database.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    if not config.distance_to_location:
        print("distance_to_location is false in config. Set it true to run backfill.")
        return
    if config.location_latitude is None or config.location_longitude is None:
        print("location_latitude and location_longitude are required when distance_to_location is true.")
        return

    storage = Storage(config.data_dir)
    conn = sqlite3.connect(str(storage.db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, latitude, longitude
        FROM listings
        WHERE latitude IS NOT NULL
          AND longitude IS NOT NULL
          AND distance_to_location IS NULL
        """
    ).fetchall()
    conn.close()
    if not rows:
        print("No listings need distance backfill.")
        storage.close()
        return

    destinations = [(str(r["id"]), float(r["latitude"]), float(r["longitude"])) for r in rows]
    print(f"Found {len(destinations)} listing(s) missing distance_to_location.")
    distances = fetch_distances_batch_km(
        origin_lat=float(config.location_latitude),
        origin_lng=float(config.location_longitude),
        destinations=destinations,
    )
    if args.dry_run:
        print(f"[DRY RUN] Would update {len(distances)} listing(s).")
        storage.close()
        return

    updated = storage.update_distances(distances)
    storage.close()
    print(f"Updated: {updated}")
    print(f"Still missing: {len(destinations) - len(distances)}")


if __name__ == "__main__":
    main()
