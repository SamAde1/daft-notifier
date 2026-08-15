"""Stage 5: shared analytics helpers — DB to pandas, v1-epoch exclusion, segments.

This module is imported only by offline `scripts/*.py` analysis tools. It is
never imported by the monitor runtime (`main.py`, `storage.py`, etc.), so
pandas is not a server dependency — see requirements-scripts.txt.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from daft_monitor.constants import (
    EVENT_NEW,
    EVENT_PRICE_CHANGE,
    EVENT_REMOVED,
    EVENT_RELISTED,
    EVENT_SEED,
)

# Columns every loader tries to expose so a Segment predicate can run against
# listings_df, events_df, or memberships_df interchangeably.
SEGMENT_COLUMNS = (
    "title", "location", "bedrooms", "price_monthly_eq", "price_value",
    "price_period", "analysis_price", "search_name", "room_type", "facilities",
)


def effective_analysis_price(
    price_value: float | None,
    price_period: str | None,
    price_monthly_eq: float | None,
) -> float | None:
    """Sales use price_value; rents/sharing use monthly equivalent."""
    if price_period == "sale":
        return float(price_value) if price_value is not None else None
    if price_monthly_eq is not None:
        return float(price_monthly_eq)
    if price_value is not None and price_period in {"month", "week"}:
        return float(price_value)
    return None


def _with_analysis_price(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        df = df.copy()
        df["analysis_price"] = pd.Series(dtype=float)
        return df
    out = df.copy()
    if "price_value" not in out.columns:
        out["price_value"] = None
    if "price_period" not in out.columns:
        out["price_period"] = None
    if "price_monthly_eq" not in out.columns:
        out["price_monthly_eq"] = None
    out["analysis_price"] = out.apply(
        lambda row: effective_analysis_price(
            row.get("price_value"),
            row.get("price_period"),
            row.get("price_monthly_eq"),
        ),
        axis=1,
    )
    return out


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _to_utc(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce")


def load_listings(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query("SELECT * FROM listings", conn)
    for col in ("first_seen", "last_seen", "removed_at"):
        if col in df.columns:
            df[col] = _to_utc(df[col])
    return _with_analysis_price(df)


def load_membership_events(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT
            e.id, e.listing_id, e.search_id, e.event_type, e.timestamp,
            e.title, e.location, e.bedrooms, e.price_raw, e.price_value,
            e.price_period, e.price_monthly_eq, e.room_type, e.facilities,
            s.name AS search_name
        FROM listing_search_events e
        JOIN searches s ON s.search_id = e.search_id
        """,
        conn,
    )
    df["timestamp"] = _to_utc(df["timestamp"])
    return _with_analysis_price(df)


def load_episodes(conn: sqlite3.Connection) -> pd.DataFrame:
    """Episodes with immutable segment attributes snapshotted at episode open.

    Attributes captured at open time win over current listing rows, so
    time-on-market segments reflect what the listing was when it entered the
    market, not its latest mutation.
    """
    df = pd.read_sql_query(
        """
        SELECT
            ep.id, ep.listing_id, ep.search_id, ep.started_at, ep.ended_at, ep.is_active,
            s.name AS search_name,
            COALESCE(ep.title, l.title) AS title,
            COALESCE(ep.location, l.location) AS location,
            COALESCE(ep.bedrooms, l.bedrooms) AS bedrooms,
            COALESCE(ep.room_type, l.room_type) AS room_type,
            COALESCE(ep.facilities, l.facilities) AS facilities,
            COALESCE(ep.price_value, l.price_value) AS price_value,
            COALESCE(ep.price_period, l.price_period) AS price_period,
            COALESCE(ep.price_monthly_eq, l.price_monthly_eq) AS price_monthly_eq,
            COALESCE(ep.distance_to_location, l.distance_to_location) AS distance_to_location
        FROM listing_search_episodes ep
        JOIN searches s ON s.search_id = ep.search_id
        JOIN listings l ON l.id = ep.listing_id
        """,
        conn,
    )
    for col in ("started_at", "ended_at"):
        df[col] = _to_utc(df[col])
    return _with_analysis_price(df)


