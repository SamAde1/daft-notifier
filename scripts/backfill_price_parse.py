#!/usr/bin/env python3
"""
Backfill parsed price columns and listing_search_state for existing DBs.

What this does (safe / idempotent):
  1. Parses every listing's free-text `price` into:
       price_value, price_period, price_monthly_eq
  2. Creates a listing_search_state row for each listing using its
     current search_name as the search_id (and registers that search
     in the searches table if missing).

Run with the monitor STOPPED against a snapshot/copy first if you prefer.

Usage:
    python scripts/backfill_price_parse.py --data-dir ./data
    python scripts/backfill_price_parse.py --data-dir ./data-sales
    python scripts/backfill_price_parse.py --data-dir ./data --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daft_monitor.models import Listing  # noqa: E402
from daft_monitor.price_parser import parse_price_fields  # noqa: E402
from daft_monitor.storage import Storage  # noqa: E402


def _backfill(data_dir: str, dry_run: bool) -> int:
    storage = Storage(data_dir)
    now = Listing.now_iso()

    rows = storage.conn.execute(
        """
        SELECT id, price, bedrooms, search_name, first_seen, last_seen, is_active,
               price_value, price_period, price_monthly_eq
        FROM listings
        """
    ).fetchall()

    parsed_ok = 0
    parsed_skip = 0
    unparseable: list[tuple[str, str]] = []
    memberships = 0
    searches_registered: set[str] = set()
    episodes_opened = 0

    for row in rows:
        listing_id = str(row["id"])
        price = str(row["price"])
        bedrooms = str(row["bedrooms"]) if row["bedrooms"] is not None else None
        search_name = str(row["search_name"])
        first_seen = str(row["first_seen"])
        last_seen = str(row["last_seen"]) if row["last_seen"] is not None else first_seen
        is_active = 1 if row["is_active"] in (1, True, "1") else 0

        # --- price columns ---
        already = row["price_value"] is not None or row["price_period"] is not None
        if already:
            parsed_skip += 1
        else:
            value, period, monthly = parse_price_fields(price, bedrooms)
            if value is None and period is None:
                unparseable.append((listing_id, price))
            else:
                parsed_ok += 1
            if not dry_run:
                storage.update_parsed_price(listing_id, value, period, monthly)

        # --- search registry + membership ---
        search_id = search_name  # historical rows used name as identity
        if search_id not in searches_registered:
            if not dry_run:
                existing = storage.conn.execute(
                    "SELECT 1 FROM searches WHERE search_id = ?",
                    (search_id,),
                ).fetchone()
                if existing is None:
                    storage.conn.execute(
                        """
                        INSERT INTO searches (search_id, name, criteria_fingerprint, first_registered)
                        VALUES (?, ?, ?, ?)
                        """,
                        (search_id, search_name, "backfill:unknown", now),
                    )
            searches_registered.add(search_id)

        if not dry_run:
            existing_m = storage.conn.execute(
                """
                SELECT 1 FROM listing_search_state
                WHERE listing_id = ? AND search_id = ?
                """,
                (listing_id, search_id),
            ).fetchone()
            if existing_m is None:
                storage.conn.execute(
                    """
                    INSERT INTO listing_search_state
                    (listing_id, search_id, first_seen, last_seen, is_active, removed_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        listing_id,
                        search_id,
                        first_seen,
                        last_seen,
                        is_active,
                        None if is_active else last_seen,
                    ),
                )
                memberships += 1
        else:
            memberships += 1

    if not dry_run:
        episodes_opened = storage.ensure_active_membership_episodes(now)
        storage.commit()
    storage.close()

    print(f"data_dir={data_dir} dry_run={dry_run}")
    print(f"listings scanned: {len(rows)}")
    print(f"prices newly parsed: {parsed_ok}")
    print(f"prices already filled (skipped): {parsed_skip}")
    print(f"prices unparseable: {len(unparseable)}")
    print(f"searches registered: {len(searches_registered)}")
    print(f"memberships inserted: {memberships}")
    print(f"active episodes opened: {episodes_opened}")
    if unparseable:
        print("unparseable samples (up to 20):")
        for listing_id, price in unparseable[:20]:
            print(f"  {listing_id}: {price!r}")
    return 0 if not unparseable else 0  # unparseable is informational, not a hard fail


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill parsed prices and search memberships.")
    parser.add_argument("--data-dir", default="./data", help="Directory containing listings.db")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report without writing changes.",
    )
    args = parser.parse_args()
    return _backfill(args.data_dir, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
