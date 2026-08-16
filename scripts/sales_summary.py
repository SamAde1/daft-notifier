#!/usr/bin/env python3
"""
Generate a concise daily/weekly sales market summary from listings.db.

Always prints summary to terminal.
Optionally sends the summary to ntfy and/or saves to a local text file.
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daft_monitor.constants import EVENT_NEW, EVENT_PRICE_CHANGE, EVENT_RELISTED, EVENT_REMOVED
from sales_stats import extract_county, parse_sale_price


def _fmt_eur(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"EUR{value:,.0f}"


def _pool_name(search_name: str, combine_beds: bool) -> str:
    return "Combined 3/4 Bed" if combine_beds else search_name


def _sort_counties_for_output(counties: list[str] | set[str]) -> list[str]:
    without_other = sorted([county for county in counties if county != "Other"])
    if "Other" in counties:
        without_other.append("Other")
    return without_other


def _safe_topic_title(window_days: int) -> str:
    label = "Daily" if window_days == 1 else "Weekly"
    today = datetime.now(timezone.utc).date().isoformat()
    return f"Sales Summary - {label} - {today}"


def _send_ntfy(server: str, topic: str, token: str | None, title: str, body: str) -> bool:
    import requests

    url = f"{server.rstrip('/')}/{topic}"
    headers: dict[str, str] = {
        "Title": title.encode("latin-1", errors="replace").decode("latin-1")[:200],
        "Priority": "default",
        "Tags": "chart_with_upwards_trend",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    max_body_chars = 3800
    payload = body
    if len(payload) > max_body_chars:
        payload = payload[: max_body_chars - 29] + "\n\n[summary truncated for ntfy]"

    try:
        response = requests.post(url, data=payload.encode("utf-8"), headers=headers, timeout=20)
        response.raise_for_status()
        print(f"ntfy send OK: {response.status_code}")
        return True
    except Exception as exc:
        print(f"ntfy send failed: {exc}")
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a daily/weekly sales summary from listings.db")
    parser.add_argument("--data-dir", default="./data-sales", help="Data directory containing listings.db.")
    parser.add_argument("--window", type=int, default=7, help="Window in days (default: 7, use 1 for daily).")
    parser.add_argument("--budget", type=float, default=450000, help="Budget ceiling in EUR.")
    parser.add_argument("--combine-beds", action="store_true", help="Combine 3-bed and 4-bed searches.")
    parser.add_argument("--ntfy-server", default="https://ntfy.sh", help="ntfy server URL.")
    parser.add_argument("--ntfy-topic", default=None, help="Optional ntfy topic to send summary to.")
    parser.add_argument("--ntfy-token", default=None, help="Optional ntfy bearer token.")
    parser.add_argument("--save", default=None, help="Optional file path to save summary text.")
    return parser.parse_args()


def _fetch_active(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, title, search_name, location, COALESCE(last_price, price) AS current_price
        FROM listings
        WHERE is_active = 1
        """
    ).fetchall()