def load_events(conn: sqlite3.Connection) -> pd.DataFrame:
    """Legacy global listing_events joined with current listing attributes."""
    df = pd.read_sql_query(
        """
        SELECT
            e.id, e.listing_id, e.event_type, e.timestamp, e.old_value, e.new_value,
            l.title, l.search_name, l.location, l.bedrooms,
            l.price_value, l.price_period, l.price_monthly_eq, l.room_type, l.facilities,
            l.distance_to_location
        FROM listing_events e
        JOIN listings l ON l.id = e.listing_id
        """,
        conn,
    )
    df["timestamp"] = _to_utc(df["timestamp"])
    return _with_analysis_price(df)


def load_memberships(conn: sqlite3.Connection) -> pd.DataFrame:
    """Per-(listing, search) membership rows — the Stage 3 lifecycle truth.

    Joined with `searches` (for search_name) and `listings` (for the segment
    columns), so this is the preferred source for time-on-market analysis:
    unlike `listings.is_active`, it distinguishes "gone from this search" per
    search rather than only the globally-removed flag.
    """
    df = pd.read_sql_query(
        """
        SELECT
            ls.listing_id, ls.search_id, ls.first_seen, ls.last_seen,
            ls.is_active, ls.removed_at,
            s.name AS search_name,
            l.title, l.location, l.bedrooms, l.price_value, l.price_period,
            l.price_monthly_eq, l.room_type, l.facilities, l.distance_to_location
        FROM listing_search_state ls
        JOIN searches s ON s.search_id = ls.search_id
        JOIN listings l ON l.id = ls.listing_id
        """,
        conn,
    )
    for col in ("first_seen", "last_seen", "removed_at"):
        df[col] = _to_utc(df[col])
    return _with_analysis_price(df)


def get_analytics_v2_started_at(conn: sqlite3.Connection) -> pd.Timestamp | None:
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = 'analytics_v2_started_at'"
    ).fetchone()
    if row is None:
        return None
    return _to_utc(pd.Series([row[0]])).iloc[0]


def trusted_membership_events(
    events_df: pd.DataFrame, analytics_v2_started_at: pd.Timestamp | None
) -> pd.DataFrame:
    if analytics_v2_started_at is None or events_df.empty:
        return events_df
    return events_df.loc[events_df["timestamp"] >= analytics_v2_started_at].reset_index(drop=True)


def get_lifecycle_v2_started_at(conn: sqlite3.Connection) -> pd.Timestamp | None:
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = 'lifecycle_v2_started_at'"
    ).fetchone()
    if row is None:
        return None
    return _to_utc(pd.Series([row[0]])).iloc[0]


def trusted_lifecycle_events(
    events_df: pd.DataFrame, v2_started_at: pd.Timestamp | None
) -> pd.DataFrame:
    """Drop removed/relisted events recorded before the Stage 3 lifecycle cutover.

    Pre-cutover removals/relists are artifacts of the page-limited-fetch bug
    (see Stage 3): a listing missing from a shallow, few-page fetch was
    marked removed even though it was still live. `new`/`seed`/`price_change`
    events are unaffected by that bug and are kept regardless of epoch.
    """
    if v2_started_at is None or events_df.empty:
        return events_df
    untrustworthy_types = {EVENT_REMOVED, EVENT_RELISTED}
    is_untrustworthy_type = events_df["event_type"].isin(untrustworthy_types)
    is_before_v2 = events_df["timestamp"] < v2_started_at
    return events_df.loc[~(is_untrustworthy_type & is_before_v2)].reset_index(drop=True)


# --------------------------------------------------------------------------
# Named segments — analysis-time filters that reproduce/replace old narrow
# live searches (see Stage 4: those became broad silent searches).
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Segment:
    key: str
    description: str
    predicate: Callable[[pd.DataFrame], pd.Series]


