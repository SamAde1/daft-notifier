from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from daft_monitor.models import Listing, ListingEvent


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
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS listing_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                metadata TEXT,
                FOREIGN KEY (listing_id) REFERENCES listings(id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_listing_events_listing_id_type
            ON listing_events(listing_id, event_type)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_listing_events_timestamp
            ON listing_events(timestamp)
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
        if "last_seen" not in columns:
            self.conn.execute("ALTER TABLE listings ADD COLUMN last_seen TEXT")
        if "is_active" not in columns:
            self.conn.execute("ALTER TABLE listings ADD COLUMN is_active INTEGER DEFAULT 1")
        if "last_price" not in columns:
            self.conn.execute("ALTER TABLE listings ADD COLUMN last_price TEXT")

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
                l.last_seen or l.first_seen,
                1 if l.is_active else 0,
                l.last_price or l.price,
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
             latitude, longitude, distance_to_location, last_seen, is_active, last_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def insert_event(self, event: ListingEvent) -> None:
        self.insert_events([event])

    def insert_events(self, events: Iterable[ListingEvent]) -> int:
        rows = [
            (
                event.listing_id,
                event.event_type,
                event.timestamp,
                event.old_value,
                event.new_value,
                event.metadata,
            )
            for event in events
        ]
        if not rows:
            return 0
        before = self.conn.total_changes
        self.conn.executemany(
            """
            INSERT INTO listing_events
            (listing_id, event_type, timestamp, old_value, new_value, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def get_active_listing_ids(self) -> set[str]:
        rows = self.conn.execute("SELECT id FROM listings WHERE is_active = 1").fetchall()
        return {str(row["id"]) for row in rows}

    def get_listings_by_ids(self, ids: set[str]) -> list[Listing]:
        if not ids:
            return []
        ordered_ids = sorted(ids)
        placeholders = ",".join("?" for _ in ordered_ids)
        rows = self.conn.execute(
            f"""
            SELECT id, title, price, url, location, bedrooms, image_url, search_name, first_seen,
                   latitude, longitude, distance_to_location, last_seen, is_active, last_price
            FROM listings
            WHERE id IN ({placeholders})
            """,
            ordered_ids,
        ).fetchall()
        return [self._row_to_listing(row) for row in rows]

    def mark_listings_removed(self, ids: set[str], timestamp: str) -> int:
        if not ids:
            return 0
        ordered_ids = sorted(ids)
        placeholders = ",".join("?" for _ in ordered_ids)
        before = self.conn.total_changes
        self.conn.execute(
            f"""
            UPDATE listings
            SET is_active = 0, last_seen = ?
            WHERE id IN ({placeholders}) AND is_active != 0
            """,
            [timestamp, *ordered_ids],
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def mark_listings_active(self, ids: set[str], timestamp: str) -> int:
        if not ids:
            return 0
        ordered_ids = sorted(ids)
        placeholders = ",".join("?" for _ in ordered_ids)
        before = self.conn.total_changes
        self.conn.execute(
            f"""
            UPDATE listings
            SET is_active = 1, last_seen = ?
            WHERE id IN ({placeholders})
            """,
            [timestamp, *ordered_ids],
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def update_listing_price(self, listing_id: str, new_price: str, timestamp: str) -> None:
        self.conn.execute(
            """
            UPDATE listings
            SET last_price = ?, last_seen = ?
            WHERE id = ?
            """,
            (new_price, timestamp, listing_id),
        )
        self.conn.commit()

    def update_last_seen(self, ids: set[str], timestamp: str) -> int:
        if not ids:
            return 0
        ordered_ids = sorted(ids)
        placeholders = ",".join("?" for _ in ordered_ids)
        before = self.conn.total_changes
        self.conn.execute(
            f"""
            UPDATE listings
            SET last_seen = ?
            WHERE id IN ({placeholders})
            """,
            [timestamp, *ordered_ids],
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

    @staticmethod
    def _row_to_listing(row: sqlite3.Row) -> Listing:
        return Listing(
            id=str(row["id"]),
            title=str(row["title"]),
            price=str(row["price"]),
            url=str(row["url"]),
            location=str(row["location"]),
            bedrooms=str(row["bedrooms"]) if row["bedrooms"] is not None else None,
            image_url=str(row["image_url"]) if row["image_url"] is not None else None,
            search_name=str(row["search_name"]),
            first_seen=str(row["first_seen"]),
            latitude=float(row["latitude"]) if row["latitude"] is not None else None,
            longitude=float(row["longitude"]) if row["longitude"] is not None else None,
            distance_to_location=(
                float(row["distance_to_location"]) if row["distance_to_location"] is not None else None
            ),
            last_seen=str(row["last_seen"]) if row["last_seen"] is not None else None,
            is_active=bool(row["is_active"]) if row["is_active"] is not None else True,
            last_price=str(row["last_price"]) if row["last_price"] is not None else None,
        )

