#!/usr/bin/env python3
"""Check observation-mode schema and invariants on a database copy.

Opens the DB through Storage so additive migrations run, then reports counts
and fails on integrity problems. Does not invent historical episodes.

Usage:
    python scripts/verify_observation_migration.py --data-dir ./data-migration-test
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daft_monitor.storage import Storage  # noqa: E402

REQUIRED_TABLES = (
    "listings",
    "listing_events",
    "searches",
    "listing_search_state",
    "search_runs",
    "search_run_listings",
    "listing_search_events",
    "listing_search_episodes",
    "app_meta",
)

REQUIRED_LISTING_COLUMNS = (
    "price_value",
    "price_period",
    "price_monthly_eq",
    "removed_at",
    "room_type",
    "facilities",
)


def _count(storage: Storage, sql: str, params: tuple[object, ...] = ()) -> int:
    row = storage.conn.execute(sql, params).fetchone()
    return int(row[0])


def _load_baseline(path: Path | None) -> dict[str, int] | None:
    if path is None:
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    counts = raw.get("counts") if isinstance(raw, dict) else None
    if not isinstance(counts, dict):
        raise ValueError("baseline json must contain object key 'counts'")
    return {str(k): int(v) for k, v in counts.items()}


def verify(data_dir: str, baseline_counts: dict[str, int] | None = None) -> list[str]:
    storage = Storage(data_dir)
    failures: list[str] = []
    try:
        integrity = storage.conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            failures.append(f"PRAGMA integrity_check: {integrity[0] if integrity else 'empty'}")
        fk_rows = storage.conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_rows:
            failures.append(f"PRAGMA foreign_key_check returned {len(fk_rows)} row(s)")

        tables = {row[0] for row in storage.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in REQUIRED_TABLES:
            if table not in tables:
                failures.append(f"missing table {table}")

        listing_columns = {row[1] for row in storage.conn.execute("PRAGMA table_info(listings)")}
        for column in REQUIRED_LISTING_COLUMNS:
            if column not in listing_columns:
                failures.append(f"listings missing column {column}")

        counts = {
            "listings": _count(storage, "SELECT COUNT(*) FROM listings"),
            "listing_events": _count(storage, "SELECT COUNT(*) FROM listing_events"),
            "searches": _count(storage, "SELECT COUNT(*) FROM searches"),
            "listing_search_state": _count(storage, "SELECT COUNT(*) FROM listing_search_state"),
            "listing_search_events": _count(storage, "SELECT COUNT(*) FROM listing_search_events"),
            "listing_search_episodes": _count(storage, "SELECT COUNT(*) FROM listing_search_episodes"),
            "search_runs": _count(storage, "SELECT COUNT(*) FROM search_runs"),
            "active_listings": _count(storage, "SELECT COUNT(*) FROM listings WHERE is_active = 1"),
        }
        print("Counts:")
        for key, value in counts.items():
            print(f"  {key}: {value}")
        if baseline_counts is not None:
            for key in ("listings", "listing_events"):
                baseline = baseline_counts.get(key)
                if baseline is None:
                    continue
                current = counts.get(key)
                if current is not None and current < baseline:
                    failures.append(
                        f"{key} decreased from baseline {baseline} to {current}"
                    )

        duplicate_active = _count(
            storage,
            """
            SELECT COUNT(*) FROM (
                SELECT listing_id, search_id
                FROM listing_search_episodes
                WHERE is_active = 1
                GROUP BY listing_id, search_id
                HAVING COUNT(*) > 1
            )
            """,
        )
        if duplicate_active:
            failures.append(f"{duplicate_active} memberships have more than one open episode")

        inactive_open_episodes = _count(
            storage,
            """
            SELECT COUNT(*)
            FROM listing_search_episodes e
            JOIN listing_search_state s
              ON s.listing_id = e.listing_id AND s.search_id = e.search_id
            WHERE e.is_active = 1 AND s.is_active = 0
            """,
        )
        if inactive_open_episodes:
            failures.append(
                f"{inactive_open_episodes} open episodes on inactive memberships"
            )

        active_missing_episodes = _count(
            storage,
            """
            SELECT COUNT(*)
            FROM listing_search_state s
            WHERE s.is_active = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM listing_search_episodes e
                  WHERE e.listing_id = s.listing_id
                    AND e.search_id = s.search_id
                    AND e.is_active = 1
              )
            """,
        )
        if active_missing_episodes:
            failures.append(
                f"{active_missing_episodes} active memberships lack an active episode"
            )

        lifecycle_epoch = storage.get_meta("lifecycle_v2_started_at")
        analytics_epoch = storage.get_meta("analytics_v2_started_at")
        print(f"  lifecycle_v2_started_at: {lifecycle_epoch}")
        print(f"  analytics_v2_started_at: {analytics_epoch}")
        if counts["listing_search_episodes"] > 0 and analytics_epoch is None:
            failures.append("episodes exist but analytics_v2_started_at is unset")

        print(f"  listings.db: {storage.db_path} ({storage.db_path.stat().st_size} bytes)")
    finally:
        storage.close()
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify observation-mode DB migration.")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument(
        "--baseline-json",
        type=Path,
        default=None,
        help="Optional JSON file with {'counts': {'listings': N, 'listing_events': M}}.",
    )
    args = parser.parse_args()
    baseline = _load_baseline(args.baseline_json)
    failures = verify(args.data_dir, baseline)
    if failures:
        print("FAILED:")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("OK: integrity and observation invariants passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
