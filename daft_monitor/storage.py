from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from daft_monitor.models import Listing, ListingEvent, MembershipTransition
from daft_monitor.price_parser import parse_price_fields
from daft_monitor.search_identity import criteria_fingerprint, resolved_search_id
from daft_monitor.config import SearchConfig
from daft_monitor.constants import (
    EVENT_CONFIG_RETIRED,
    EVENT_NEW,
    EVENT_PRICE_CHANGE,
    EVENT_RELISTED,
    EVENT_REMOVED,
    EVENT_SEED,
)


class Storage:
    def __init__(self, data_dir: str):
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "listings.db"
        # timeout: wait up to 30s if another connection holds a lock (e.g. backup).
        self.conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 30000")
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
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS searches (
                search_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                criteria_fingerprint TEXT NOT NULL,
                first_registered TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS listing_search_state (
                listing_id TEXT NOT NULL,
                search_id TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                removed_at TEXT,
                missing_since TEXT,
                last_authoritative_run_id INTEGER,
                PRIMARY KEY (listing_id, search_id),
                FOREIGN KEY (listing_id) REFERENCES listings(id),
                FOREIGN KEY (search_id) REFERENCES searches(search_id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_listing_search_state_search
            ON listing_search_state(search_id, is_active)
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS search_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                pages_fetched INTEGER,
                results_count INTEGER,
                complete INTEGER NOT NULL DEFAULT 0,
                is_deep INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                run_kind TEXT,
                FOREIGN KEY (search_id) REFERENCES searches(search_id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS search_run_listings (
                search_run_id INTEGER NOT NULL,
                listing_id TEXT NOT NULL,
                PRIMARY KEY (search_run_id, listing_id),
                FOREIGN KEY (search_run_id) REFERENCES search_runs(id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_search_runs_search_finished
            ON search_runs(search_id, finished_at)
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS listing_search_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id TEXT NOT NULL,
                search_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                title TEXT,
                location TEXT,
                bedrooms TEXT,
                price_raw TEXT,
                price_value REAL,
                price_period TEXT,
                price_monthly_eq REAL,
                room_type TEXT,
                facilities TEXT,
                old_value TEXT,
                new_value TEXT,
                FOREIGN KEY (listing_id) REFERENCES listings(id),
                FOREIGN KEY (search_id) REFERENCES searches(search_id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_listing_search_events_search_time
            ON listing_search_events(search_id, timestamp)
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS listing_search_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id TEXT NOT NULL,
                search_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                title TEXT,
                location TEXT,
                bedrooms TEXT,
                room_type TEXT,
                facilities TEXT,
                price_value REAL,
                price_period TEXT,
                price_monthly_eq REAL,
                distance_to_location REAL,
                FOREIGN KEY (listing_id) REFERENCES listings(id),
                FOREIGN KEY (search_id) REFERENCES searches(search_id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_listing_search_episodes_search_active
            ON listing_search_episodes(search_id, is_active)
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
        if "price_value" not in columns:
            self.conn.execute("ALTER TABLE listings ADD COLUMN price_value REAL")
        if "price_period" not in columns:
            self.conn.execute("ALTER TABLE listings ADD COLUMN price_period TEXT")
        if "price_monthly_eq" not in columns:
            self.conn.execute("ALTER TABLE listings ADD COLUMN price_monthly_eq REAL")
        if "removed_at" not in columns:
            self.conn.execute("ALTER TABLE listings ADD COLUMN removed_at TEXT")
        if "room_type" not in columns:
            self.conn.execute("ALTER TABLE listings ADD COLUMN room_type TEXT")
        if "facilities" not in columns:
            self.conn.execute("ALTER TABLE listings ADD COLUMN facilities TEXT")

        search_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(searches)").fetchall()}
        if "pending_seed_fingerprint" not in search_columns:
            self.conn.execute("ALTER TABLE searches ADD COLUMN pending_seed_fingerprint TEXT")
        if "seed_completed_fingerprint" not in search_columns:
            self.conn.execute("ALTER TABLE searches ADD COLUMN seed_completed_fingerprint TEXT")

        run_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(search_runs)").fetchall()}
        if "criteria_fingerprint" not in run_columns:
            self.conn.execute("ALTER TABLE search_runs ADD COLUMN criteria_fingerprint TEXT")
        if "run_kind" not in run_columns:
            self.conn.execute("ALTER TABLE search_runs ADD COLUMN run_kind TEXT")

        state_columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(listing_search_state)").fetchall()
        }
        if "missing_since" not in state_columns:
            self.conn.execute("ALTER TABLE listing_search_state ADD COLUMN missing_since TEXT")
        if "last_authoritative_run_id" not in state_columns:
            self.conn.execute(
                "ALTER TABLE listing_search_state ADD COLUMN last_authoritative_run_id INTEGER"
            )

        event_columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(listing_search_events)").fetchall()
        }
        if "old_value" not in event_columns:
            self.conn.execute("ALTER TABLE listing_search_events ADD COLUMN old_value TEXT")
        if "new_value" not in event_columns:
            self.conn.execute("ALTER TABLE listing_search_events ADD COLUMN new_value TEXT")

        episode_columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(listing_search_episodes)").fetchall()
        }
        for column, ddl in (
            ("title", "TEXT"),
            ("location", "TEXT"),
            ("bedrooms", "TEXT"),
            ("room_type", "TEXT"),
            ("facilities", "TEXT"),
            ("price_value", "REAL"),
            ("price_period", "TEXT"),
            ("price_monthly_eq", "REAL"),
            ("distance_to_location", "REAL"),
        ):
            if column not in episode_columns:
                self.conn.execute(
                    f"ALTER TABLE listing_search_episodes ADD COLUMN {column} {ddl}"
                )

        self._dedupe_active_episodes()
        self.conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_episode_one_active_per_membership
            ON listing_search_episodes(listing_id, search_id)
            WHERE is_active = 1
            """
        )

        self._bootstrap_analytics_v2_epoch()

    def _dedupe_active_episodes(self) -> None:
        """Close all but the latest active episode per membership.

        Pre-hardening databases may carry duplicate open episodes from the
        non-atomic record path; the unique index cannot be created over them.
        """
        rows = self.conn.execute(
            """
            SELECT id, listing_id, search_id, started_at
            FROM listing_search_episodes
            WHERE is_active = 1
            ORDER BY listing_id, search_id, started_at DESC, id DESC
            """
        ).fetchall()
        seen: set[tuple[str, str]] = set()
        duplicate_ids: list[int] = []
        for row in rows:
            key = (str(row["listing_id"]), str(row["search_id"]))
            if key in seen:
                duplicate_ids.append(int(row["id"]))
            else:
                seen.add(key)
        for episode_id in duplicate_ids:
            self.conn.execute(
                """
                UPDATE listing_search_episodes
                SET is_active = 0, ended_at = started_at
                WHERE id = ?
                """,
                (episode_id,),
            )

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # app_meta helpers
    # ------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row is not None else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO app_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Search registry
    # ------------------------------------------------------------------

    def register_search(self, search: SearchConfig, timestamp: str) -> tuple[str, bool]:
        """Upsert a search into the registry.

        Returns (search_id, fingerprint_changed).
        fingerprint_changed is True when this is a brand-new search OR the
        criteria hash differs from what was stored previously.
        Pending seeding is persisted until a complete deep scan for the new
        fingerprint clears it.
        """
        search_id = resolved_search_id(search)
        fingerprint = criteria_fingerprint(search)
        existing = self.conn.execute(
            "SELECT criteria_fingerprint, pending_seed_fingerprint FROM searches WHERE search_id = ?",
            (search_id,),
        ).fetchone()

        if existing is None:
            self.conn.execute(
                """
                INSERT INTO searches (
                    search_id, name, criteria_fingerprint, first_registered,
                    pending_seed_fingerprint, seed_completed_fingerprint
                )
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (search_id, search.name, fingerprint, timestamp, fingerprint),
            )
            self.conn.commit()
            return search_id, True

        changed = str(existing["criteria_fingerprint"]) != fingerprint
        if changed:
            self.conn.execute(
                """
                UPDATE searches
                SET name = ?, criteria_fingerprint = ?, pending_seed_fingerprint = ?
                WHERE search_id = ?
                """,
                (search.name, fingerprint, fingerprint, search_id),
            )
            self.conn.commit()
        else:
            self.conn.execute(
                "UPDATE searches SET name = ? WHERE search_id = ?",
                (search.name, search_id),
            )
            self.conn.commit()
        return search_id, changed

    def get_search_fingerprint(self, search_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT criteria_fingerprint FROM searches WHERE search_id = ?",
            (search_id,),
        ).fetchone()
        return str(row["criteria_fingerprint"]) if row is not None else None

    def get_registered_search_ids(self) -> set[str]:
        rows = self.conn.execute("SELECT search_id FROM searches").fetchall()
        return {str(row["search_id"]) for row in rows}

    def retire_search_memberships(self, search_ids: set[str], timestamp: str) -> int:
        """Retire memberships for searches removed from config.

        This is a configuration event, not a market removal: we close
        memberships/episodes and record membership-level `config_retired`
        events, but do not emit global listing `removed` events.
        """
        if not search_ids:
            return 0
        search_id_list = sorted(search_ids)
        search_ph = ",".join("?" for _ in search_id_list)
        rows = self.conn.execute(
            f"""
            SELECT listing_id, search_id
            FROM listing_search_state
            WHERE search_id IN ({search_ph}) AND is_active = 1
            """,
            search_id_list,
        ).fetchall()
        if not rows:
            return 0
        before = self.conn.total_changes
        listing_ids = {str(row["listing_id"]) for row in rows}
        listings_by_id = {l.id: l for l in self.get_listings_by_ids(listing_ids)}
        for row in rows:
            listing_id = str(row["listing_id"])
            search_id = str(row["search_id"])
            self.conn.execute(
                """
                UPDATE listing_search_state
                SET is_active = 0, removed_at = ?, missing_since = NULL
                WHERE listing_id = ? AND search_id = ? AND is_active = 1
                """,
                (timestamp, listing_id, search_id),
            )
            listing = listings_by_id.get(listing_id)
            if listing is not None:
                self._insert_membership_event(
                    listing_id=listing_id,
                    search_id=search_id,
                    event_type=EVENT_CONFIG_RETIRED,
                    timestamp=timestamp,
                    listing=listing,
                )
            self._close_episode(listing_id, search_id, timestamp)
        self.conn.commit()
        return self.conn.total_changes - before

    def search_is_seeding(self, search_id: str) -> bool:
        """True while pending baseline seeding has not completed for current criteria."""
        row = self.conn.execute(
            """
            SELECT pending_seed_fingerprint, criteria_fingerprint
            FROM searches WHERE search_id = ?
            """,
            (search_id,),
        ).fetchone()
        if row is None:
            return False
        pending = row["pending_seed_fingerprint"]
        if pending is None:
            return False
        return str(pending) == str(row["criteria_fingerprint"])

    def clear_pending_seed(self, search_id: str, fingerprint: str) -> None:
        self.conn.execute(
            """
            UPDATE searches
            SET pending_seed_fingerprint = NULL, seed_completed_fingerprint = ?
            WHERE search_id = ? AND criteria_fingerprint = ?
            """,
            (fingerprint, search_id, fingerprint),
        )
        self.conn.commit()

    def search_needs_seed(self, search_id: str) -> bool:
        """True when seeding is pending or the search has no memberships yet."""
        if self.search_is_seeding(search_id):
            return True
        row = self.conn.execute(
            "SELECT COUNT(1) AS count FROM listing_search_state WHERE search_id = ?",
            (search_id,),
        ).fetchone()
        return int(row["count"]) == 0

    def is_first_run(self) -> bool:
        """Legacy helper: True when the listings table is completely empty."""
        row = self.conn.execute("SELECT COUNT(1) AS count FROM listings").fetchone()
        return int(row["count"]) == 0

    # ------------------------------------------------------------------
    # Listing CRUD
    # ------------------------------------------------------------------

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

    @staticmethod
    def _enrich_parsed_price(listing: Listing) -> Listing:
        """Fill price_value / price_period / price_monthly_eq on a listing in place."""
        if listing.price_value is None and listing.price_period is None:
            value, period, monthly = parse_price_fields(listing.price, listing.bedrooms)
            listing.price_value = value
            listing.price_period = period
            listing.price_monthly_eq = monthly
        return listing

    def insert_listings(self, listings: Iterable[Listing]) -> int:
        rows = []
        for listing in listings:
            self._enrich_parsed_price(listing)
            rows.append(
                (
                    listing.id,
                    listing.title,
                    listing.price,
                    listing.url,
                    listing.location,
                    listing.bedrooms,
                    listing.image_url,
                    listing.search_name,
                    listing.first_seen,
                    listing.latitude,
                    listing.longitude,
                    listing.distance_to_location,
                    listing.last_seen or listing.first_seen,
                    1 if listing.is_active else 0,
                    listing.last_price or listing.price,
                    listing.price_value,
                    listing.price_period,
                    listing.price_monthly_eq,
                    listing.removed_at,
                    listing.room_type,
                    self._facilities_to_db(listing.facilities),
                )
            )
        if not rows:
            return 0
        before = self.conn.total_changes
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO listings
            (id, title, price, url, location, bedrooms, image_url, search_name, first_seen,
             latitude, longitude, distance_to_location, last_seen, is_active, last_price,
             price_value, price_period, price_monthly_eq, removed_at, room_type, facilities)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    # ------------------------------------------------------------------
    # listing_search_state (per-search membership)
    # ------------------------------------------------------------------

    def upsert_listing_search_state(
        self,
        memberships: Iterable[tuple[str, str, str]],
        *,
        activate: bool = True,
    ) -> int:
        """Legacy row upsert. Prefer apply_memberships_seen for transitions."""
        rows = list(memberships)
        if not rows:
            return 0
        before = self.conn.total_changes
        for listing_id, search_id, timestamp in rows:
            existing = self.conn.execute(
                """
                SELECT 1 FROM listing_search_state
                WHERE listing_id = ? AND search_id = ?
                """,
                (listing_id, search_id),
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    """
                    INSERT INTO listing_search_state
                    (listing_id, search_id, first_seen, last_seen, is_active, removed_at)
                    VALUES (?, ?, ?, ?, 1, NULL)
                    """,
                    (listing_id, search_id, timestamp, timestamp),
                )
            elif activate:
                self.conn.execute(
                    """
                    UPDATE listing_search_state
                    SET last_seen = ?, is_active = 1, removed_at = NULL
                    WHERE listing_id = ? AND search_id = ?
                    """,
                    (timestamp, listing_id, search_id),
                )
            else:
                self.conn.execute(
                    """
                    UPDATE listing_search_state
                    SET last_seen = ?
                    WHERE listing_id = ? AND search_id = ?
                    """,
                    (timestamp, listing_id, search_id),
                )
        self.conn.commit()
        return self.conn.total_changes - before

    def apply_search_observations(
        self,
        memberships: Iterable[tuple[str, str, str, Listing]],
        seed_flags: dict[str, bool],
    ) -> list[MembershipTransition]:
        """Atomically apply observed memberships: state upsert, episode open,
        and new/seed/relisted event recording in a single transaction.

        `seed_flags` is the per-search seeding state captured before the
        search runs; events are classified from it so that clearing pending
        state afterwards never mislabels the same cycle's events.
        """
        transitions: list[MembershipTransition] = []
        for listing_id, search_id, timestamp, listing in memberships:
            existing = self.conn.execute(
                """
                SELECT is_active FROM listing_search_state
                WHERE listing_id = ? AND search_id = ?
                """,
                (listing_id, search_id),
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    """
                    INSERT INTO listing_search_state
                    (listing_id, search_id, first_seen, last_seen, is_active,
                     removed_at, missing_since)
                    VALUES (?, ?, ?, ?, 1, NULL, NULL)
                    """,
                    (listing_id, search_id, timestamp, timestamp),
                )
                transition = MembershipTransition(
                    listing_id=listing_id,
                    search_id=search_id,
                    transition="created",
                    listing=listing,
                    timestamp=timestamp,
                )
                self._open_episode(listing_id, search_id, timestamp, listing)
                event_type = EVENT_SEED if seed_flags.get(search_id, False) else EVENT_NEW
            elif not bool(existing["is_active"]):
                self.conn.execute(
                    """
                    UPDATE listing_search_state
                    SET last_seen = ?, is_active = 1, removed_at = NULL, missing_since = NULL
                    WHERE listing_id = ? AND search_id = ?
                    """,
                    (timestamp, listing_id, search_id),
                )
                transition = MembershipTransition(
                    listing_id=listing_id,
                    search_id=search_id,
                    transition="reactivated",
                    listing=listing,
                    timestamp=timestamp,
                )
                self._open_episode(listing_id, search_id, timestamp, listing)
                event_type = EVENT_SEED if seed_flags.get(search_id, False) else EVENT_RELISTED
            else:
                self.conn.execute(
                    """
                    UPDATE listing_search_state
                    SET last_seen = ?, missing_since = NULL
                    WHERE listing_id = ? AND search_id = ?
                    """,
                    (timestamp, listing_id, search_id),
                )
                transition = MembershipTransition(
                    listing_id=listing_id,
                    search_id=search_id,
                    transition="unchanged",
                    listing=listing,
                    timestamp=timestamp,
                )
                event_type = None
            if event_type is not None:
                self._insert_membership_event(
                    listing_id=listing_id,
                    search_id=search_id,
                    event_type=event_type,
                    timestamp=timestamp,
                    listing=listing,
                )
            transitions.append(transition)
        self.conn.commit()
        return transitions

    def record_membership_price_changes(
        self,
        listing: Listing,
        *,
        old_price_raw: str,
        new_price_raw: str,
        timestamp: str,
    ) -> int:
        """Record a price_change membership event for every active membership.

        Only complete deep/baseline-scoped analyses can tell which searches a
        listing truly belongs to; active memberships are the best in-DB truth.
        """
        rows = self.conn.execute(
            """
            SELECT search_id FROM listing_search_state
            WHERE listing_id = ? AND is_active = 1
            """,
            (listing.id,),
        ).fetchall()
        count = 0
        for row in rows:
            self._insert_membership_event(
                listing_id=listing.id,
                search_id=str(row["search_id"]),
                event_type=EVENT_PRICE_CHANGE,
                timestamp=timestamp,
                listing=listing,
                old_value=old_price_raw,
                new_value=new_price_raw,
            )
            count += 1
        if count:
            self.conn.commit()
        return count

    def get_membership_listing_ids(self, search_id: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT listing_id FROM listing_search_state WHERE search_id = ?",
            (search_id,),
        ).fetchall()
        return {str(row["listing_id"]) for row in rows}

    def get_active_memberships(
        self, search_id: str
    ) -> list[tuple[str, str]]:
        """Return (listing_id, last_seen) for active memberships of a search."""
        rows = self.conn.execute(
            """
            SELECT listing_id, last_seen
            FROM listing_search_state
            WHERE search_id = ? AND is_active = 1
            """,
            (search_id,),
        ).fetchall()
        return [(str(row["listing_id"]), str(row["last_seen"])) for row in rows]

    def get_active_membership_rows(
        self, search_id: str
    ) -> list[tuple[str, str, str | None, int | None]]:
        """Return (listing_id, last_seen, missing_since, first_missing_run_id)."""
        rows = self.conn.execute(
            """
            SELECT listing_id, last_seen, missing_since, last_authoritative_run_id
            FROM listing_search_state
            WHERE search_id = ? AND is_active = 1
            """,
            (search_id,),
        ).fetchall()
        return [
            (
                str(row["listing_id"]),
                str(row["last_seen"]),
                str(row["missing_since"]) if row["missing_since"] is not None else None,
                int(row["last_authoritative_run_id"])
                if row["last_authoritative_run_id"] is not None
                else None,
            )
            for row in rows
        ]

    def mark_memberships_missing(
        self,
        search_id: str,
        listing_ids: Iterable[str],
        missing_since: str,
        authoritative_run_id: int,
    ) -> int:
        """Record first-authoritative-absence for the grace timer.

        Called when a complete deep scan (the authoritative run) did not
        return these listings. Removals are only authorized once
        `missing_since` ages past `removal_grace_hours`.
        """
        ids = sorted(set(listing_ids))
        if not ids:
            return 0
        before = self.conn.total_changes
        for listing_id in ids:
            self.conn.execute(
                """
                UPDATE listing_search_state
                SET missing_since = COALESCE(missing_since, ?),
                    last_authoritative_run_id = COALESCE(last_authoritative_run_id, ?)
                WHERE search_id = ? AND listing_id = ? AND is_active = 1
                """,
                (missing_since, authoritative_run_id, search_id, listing_id),
            )
        self.conn.commit()
        return self.conn.total_changes - before

    def mark_memberships_inactive(
        self,
        search_id: str,
        listing_ids: set[str],
        timestamp: str,
    ) -> int:
        """Deactivate memberships and atomically record removal truth.

        Listing snapshots are always loaded from the DB so every removal —
        including removals of listings absent from the current cycle's
        shallow results — gets a listing_search_events row and a closed
        episode in the same transaction.
        """
        if not listing_ids:
            return 0
        ordered = sorted(listing_ids)
        placeholders = ",".join("?" for _ in ordered)
        before = self.conn.total_changes
        self.conn.execute(
            f"""
            UPDATE listing_search_state
            SET is_active = 0, removed_at = ?, missing_since = NULL
            WHERE search_id = ?
              AND listing_id IN ({placeholders})
              AND is_active != 0
            """,
            [timestamp, search_id, *ordered],
        )
        listings_by_id = {l.id: l for l in self.get_listings_by_ids(set(ordered))}
        for listing_id in ordered:
            listing = listings_by_id.get(listing_id)
            if listing is not None:
                self._insert_membership_event(
                    listing_id=listing_id,
                    search_id=search_id,
                    event_type=EVENT_REMOVED,
                    timestamp=timestamp,
                    listing=listing,
                )
            self._close_episode(listing_id, search_id, timestamp)
        self.conn.commit()
        return self.conn.total_changes - before

    def listing_ids_inactive_in_all_configured_searches(
        self,
        listing_ids: set[str],
        configured_search_ids: set[str],
    ) -> set[str]:
        """Listings whose every membership in *configured* searches is inactive.

        Memberships for searches no longer in config are ignored (orphan fix).
        Listings with *no* configured membership rows are left alone (unmigrated
        or never attributed). Listings whose only remaining memberships belong to
        deleted searches are also left alone here — callers that want orphan
        cleanup should pass those listing ids separately after detecting
        deleted-search-only rows.
        """
        if not listing_ids or not configured_search_ids:
            return set()

        removable: set[str] = set()
        ordered = sorted(listing_ids)
        cfg = sorted(configured_search_ids)
        listing_ph = ",".join("?" for _ in ordered)
        search_ph = ",".join("?" for _ in cfg)
        rows = self.conn.execute(
            f"""
            SELECT listing_id, search_id, is_active
            FROM listing_search_state
            WHERE listing_id IN ({listing_ph})
              AND search_id IN ({search_ph})
            """,
            [*ordered, *cfg],
        ).fetchall()

        by_listing: dict[str, list[bool]] = {lid: [] for lid in listing_ids}
        for row in rows:
            by_listing[str(row["listing_id"])].append(bool(row["is_active"]))

        for listing_id, flags in by_listing.items():
            # At least one configured membership, and all of them inactive.
            if flags and not any(flags):
                removable.add(listing_id)
        return removable

    def listing_ids_orphaned_from_configured_searches(
        self,
        listing_ids: set[str],
        configured_search_ids: set[str],
    ) -> set[str]:
        """Listings that have memberships, but none in currently configured searches.

        Deleted-search-only rows: ignore those memberships for lifecycle truth and
        treat the listing as orphaned from the live config (eligible for removal).
        Listings with zero membership rows anywhere are not returned.
        """
        if not listing_ids:
            return set()

        ordered = sorted(listing_ids)
        listing_ph = ",".join("?" for _ in ordered)
        rows = self.conn.execute(
            f"""
            SELECT listing_id, search_id
            FROM listing_search_state
            WHERE listing_id IN ({listing_ph})
            """,
            ordered,
        ).fetchall()

        by_listing: dict[str, set[str]] = {}
        for row in rows:
            by_listing.setdefault(str(row["listing_id"]), set()).add(str(row["search_id"]))

        orphaned: set[str] = set()
        for listing_id, search_ids in by_listing.items():
            if not search_ids:
                continue
            if not (search_ids & configured_search_ids):
                orphaned.add(listing_id)
        return orphaned

    # ------------------------------------------------------------------
    # search_runs
    # ------------------------------------------------------------------

    def insert_search_run(
        self,
        *,
        search_id: str,
        started_at: str,
        finished_at: str,
        pages_fetched: int,
        results_count: int,
        complete: bool,
        is_deep: bool,
        error: str | None = None,
        criteria_fingerprint: str | None = None,
        run_kind: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO search_runs
            (search_id, started_at, finished_at, pages_fetched, results_count,
             complete, is_deep, error, criteria_fingerprint, run_kind)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                search_id,
                started_at,
                finished_at,
                pages_fetched,
                results_count,
                1 if complete else 0,
                1 if is_deep else 0,
                error,
                criteria_fingerprint,
                run_kind or ("deep" if is_deep else "shallow"),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def insert_search_run_listings(self, search_run_id: int, listing_ids: Iterable[str]) -> int:
        """Persist exactly which listings a search run returned."""
        rows = [(search_run_id, lid) for lid in sorted(set(listing_ids))]
        if not rows:
            return 0
        before = self.conn.total_changes
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO search_run_listings (search_run_id, listing_id)
            VALUES (?, ?)
            """,
            rows,
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def get_run_listing_ids(self, search_run_id: int) -> set[str]:
        rows = self.conn.execute(
            "SELECT listing_id FROM search_run_listings WHERE search_run_id = ?",
            (search_run_id,),
        ).fetchall()
        return {str(row["listing_id"]) for row in rows}

    def get_previous_complete_deep_results_count(
        self, search_id: str, criteria_fingerprint: str | None = None
    ) -> int | None:
        fingerprint = criteria_fingerprint or self.get_search_fingerprint(search_id)
        if fingerprint is None:
            return None
        row = self.conn.execute(
            """
            SELECT results_count
            FROM search_runs
            WHERE search_id = ? AND is_deep = 1 AND complete = 1
              AND criteria_fingerprint = ?
            ORDER BY finished_at DESC, id DESC
            LIMIT 1
            """,
            (search_id, fingerprint),
        ).fetchone()
        if row is None:
            return None
        return int(row["results_count"])

    def get_latest_complete_deep_scan(
        self, search_id: str, criteria_fingerprint: str | None = None
    ) -> tuple[str, int] | None:
        """Return (finished_at, results_count) for the latest complete deep scan."""
        run = self.get_latest_complete_deep_scan_run(search_id, criteria_fingerprint)
        if run is None:
            return None
        return run[1], run[2]

    def get_latest_complete_deep_scan_run(
        self, search_id: str, criteria_fingerprint: str | None = None
    ) -> tuple[int, str, int] | None:
        """Return (run_id, finished_at, results_count) for the latest complete
        deep scan of the current criteria fingerprint."""
        fingerprint = criteria_fingerprint or self.get_search_fingerprint(search_id)
        if fingerprint is None:
            return None
        row = self.conn.execute(
            """
            SELECT id, finished_at, results_count
            FROM search_runs
            WHERE search_id = ? AND is_deep = 1 AND complete = 1
              AND criteria_fingerprint = ?
              AND (run_kind IS NULL OR run_kind = 'deep')
            ORDER BY finished_at DESC, id DESC
            LIMIT 1
            """,
            (search_id, fingerprint),
        ).fetchone()
        if row is None or row["finished_at"] is None:
            return None
        return int(row["id"]), str(row["finished_at"]), int(row["results_count"])

    def ensure_analytics_v2_started(self, timestamp: str) -> bool:
        """Stamp analytics_v2_started_at once. Returns True if this call wrote it."""
        existing = self.get_meta("analytics_v2_started_at")
        if existing is not None:
            return False
        self.set_meta("analytics_v2_started_at", timestamp)
        return True

    def get_analytics_v2_started_at(self) -> str | None:
        return self.get_meta("analytics_v2_started_at")

    def ensure_lifecycle_v2_started(self, timestamp: str) -> bool:
        """Stamp lifecycle_v2_started_at once. Returns True if this call wrote it."""
        existing = self.get_meta("lifecycle_v2_started_at")
        if existing is not None:
            return False
        self.set_meta("lifecycle_v2_started_at", timestamp)
        return True

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
                   latitude, longitude, distance_to_location, last_seen, is_active, last_price,
                   price_value, price_period, price_monthly_eq, removed_at, room_type, facilities
            FROM listings
            WHERE id IN ({placeholders})
            """,
            ordered_ids,
        ).fetchall()
        return [self._row_to_listing(row) for row in rows]

    def mark_listings_removed(self, ids: set[str], timestamp: str) -> int:
        """Mark listings inactive. Preserves last_seen; sets removed_at."""
        if not ids:
            return 0
        ordered_ids = sorted(ids)
        placeholders = ",".join("?" for _ in ordered_ids)
        before = self.conn.total_changes
        self.conn.execute(
            f"""
            UPDATE listings
            SET is_active = 0, removed_at = ?
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
            SET is_active = 1, last_seen = ?, removed_at = NULL
            WHERE id IN ({placeholders})
            """,
            [timestamp, *ordered_ids],
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def update_listing_price(self, listing_id: str, new_price: str, timestamp: str, bedrooms: str | None = None) -> None:
        """Update raw price, last_price, parsed columns, and last_seen."""
        value, period, monthly = parse_price_fields(new_price, bedrooms)
        self.conn.execute(
            """
            UPDATE listings
            SET price = ?,
                last_price = ?,
                price_value = ?,
                price_period = ?,
                price_monthly_eq = ?,
                last_seen = ?
            WHERE id = ?
            """,
            (new_price, new_price, value, period, monthly, timestamp, listing_id),
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

    def update_parsed_price(
        self,
        listing_id: str,
        price_value: float | None,
        price_period: str | None,
        price_monthly_eq: float | None,
    ) -> None:
        """Set parsed price columns (used by the offline backfill script)."""
        self.conn.execute(
            """
            UPDATE listings
            SET price_value = ?, price_period = ?, price_monthly_eq = ?
            WHERE id = ?
            """,
            (price_value, price_period, price_monthly_eq, listing_id),
        )

    def commit(self) -> None:
        self.conn.commit()

    def _bootstrap_analytics_v2_epoch(self) -> None:
        """Ensure analytics epoch exists and active memberships have episodes."""
        existing_epoch = self.get_meta("analytics_v2_started_at")
        timestamp = existing_epoch or Listing.now_iso()
        self._ensure_active_membership_episodes(timestamp)
        if existing_epoch is None:
            self.conn.execute(
                """
                INSERT INTO app_meta (key, value) VALUES ('analytics_v2_started_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (timestamp,),
            )
        self.conn.commit()

    def _ensure_active_membership_episodes(self, timestamp: str) -> int:
        """Open missing active episodes for active memberships.

        Idempotent: does nothing for memberships that already have an open
        episode. Does not commit; callers control transaction boundaries.
        """
        rows = self.conn.execute(
            """
            SELECT s.listing_id, s.search_id
            FROM listing_search_state s
            WHERE s.is_active = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM listing_search_episodes e
                  WHERE e.listing_id = s.listing_id
                    AND e.search_id = s.search_id
                    AND e.is_active = 1
              )
            """
        ).fetchall()
        if not rows:
            return 0
        listing_ids = {str(row["listing_id"]) for row in rows}
        listings_by_id = {l.id: l for l in self.get_listings_by_ids(listing_ids)}
        opened = 0
        for row in rows:
            listing_id = str(row["listing_id"])
            self._open_episode(
                listing_id,
                str(row["search_id"]),
                timestamp,
                listings_by_id.get(listing_id),
            )
            opened += 1
        return opened

    def ensure_active_membership_episodes(self, timestamp: str) -> int:
        """Public wrapper that opens missing active episodes and commits."""
        opened = self._ensure_active_membership_episodes(timestamp)
        if opened:
            self.conn.commit()
        return opened

    @staticmethod
    def _facilities_to_db(facilities: list[str] | None) -> str | None:
        if not facilities:
            return None
        return json.dumps(sorted({f.lower() for f in facilities}))

    @staticmethod
    def _facilities_from_db(raw: str | None) -> list[str] | None:
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return None

    def _insert_membership_event(
        self,
        *,
        listing_id: str,
        search_id: str,
        event_type: str,
        timestamp: str,
        listing: Listing,
        old_value: str | None = None,
        new_value: str | None = None,
    ) -> None:
        self._enrich_parsed_price(listing)
        self.conn.execute(
            """
            INSERT INTO listing_search_events (
                listing_id, search_id, event_type, timestamp,
                title, location, bedrooms, price_raw, price_value, price_period,
                price_monthly_eq, room_type, facilities, old_value, new_value
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id,
                search_id,
                event_type,
                timestamp,
                listing.title,
                listing.location,
                listing.bedrooms,
                listing.price,
                listing.price_value,
                listing.price_period,
                listing.price_monthly_eq,
                listing.room_type,
                self._facilities_to_db(listing.facilities),
                old_value,
                new_value,
            ),
        )

    def _open_episode(
        self,
        listing_id: str,
        search_id: str,
        timestamp: str,
        listing: Listing | None = None,
    ) -> None:
        """Open an episode, defensively skipping if one is already active."""
        existing = self.conn.execute(
            """
            SELECT id FROM listing_search_episodes
            WHERE listing_id = ? AND search_id = ? AND is_active = 1
            """,
            (listing_id, search_id),
        ).fetchone()
        if existing is not None:
            return
        if listing is not None:
            self._enrich_parsed_price(listing)
        self.conn.execute(
            """
            INSERT INTO listing_search_episodes
            (listing_id, search_id, started_at, ended_at, is_active,
             title, location, bedrooms, room_type, facilities,
             price_value, price_period, price_monthly_eq, distance_to_location)
            VALUES (?, ?, ?, NULL, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id,
                search_id,
                timestamp,
                listing.title if listing is not None else None,
                listing.location if listing is not None else None,
                listing.bedrooms if listing is not None else None,
                listing.room_type if listing is not None else None,
                self._facilities_to_db(listing.facilities) if listing is not None else None,
                listing.price_value if listing is not None else None,
                listing.price_period if listing is not None else None,
                listing.price_monthly_eq if listing is not None else None,
                listing.distance_to_location if listing is not None else None,
            ),
        )

    def _close_episode(self, listing_id: str, search_id: str, timestamp: str) -> None:
        self.conn.execute(
            """
            UPDATE listing_search_episodes
            SET ended_at = ?, is_active = 0
            WHERE listing_id = ? AND search_id = ? AND is_active = 1
            """,
            (timestamp, listing_id, search_id),
        )

    @staticmethod
    def _row_to_listing(row: sqlite3.Row) -> Listing:
        keys = set(row.keys())
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
            price_value=float(row["price_value"]) if "price_value" in keys and row["price_value"] is not None else None,
            price_period=str(row["price_period"]) if "price_period" in keys and row["price_period"] is not None else None,
            price_monthly_eq=(
                float(row["price_monthly_eq"])
                if "price_monthly_eq" in keys and row["price_monthly_eq"] is not None
                else None
            ),
            removed_at=str(row["removed_at"]) if "removed_at" in keys and row["removed_at"] is not None else None,
            room_type=str(row["room_type"]) if "room_type" in keys and row["room_type"] is not None else None,
            facilities=Storage._facilities_from_db(
                str(row["facilities"]) if "facilities" in keys and row["facilities"] is not None else None
            ),
        )