def _fetch_window_events(conn: sqlite3.Connection, cutoff_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            e.listing_id,
            e.event_type,
            e.timestamp,
            e.old_value,
            e.new_value,
            l.title,
            l.search_name,
            l.location,
            l.first_seen,
            COALESCE(l.last_price, l.price) AS current_price
        FROM listing_events e
        JOIN listings l ON l.id = e.listing_id
        WHERE e.timestamp >= ?
        ORDER BY e.timestamp DESC
        """,
        (cutoff_iso,),
    ).fetchall()


def _build_summary(*, conn: sqlite3.Connection, window_days: int, budget: float, combine_beds: bool) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    cutoff_iso = cutoff.isoformat()
    active_rows = _fetch_active(conn)
    event_rows = _fetch_window_events(conn, cutoff_iso)

    if not active_rows:
        return f"Sales Market Summary (last {window_days} day(s))\nNo active listings found."

    pool_prices: dict[str, list[float]] = defaultdict(list)
    pool_within_budget: dict[str, int] = defaultdict(int)
    county_prices: dict[str, list[float]] = defaultdict(list)
    total_parseable = 0

    for row in active_rows:
        pool = _pool_name(str(row["search_name"]), combine_beds)
        price, _source = parse_sale_price(row["current_price"])
        if price is None:
            continue
        total_parseable += 1
        pool_prices[pool].append(price)
        if price <= budget:
            pool_within_budget[pool] += 1
        county_prices[extract_county(str(row["location"]))].append(price)

    counts = defaultdict(int)
    new_prices: list[float] = []
    drop_rows: list[tuple[float, float, float, str]] = []
    activity_by_county = defaultdict(int)

    for row in event_rows:
        event_type = str(row["event_type"])
        counts[event_type] += 1
        activity_by_county[extract_county(str(row["location"]))] += 1

        if event_type == EVENT_NEW:
            new_price, _ = parse_sale_price(row["current_price"])
            if new_price is not None:
                new_prices.append(new_price)

        if event_type == EVENT_PRICE_CHANGE:
            old_price, _ = parse_sale_price(row["old_value"])
            new_price, _ = parse_sale_price(row["new_value"])
            if old_price is None or new_price is None or new_price >= old_price:
                continue
            reduction = old_price - new_price
            reduction_pct = (reduction / old_price) * 100.0 if old_price > 0 else 0.0
            title = str(row["title"])
            drop_rows.append((reduction, old_price, new_price, f"{title} ({reduction_pct:.1f}%)"))

    drop_rows.sort(key=lambda item: item[0], reverse=True)
    top_drops = drop_rows[:3]

    lines: list[str] = []
    lines.append(f"Sales Market Summary - {'Daily' if window_days == 1 else 'Weekly'}")
    lines.append(f"Window: last {window_days} day(s) since {cutoff.date().isoformat()}")
    lines.append("")
    lines.append("SNAPSHOT")
    lines.append(f"  Active parseable listings: {total_parseable}")

    if pool_prices:
        for pool in sorted(pool_prices.keys()):
            prices = pool_prices[pool]
            if not prices:
                continue
            median = float(statistics.median(prices))
            within = pool_within_budget.get(pool, 0)
            within_pct = (within / len(prices)) * 100.0 if prices else 0.0
            lines.append(
                f"  {pool}: n={len(prices)}, median={_fmt_eur(median)}, <=budget={within}/{len(prices)} ({within_pct:.1f}%)"
            )

    lines.append("")
    lines.append(f"WINDOW (last {window_days} day(s))")
    lines.append(
        "  "
        + f"New: {counts[EVENT_NEW]} | Removed: {counts[EVENT_REMOVED]} | "
        + f"Price changes: {counts[EVENT_PRICE_CHANGE]} | Relisted: {counts[EVENT_RELISTED]}"
    )
    if new_prices:
        lines.append(f"  New listing median asking: {_fmt_eur(float(statistics.median(new_prices)))}")

    if activity_by_county:
        lines.append("  Counties with most activity:")
        for county, c in sorted(activity_by_county.items(), key=lambda item: item[1], reverse=True):
            lines.append(f"    - {county}: {c}")

    if county_prices:
        lines.append("")
        lines.append("MEDIAN ASKING BY COUNTY (active)")
        for county in _sort_counties_for_output(set(county_prices.keys())):
            values = county_prices.get(county, [])
            if not values:
                continue
            lines.append(f"  {county}: n={len(values)}, median={_fmt_eur(float(statistics.median(values)))}")

    if top_drops:
        lines.append("")
        lines.append("BIGGEST PRICE DROPS")
        for reduction, old_price, new_price, title in top_drops:
            title_short = title[:50] + ("..." if len(title) > 50 else "")
            lines.append(f"  - {title_short}: {_fmt_eur(old_price)} -> {_fmt_eur(new_price)} (-{_fmt_eur(reduction)})")

    return "\n".join(lines)


def run_summary(
    *,
    data_dir: str,
    window_days: int,
    budget: float,
    combine_beds: bool,
    ntfy_server: str,
    ntfy_topic: str | None,
    ntfy_token: str | None,
    save_path: str | None,
) -> None:
    if window_days <= 0:
        raise ValueError("--window must be > 0.")

    db_path = Path(data_dir) / "listings.db"
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        summary = _build_summary(conn=conn, window_days=window_days, budget=budget, combine_beds=combine_beds)
    finally:
        conn.close()

    print(summary)

    if save_path:
        save_target = Path(save_path)
        save_target.parent.mkdir(parents=True, exist_ok=True)
        save_target.write_text(summary, encoding="utf-8")
        print(f"Summary saved to: {save_target}")

    if ntfy_topic:
        _send_ntfy(
            server=ntfy_server,
            topic=ntfy_topic,
            token=ntfy_token,
            title=_safe_topic_title(window_days),
            body=summary,
        )


def main() -> None:
    args = parse_args()
    run_summary(
        data_dir=args.data_dir,
        window_days=args.window,
        budget=args.budget,
        combine_beds=args.combine_beds,
        ntfy_server=args.ntfy_server,
        ntfy_topic=args.ntfy_topic,
        ntfy_token=args.ntfy_token,
        save_path=args.save,
    )


if __name__ == "__main__":
    main()
