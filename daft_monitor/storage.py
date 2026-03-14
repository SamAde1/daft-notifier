from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from daft_monitor.models import Listing


class Storage:
    def __init__(self, data_dir: str):
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "listings.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS listings (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                price TEXT NOT NULL,
                url TEXT NOT NULL,
                location TEXT NOT NULL,
                bedrooms TEXT,
                image_url TEXT,
                search_name TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                latitude REAL,
                longitude REAL,
                distance_to_location REAL
            )
            """
        )
        self._migrate_schema()
        self.conn.commit()

    def _migrate_schema(self) -> None:
        """Add columns to existing databases when new fields are introduced."""
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(listings)").fetchall()}
        if "latitude" not in columns:
            self.conn.execute("ALTER TABLE listings ADD COLUMN latitude REAL")
        if "longitude" not in columns:
            self.conn.execute("ALTER TABLE listings ADD COLUMN longitude REAL")
        if "distance_to_location" not in columns:
            self.conn.execute("ALTER TABLE listings ADD COLUMN distance_to_location REAL")

    def close(self) -> None:
        self.conn.close()

    def is_first_run(self) -> bool:
        row = self.conn.execute("SELECT COUNT(1) AS count FROM listings").fetchone()
        return int(row["count"]) == 0

    def listing_exists(self, listing_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM listings WHERE id = ?", (listing_id,)).fetchone()
        return row is not None

    def filter_new_listings(self, listings: Iterable[Listing]) -> list[Listing]:
        listings_list = list(listings)
        if not listings_list:
            return []
        ids = [l.id for l in listings_list]
        placeholders = ",".join("?" for _ in ids)
        query = f"SELECT id FROM listings WHERE id IN ({placeholders})"
        existing_rows = self.conn.execute(query, ids).fetchall()
        existing_ids = {str(row["id"]) for row in existing_rows}
        return [l for l in listings_list if l.id not in existing_ids]

    def insert_listings(self, listings: Iterable[Listing]) -> int:
        rows = [
            (
                l.id,
                l.title,
                l.price,
                l.url,
                l.location,
                l.bedrooms,
                l.image_url,
                l.search_name,
                l.first_seen,
                l.latitude,
                l.longitude,
                l.distance_to_location,
            )
            for l in listings
        ]
        if not rows:
            return 0
        before = self.conn.total_changes
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO listings
            (id, title, price, url, location, bedrooms, image_url, search_name, first_seen,
             latitude, longitude, distance_to_location)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def update_coordinates(self, listing_id: str, latitude: float, longitude: float) -> None:
        """Backfill lat/lng for an existing listing."""
        self.conn.execute(
            "UPDATE listings SET latitude = ?, longitude = ? WHERE id = ?",
            (latitude, longitude, listing_id),
        )
        self.conn.commit()

    def update_distances(self, distances: dict[str, float]) -> int:
        """Update distance_to_location for many listing ids in one commit."""
        if not distances:
            return 0
        before = self.conn.total_changes
        self.conn.executemany(
            "UPDATE listings SET distance_to_location = ? WHERE id = ?",
            [(distance, listing_id) for listing_id, distance in distances.items()],
        )
        self.conn.commit()
        return self.conn.total_changes - before

