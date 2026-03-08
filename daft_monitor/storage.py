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
                first_seen TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

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
            )
            for l in listings
        ]
        if not rows:
            return 0
        before = self.conn.total_changes
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO listings
            (id, title, price, url, location, bedrooms, image_url, search_name, first_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.conn.commit()
        return self.conn.total_changes - before