def _text_contains(df: pd.DataFrame, column: str, *keywords: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(False, index=df.index)
    values = df[column].fillna("").astype(str).str.lower()
    mask = pd.Series(False, index=df.index)
    for keyword in keywords:
        mask = mask | values.str.contains(keyword.lower(), regex=False)
    return mask


def _price_at_most(df: pd.DataFrame, max_price: float) -> pd.Series:
    if "analysis_price" not in df.columns:
        return pd.Series(False, index=df.index)
    return df["analysis_price"].fillna(float("inf")) <= max_price


def _room_is_double(df: pd.DataFrame) -> pd.Series:
    """Structured room_type when present; per-row fallback to bedrooms text."""
    if "room_type" in df.columns:
        room = df["room_type"].fillna("").astype(str).str.lower()
    else:
        room = pd.Series("", index=df.index)
    if "bedrooms" in df.columns:
        beds = df["bedrooms"].fillna("").astype(str).str.lower()
    else:
        beds = pd.Series("", index=df.index)
    return (room == "double") | ((room == "") & beds.str.contains("double", regex=False))


def _has_facility_keyword(df: pd.DataFrame, keyword: str) -> pd.Series:
    """Structured facilities when present; per-row fallback to title text."""
    keyword = keyword.lower()
    if "facilities" in df.columns:
        facilities = df["facilities"].fillna("").astype(str).str.lower()
    else:
        facilities = pd.Series("", index=df.index)
    if "title" in df.columns:
        titles = df["title"].fillna("").astype(str).str.lower()
    else:
        titles = pd.Series("", index=df.index)
    return facilities.str.contains(keyword, regex=False) | (
        (facilities == "") & titles.str.contains(keyword, regex=False)
    )


SEGMENTS: dict[str, Segment] = {
    "all": Segment("all", "Every row, no filter.", lambda df: pd.Series(True, index=df.index)),
    "dublin_sharing_ensuite_1400": Segment(
        "dublin_sharing_ensuite_1400",
        "Dublin sharing double room <=EUR1400/mo (ensuite via facilities or title proxy).",
        lambda df: (
            _text_contains(df, "location", "dublin")
            & _room_is_double(df)
            & _has_facility_keyword(df, "ensuite")
            & _price_at_most(df, 1400)
        ),
    ),
    "dublin_kildare_meath_houses": Segment(
        "dublin_kildare_meath_houses",
        "Sale houses across Dublin/Kildare/Meath (mirrors the retired bed-split searches).",
        lambda df: _text_contains(df, "location", "dublin", "kildare", "meath"),
    ),
    "commuter_belt_sub_500k": Segment(
        "commuter_belt_sub_500k",
        "Residential sales in Dublin/Kildare/Meath/Wicklow priced at or below EUR500k.",
        lambda df: (
            _text_contains(df, "location", "dublin", "kildare", "meath", "wicklow")
            & _price_at_most(df, 500000)
        ),
    ),
    "dublin_kildare_meath_wicklow": Segment(
        "dublin_kildare_meath_wicklow",
        "Four-county observation coverage for Dublin, Kildare, Meath, and Wicklow.",
        lambda df: _text_contains(df, "location", "dublin", "kildare", "meath", "wicklow"),
    ),
}


def apply_segment(df: pd.DataFrame, segment_key: str) -> pd.DataFrame:
    if segment_key not in SEGMENTS:
        raise KeyError(f"Unknown segment {segment_key!r}. Known segments: {sorted(SEGMENTS)}")
    segment = SEGMENTS[segment_key]
    return df.loc[segment.predicate(df)].reset_index(drop=True)


# --------------------------------------------------------------------------
# Summary statistics
# --------------------------------------------------------------------------


def price_summary(values: pd.Series) -> dict[str, float]:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    if clean.empty:
        return {}
    return {
        "count": int(clean.count()),
        "median": float(clean.median()),
        "mean": float(clean.mean()),
        "min": float(clean.min()),
        "max": float(clean.max()),
        "p10": float(clean.quantile(0.10)),
        "p25": float(clean.quantile(0.25)),
        "p75": float(clean.quantile(0.75)),
        "p90": float(clean.quantile(0.90)),
    }


def binned_medians(
    x: pd.Series, y: pd.Series, *, bin_size: float
) -> tuple[list[float], list[float]]:
    """Median of `y` in fixed-width bins of `x`. Used for distance-price gradients."""
    pairs = pd.DataFrame({"x": x, "y": y}).dropna()
    if pairs.empty:
        return [], []
    max_x = float(pairs["x"].max())
    bins_count = int(max_x // bin_size) + 1
    centers: list[float] = []
    medians: list[float] = []
    for b in range(bins_count):
        low, high = b * bin_size, (b + 1) * bin_size
        bucket = pairs.loc[(pairs["x"] >= low) & (pairs["x"] < high), "y"]
        if not bucket.empty:
            centers.append(low + bin_size / 2.0)
            medians.append(float(bucket.median()))
    return centers, medians


def weekly_velocity(events_df: pd.DataFrame) -> pd.DataFrame:
    """Weekly supply-added / supply-removed counts, a proxy for market tightness."""
    columns = ["week", EVENT_NEW, EVENT_SEED, EVENT_REMOVED, EVENT_RELISTED, EVENT_PRICE_CHANGE,
               "supply_added", "supply_removed", "net_supply_change"]
    if events_df.empty:
        return pd.DataFrame(columns=columns)
    df = events_df.copy()
    df["week"] = df["timestamp"].dt.tz_convert("UTC").dt.tz_localize(None).dt.to_period("W").dt.start_time
    counts = df.groupby(["week", "event_type"]).size().unstack(fill_value=0)
    for event_type in (EVENT_NEW, EVENT_SEED, EVENT_REMOVED, EVENT_RELISTED, EVENT_PRICE_CHANGE):
        if event_type not in counts.columns:
            counts[event_type] = 0
    counts["supply_added"] = counts[EVENT_NEW] + counts[EVENT_RELISTED]
    counts["supply_removed"] = counts[EVENT_REMOVED]
    counts["net_supply_change"] = counts["supply_added"] - counts["supply_removed"]
    if EVENT_SEED in counts.columns:
        counts["baseline_seed"] = counts[EVENT_SEED]
    else:
        counts["baseline_seed"] = 0
    return counts.reset_index()


def posting_time_patterns(events_df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Hour-of-day and day-of-week counts for new+relisted events."""
    day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    if events_df.empty:
        return pd.Series(dtype=int), pd.Series(0, index=day_order, dtype=int)
    new_like = events_df[events_df["event_type"].isin([EVENT_NEW, EVENT_RELISTED])]
    hours = new_like["timestamp"].dt.hour.value_counts().sort_index()
    days = new_like["timestamp"].dt.day_name().value_counts()
    days = days.reindex(day_order).fillna(0).astype(int)
    return hours, days


# --------------------------------------------------------------------------
# Time-on-market (requires clean post-v2 deep-scan removal data)
# --------------------------------------------------------------------------


def time_on_market_frame(
    episodes_df: pd.DataFrame,
    *,
    analytics_v2_started_at: pd.Timestamp | None,
    now: pd.Timestamp,
    search_ids: set[str] | None = None,
) -> pd.DataFrame:
    """Duration (days) + observed flag per episode from listing_search_episodes."""
    df = episodes_df.copy()
    if df.empty:
        return pd.DataFrame(columns=["duration_days", "observed"])
    df["started_at"] = pd.to_datetime(df["started_at"], utc=True, errors="coerce")
    df["ended_at"] = pd.to_datetime(df["ended_at"], utc=True, errors="coerce")
    if search_ids is not None:
        if not search_ids:
            return pd.DataFrame(columns=["duration_days", "observed"])
        df = df[df["search_id"].isin(search_ids)]
    if analytics_v2_started_at is not None:
        df = df.loc[df["started_at"] >= analytics_v2_started_at].reset_index(drop=True)
    end = df["ended_at"].fillna(now)
    df["duration_days"] = (end - df["started_at"]).dt.total_seconds() / 86400.0
    df["observed"] = df["is_active"] == 0
    return df.loc[df["duration_days"] >= 0].reset_index(drop=True)


def kaplan_meier(durations: list[float], observed: list[bool]) -> tuple[list[float], list[float]]:
    """Minimal Kaplan-Meier survival estimator (no external stats dependency).

    Returns (times, survival_probability) as a step function starting at
    (0, 1.0). `observed[i]=False` right-censors `durations[i]` (still active).
    """
    if not durations:
        return [0.0], [1.0]

    durations_arr = np.asarray(durations, dtype=float)
    observed_arr = np.asarray(observed, dtype=bool)
    order = np.argsort(durations_arr)
    durations_arr = durations_arr[order]
    observed_arr = observed_arr[order]

    event_times = sorted(set(durations_arr[observed_arr].tolist()))
    survival = 1.0
    times: list[float] = [0.0]
    probs: list[float] = [1.0]
    for t in event_times:
        at_risk = int((durations_arr >= t).sum())
        deaths = int(((durations_arr == t) & observed_arr).sum())
        if at_risk > 0:
            survival *= 1.0 - (deaths / at_risk)
        times.append(float(t))
        probs.append(survival)
    return times, probs
