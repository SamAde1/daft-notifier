#!/usr/bin/env python3
"""
Offline market-observation analytics.

Reads a listings.db snapshot and reports price distributions, distance-price
gradient, weekly supply/velocity, and posting-time patterns for a named
segment. Time-on-market survival is added once enough post-lifecycle-v2
deep-scan removal data exists.

This never runs inside the monitor containers — copy the DB to a laptop and
run it there. Install extras first: pip install -e ".[scripts]"

Examples:
    python scripts/analyze_market.py --data-dir ./data --segment all
    python scripts/analyze_market.py --data-dir ./data-sales \\
        --segment dublin_kildare_meath_houses --generate-image true
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from daft_monitor import analytics
from daft_monitor.config import load_config
from daft_monitor.logging_setup import parse_bool
from daft_monitor.search_identity import resolved_search_id

MIN_TIME_ON_MARKET_SAMPLES = 30


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze observation-mode market data.")
    parser.add_argument("--data-dir", default="./data", help="Data directory containing listings.db.")
    parser.add_argument(
        "--segment",
        default="all",
        help=f"Named segment filter. Known: {', '.join(sorted(analytics.SEGMENTS))}",
    )
    parser.add_argument("--config", default=None, help="Config path, to restrict time-on-market to deep_scan searches.")
    parser.add_argument("--window-weeks", type=int, default=12, help="Weeks of history for the velocity chart.")
    parser.add_argument(
        "--generate-image",
        nargs="?",
        const=True,
        default=False,
        type=parse_bool,
        help="Generate PNG chart(s) in reports/ (default: false).",
    )
    return parser.parse_args()


def _deep_scan_search_ids(config_path: str | None) -> set[str]:
    """Restrict time-on-market to deep_scan=true searches only."""
    try:
        config = load_config(config_path)
    except Exception as exc:
        print(f"(config not loaded, suppressing time-on-market: {exc})")
        return set()
    return {resolved_search_id(search) for search in config.searches if search.deep_scan}


def _print_price_summary(title: str, values: pd.Series) -> None:
    summary = analytics.price_summary(values)
    print(f"[{title}]")
    if not summary:
        print("  No parseable prices.")
        return
    print(f"  n={summary['count']} | median={summary['median']:,.0f} | mean={summary['mean']:,.0f}")
    print(f"  min/max={summary['min']:,.0f}/{summary['max']:,.0f}")
    print(f"  p10/p25/p75/p90={summary['p10']:,.0f}/{summary['p25']:,.0f}/{summary['p75']:,.0f}/{summary['p90']:,.0f}")


def run_analysis(
    *,
    data_dir: str,
    segment_key: str,
    config_path: str | None,
    window_weeks: int,
    generate_image: bool,
) -> None:
    db_path = Path(data_dir) / "listings.db"
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return

    conn = analytics.connect(db_path)
    try:
        listings_df = analytics.load_listings(conn)
        membership_events_df = analytics.load_membership_events(conn)
        episodes_df = analytics.load_episodes(conn)
        v2_started_at = analytics.get_lifecycle_v2_started_at(conn)
        analytics_v2_started_at = analytics.get_analytics_v2_started_at(conn)
    finally:
        conn.close()

    membership_events_df = analytics.trusted_membership_events(membership_events_df, analytics_v2_started_at)
    membership_events_df = analytics.trusted_lifecycle_events(membership_events_df, v2_started_at)

    segment_listings = analytics.apply_segment(listings_df, segment_key)
    segment_events = analytics.apply_segment(membership_events_df, segment_key)

    print("=" * 72)
    print(f"MARKET OBSERVATION - segment: {segment_key}")
    print(f"({analytics.SEGMENTS[segment_key].description})")
    print("=" * 72)
    if analytics_v2_started_at is None:
        print("analytics_v2 not yet started on this DB (no snapshot/episode trust boundary).")
    else:
        print(f"analytics_v2 started_at: {analytics_v2_started_at.isoformat()}")
    print(f"Total listings in DB: {len(listings_df)} | in segment: {len(segment_listings)}")
    print()

    print("-- PRICE DISTRIBUTION (active listings in segment) --")
    active_segment = segment_listings[segment_listings["is_active"] == 1]
    _print_price_summary("current active", active_segment["analysis_price"])
    print()

    cutoff = datetime.now(timezone.utc) - pd.Timedelta(weeks=window_weeks)
    recent_new = segment_events[
        segment_events["event_type"].isin(["new", "relisted"]) & (segment_events["timestamp"] >= cutoff)
    ]
    print(f"-- NEW LISTING PRICE TREND (last {window_weeks} weeks) --")
    if recent_new.empty:
        print("  No new/relisted events in window.")
    else:
        trend = recent_new.copy()
        trend["week"] = trend["timestamp"].dt.tz_convert("UTC").dt.to_period("W").dt.start_time
        weekly_median = trend.groupby("week")["analysis_price"].median().dropna()
        for week, median_price in weekly_median.items():
            print(f"  {week.date()}: median {median_price:,.0f} (n={int((trend['week'] == week).sum())})")
    print()

    print("-- DISTANCE-PRICE GRADIENT --")
    dist_pairs = active_segment.dropna(subset=["distance_to_location", "analysis_price"])
    distance_and_price = list(zip(dist_pairs["distance_to_location"].tolist(), dist_pairs["analysis_price"].tolist()))
    if distance_and_price:
        centers, medians = analytics.binned_medians(
            dist_pairs["distance_to_location"], dist_pairs["analysis_price"], bin_size=2.0
        )
        print(f"  {len(distance_and_price)} priced listings with distance data.")
        for center, median_price in zip(centers, medians):
            print(f"  ~{center:.1f}km: median {median_price:,.0f}")
    else:
        print("  No listings with both distance and price data.")
    print()

    print("-- WEEKLY SUPPLY / VELOCITY --")
    velocity = analytics.weekly_velocity(segment_events)
    if not velocity.empty:
        # weekly_velocity()'s "week" column is tz-naive (Period.start_time drops tz).
        cutoff_naive = pd.Timestamp(cutoff).tz_localize(None)
        velocity = velocity[velocity["week"] >= cutoff_naive]
    if velocity.empty:
        print("  No event data.")
    else:
        for _, row in velocity.iterrows():
            print(
                f"  {row['week'].date()}: added={int(row['supply_added'])} "
                f"removed={int(row['supply_removed'])} net={int(row['net_supply_change'])}"
            )
    print()

    print("-- POSTING-TIME PATTERNS (new+seed events) --")
    hours, days = analytics.posting_time_patterns(segment_events)
    if hours.empty:
        print("  No posting events.")
    else:
        peak_hour = int(hours.idxmax())
        peak_day = str(days.idxmax())
        print(f"  Peak posting hour (UTC): {peak_hour}:00 ({int(hours.max())} listings)")
        print(f"  Peak posting day: {peak_day} ({int(days.max())} listings)")
    print()

    print("-- TIME ON MARKET --")
    search_ids = _deep_scan_search_ids(config_path)
    segment_episodes = analytics.apply_segment(episodes_df, segment_key)
    if not search_ids:
        print("  No deep_scan searches configured; time-on-market suppressed.")
        tom = pd.DataFrame()
    else:
        tom = analytics.time_on_market_frame(
            segment_episodes,
            analytics_v2_started_at=analytics_v2_started_at,
            now=pd.Timestamp.now(tz="UTC"),
            search_ids=search_ids,
        )
    observed_count = int(tom["observed"].sum()) if not tom.empty else 0
    if observed_count < MIN_TIME_ON_MARKET_SAMPLES:
        print(
            f"  Not enough clean post-lifecycle-v2 removals yet "
            f"({observed_count}/{MIN_TIME_ON_MARKET_SAMPLES} needed). Check back after more deep scans complete."
        )
        tom = pd.DataFrame()
    else:
        durations = tom["duration_days"].tolist()
        observed_flags = tom["observed"].tolist()
        times, survival = analytics.kaplan_meier(durations, observed_flags)
        print(
            f"  Samples: {len(durations)} ({observed_count} observed removals, {len(durations) - observed_count} censored)"
        )
        median_survival_time = next((t for t, s in zip(times, survival) if s <= 0.5), None)
        if median_survival_time is not None:
            print(f"  Median time-on-market: ~{median_survival_time:.1f} days (50% still-listed point)")
        else:
            print("  Median time-on-market: not reached within observed range.")

    if generate_image:
        from market_charts import render_market_dashboard, render_survival_curve

        report_dir = Path(__file__).resolve().parent.parent / "reports"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        try:
            render_market_dashboard(
                segment_key=segment_key,
                prices=active_segment["analysis_price"].dropna().tolist(),
                distance_and_price=distance_and_price,
                velocity_weeks=velocity["week"].tolist() if not velocity.empty else [],
                velocity_added=velocity["supply_added"].tolist() if not velocity.empty else [],
                velocity_removed=velocity["supply_removed"].tolist() if not velocity.empty else [],
                posting_hours=hours.to_dict(),
                output_path=report_dir / f"market_{segment_key}_{ts}.png",
            )
            print(f"\nSaved dashboard chart: {report_dir / f'market_{segment_key}_{ts}.png'}")
        except Exception as exc:
            print(f"\nDashboard chart generation skipped: {exc}")

        if not tom.empty:
            try:
                render_survival_curve(
                    segment_key=segment_key,
                    times=times,
                    survival=survival,
                    sample_count=len(durations),
                    output_path=report_dir / f"market_{segment_key}_survival_{ts}.png",
                )
                print(f"Saved survival chart: {report_dir / f'market_{segment_key}_survival_{ts}.png'}")
            except Exception as exc:
                print(f"Survival chart generation skipped: {exc}")


def main() -> None:
    args = parse_args()
    if args.segment not in analytics.SEGMENTS:
        print(f"Unknown segment {args.segment!r}. Known: {', '.join(sorted(analytics.SEGMENTS))}")
        raise SystemExit(1)
    run_analysis(
        data_dir=args.data_dir,
        segment_key=args.segment,
        config_path=args.config,
        window_weeks=args.window_weeks,
        generate_image=args.generate_image,
    )


if __name__ == "__main__":
    main()
