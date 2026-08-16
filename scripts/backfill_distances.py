#!/usr/bin/env python3
"""Backfill distance_to_location using OSRM for listings that already have coordinates.

Designed for overnight catch-up after observation seeds (which skip OSRM):
  - --limit caps how many listings are processed per run
  - --delay sleeps between OSRM batch requests (~1 req/s by default)
"""

from __future__ import annotations

import argparse
import sqlite3

from daft_monitor.config import load_config
from daft_monitor.distance import fetch_distances_batch_km
from daft_monitor.storage import Storage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill listing distance_to_location with OSRM.")
    parser.add_argument("--config", default="config.yaml", help="Path to config file.")
    parser.add_argument("--dry-run", action="store_true", help="Calculate but do not write to database.")
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Max listings to process this run (default: 200). Use 0 for no cap.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds to sleep between OSRM batch requests (default: 1.0).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Destinations per OSRM table request (default: 50).",
    )
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
    if args.limit < 0:
        raise SystemExit("--limit must be >= 0 (0 = no cap)")
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be >= 1")
    if args.delay < 0:
        raise SystemExit("--delay must be >= 0")

    storage = Storage(config.data_dir)
    conn = sqlite3.connect(str(storage.db_path))
    conn.row_factory = sqlite3.Row
    query = """
        SELECT id, latitude, longitude
        FROM listings
        WHERE latitude IS NOT NULL
          AND longitude IS NOT NULL
          AND distance_to_location IS NULL
        ORDER BY first_seen ASC
    """
    if args.limit > 0:
        rows = conn.execute(f"{query} LIMIT ?", (args.limit,)).fetchall()
    else:
        rows = conn.execute(query).fetchall()
    remaining = conn.execute(
        """
        SELECT COUNT(1) AS count
        FROM listings
        WHERE latitude IS NOT NULL
          AND longitude IS NOT NULL
          AND distance_to_location IS NULL
        """
    ).fetchone()
    conn.close()
    if not rows:
        print("No listings need distance backfill.")
        storage.close()
        return

    destinations = [(str(r["id"]), float(r["latitude"]), float(r["longitude"])) for r in rows]
    total_missing = int(remaining["count"]) if remaining is not None else len(destinations)
    print(
        f"Processing {len(destinations)} listing(s) "
        f"(missing before run: {total_missing}; limit={args.limit or 'none'}; "
        f"delay={args.delay}s; batch_size={args.batch_size})."
    )

    distances = fetch_distances_batch_km(
        origin_lat=float(config.location_latitude),
        origin_lng=float(config.location_longitude),
        destinations=destinations,
        max_batch_size=args.batch_size,
        delay_seconds=args.delay,
    )
    if args.dry_run:
        print(f"[DRY RUN] Would update {len(distances)} listing(s).")
        storage.close()
        return

    updated = storage.update_distances(distances)
    storage.close()
    print(f"Updated: {updated}")
    print(f"Still missing after this run (approx): {max(0, total_missing - updated)}")


if __name__ == "__main__":
    main()
