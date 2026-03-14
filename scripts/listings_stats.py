#!/usr/bin/env python3
"""
Calculate monthly listing statistics from the Daft listings DB.

Rules:
- Weekly prices are converted to monthly by multiplying by 4.
- "From EURX to EURY": use latter if bedrooms are "Single & Double Room",
  former if "Double & Twin Room", otherwise midpoint.
- Excludes outliers: price < EUR500 per month.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

# Allow running from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daft_monitor.logging_setup import parse_bool  # noqa: E402

def parse_price_monthly(price_str: str, bedrooms: str | None) -> tuple[float | None, str]:
    """Parse a listing price to monthly euros.

    Returns (monthly_price, parse_source), where parse_source is one of:
    - "from_to_month", "from_to_week", "single_month", "single_week", "unparseable"
    """
    if not price_str or not price_str.strip():
        return None, "unparseable"
    price_str = price_str.strip()
    bedrooms = (bedrooms or "").strip().lower()

    # "From EURX to EURY per month|week"
    from_to = re.match(
        r"from\s+(?:€|eur)?\s*([\d,]+)\s+to\s+(?:€|eur)?\s*([\d,]+)\s+per\s+(month|week)\b",
        price_str,
        re.I,
    )
    if from_to:
        low = float(from_to.group(1).replace(",", ""))
        high = float(from_to.group(2).replace(",", ""))
        period = from_to.group(3).lower()

        if "single" in bedrooms and "double" in bedrooms and "twin" not in bedrooms:
            chosen = high  # Single & Double Room -> latter
        elif "double" in bedrooms and "twin" in bedrooms:
            chosen = low  # Double & Twin Room -> former
        else:
            chosen = (low + high) / 2  # fallback for other bedroom strings

        if period == "week":
            return chosen * 4, "from_to_week"
        return chosen, "from_to_month"

    # "EURX per month"
    per_month = re.search(r"(?:€|eur)?\s*([\d,]+)\s+per\s+month\b", price_str, re.I)
    if per_month:
        return float(per_month.group(1).replace(",", "")), "single_month"

    # "EURX per week" -> * 4 for monthly
    per_week = re.search(r"(?:€|eur)?\s*([\d,]+)\s+per\s+week\b", price_str, re.I)
    if per_week:
        return float(per_week.group(1).replace(",", "")) * 4, "single_week"

    return None, "unparseable"


def _binned_medians(
    distance_and_price: list[tuple[float, float]],
    *,
    bin_size_km: float = 2.0,
) -> tuple[list[float], list[float]]:
    """Return (bin_centers, median_prices) for distance bins."""
    if not distance_and_price:
        return [], []
    max_distance = max(d for d, _ in distance_and_price)
    bins_count = int(max_distance // bin_size_km) + 1

    centers: list[float] = []
    medians: list[float] = []
    for b in range(bins_count):
        low = b * bin_size_km
        high = low + bin_size_km
        values = [p for d, p in distance_and_price if low <= d < high]
        if values:
            centers.append(low + (bin_size_km / 2.0))
            medians.append(statistics.median(values))
    return centers, medians


def render_metrics_chart(
    monthly_prices: list[float],
    distance_and_price: list[tuple[float, float]],
    output_path: Path,
) -> None:
    """Render a readable 2x2 dashboard chart with key pricing metrics."""
    import matplotlib.pyplot as plt  # pyright: ignore[reportMissingImports]

    mean_ = statistics.mean(monthly_prices)
    median_ = statistics.median(monthly_prices)
    min_p = min(monthly_prices)
    max_p = max(monthly_prices)
    if len(monthly_prices) >= 2:
        q1, _, q3 = statistics.quantiles(monthly_prices, n=4, method="inclusive")
    else:
        q1 = q3 = monthly_prices[0]

    plt.style.use("seaborn-v0_8-whitegrid")
    fig, axes = plt.subplots(2, 2, figsize=(13, 8), constrained_layout=True)
    fig.patch.set_facecolor("#f8fafc")

    ax_hist, ax_box, ax_dist, ax_text = axes[0, 0], axes[0, 1], axes[1, 0], axes[1, 1]

    # Distribution histogram with reference lines.
    ax_hist.hist(monthly_prices, bins=16, color="#5b8ff9", edgecolor="#1f2937", alpha=0.85)
    ax_hist.axvline(mean_, color="#ef4444", linestyle="--", linewidth=2, label=f"Mean EUR{mean_:,.0f}")
    ax_hist.axvline(median_, color="#0ea5e9", linestyle="-", linewidth=2, label=f"Median EUR{median_:,.0f}")
    ax_hist.axvspan(q1, q3, color="#93c5fd", alpha=0.25, label="Q1-Q3")
    ax_hist.set_title("Monthly Price Distribution", fontsize=12, weight="bold")
    ax_hist.set_xlabel("Price (EUR / month)")
    ax_hist.set_ylabel("Listings")
    ax_hist.legend(frameon=True, fontsize=9)

    # Horizontal boxplot for quick spread/outlier reading.
    ax_box.boxplot(
        monthly_prices,
        vert=False,
        patch_artist=True,
        boxprops={"facecolor": "#86efac", "color": "#166534"},
        medianprops={"color": "#ef4444", "linewidth": 2},
        whiskerprops={"color": "#166534"},
        capprops={"color": "#166534"},
        flierprops={"marker": "o", "markersize": 4, "markerfacecolor": "#f59e0b", "alpha": 0.6},
    )
    ax_box.set_title("Boxplot (Spread + Outliers)", fontsize=12, weight="bold")
    ax_box.set_xlabel("Price (EUR / month)")
    ax_box.set_yticks([])

    # Option C: scatter points + binned median trend line.
    if distance_and_price:
        xs = [d for d, _ in distance_and_price]
        ys = [p for _, p in distance_and_price]
        ax_dist.scatter(xs, ys, s=26, alpha=0.5, color="#60a5fa", edgecolor="none", label="Listings")

        centers, medians = _binned_medians(distance_and_price, bin_size_km=2.0)
        if centers:
            ax_dist.plot(
                centers,
                medians,
                color="#ef4444",
                linewidth=2.4,
                marker="o",
                markersize=4,
                label="Median by 2km bin",
            )
        ax_dist.legend(frameon=True, fontsize=9)
    ax_dist.set_title("Price vs Distance from City Centre", fontsize=12, weight="bold")
    ax_dist.set_xlabel("Distance to location (km)")
    ax_dist.set_ylabel("Monthly price (EUR)")

    # Text summary card.
    try:
        stdev_ = statistics.stdev(monthly_prices)
    except statistics.StatisticsError:
        stdev_ = 0.0
    if len(monthly_prices) >= 2:
        deciles = statistics.quantiles(monthly_prices, n=10, method="inclusive")
        p10, p90 = deciles[0], deciles[8]
    else:
        p10 = p90 = monthly_prices[0]
    summary_lines = [
        f"Listings included: {len(monthly_prices)}",
        f"Median: EUR {median_:,.0f}",
        f"Mean: EUR {mean_:,.0f}",
        f"Std dev: EUR {stdev_:,.0f}",
        f"Min / Max: EUR {min_p:,.0f} / EUR {max_p:,.0f}",
        f"P10 / P90: EUR {p10:,.0f} / EUR {p90:,.0f}",
        f"Distance points: {len(distance_and_price)}",
    ]
    ax_text.set_title("Key Metrics", fontsize=12, weight="bold")
    ax_text.axis("off")
    ax_text.text(
        0.02,
        0.95,
        "\n".join(summary_lines),
        transform=ax_text.transAxes,
        va="top",
        ha="left",
        fontsize=11,
        bbox={"boxstyle": "round,pad=0.6", "facecolor": "#eef2ff", "edgecolor": "#6366f1"},
    )

    fig.suptitle("Daft Listings: Monthly Price Dashboard", fontsize=15, weight="bold")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def run_stats(generate_image: bool = False) -> None:
    data_dir = Path(__file__).resolve().parent.parent / "data"
    db_path = data_dir / "listings.db"
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT price, bedrooms, distance_to_location FROM listings").fetchall()
    conn.close()

    monthly_prices: list[float] = []
    parse_sources: dict[str, int] = {
        "single_month": 0,
        "single_week": 0,
        "from_to_month": 0,
        "from_to_week": 0,
        "unparseable": 0,
    }
    distance_and_price: list[tuple[float, float]] = []
    cost_per_km_values: list[float] = []
    skipped_unparseable = 0
    skipped_outlier = 0

    for row in rows:
        p, source = parse_price_monthly(row["price"], row["bedrooms"])
        parse_sources[source] = parse_sources.get(source, 0) + 1
        if p is None:
            skipped_unparseable += 1
            continue
        if p < 500:
            skipped_outlier += 1
            continue

        monthly_prices.append(p)

        d = row["distance_to_location"]
        if d is not None:
            distance_km = float(d)
            distance_and_price.append((distance_km, p))
            if distance_km > 0:
                cost_per_km_values.append(p / distance_km)

    n = len(monthly_prices)
    if n == 0:
        print("No listings with valid monthly price >= EUR500.")
        print(f"Skipped (unparseable): {skipped_unparseable}, (outlier < EUR500): {skipped_outlier}")
        return

    monthly_prices.sort()
    median_ = statistics.median(monthly_prices)
    mean_ = statistics.mean(monthly_prices)
    try:
        stdev_ = statistics.stdev(monthly_prices)
    except statistics.StatisticsError:
        stdev_ = 0.0

    try:
        variance_ = statistics.variance(monthly_prices)
    except statistics.StatisticsError:
        variance_ = 0.0
    min_p = min(monthly_prices)
    max_p = max(monthly_prices)
    if n >= 2:
        q1, _, q3 = statistics.quantiles(monthly_prices, n=4, method="inclusive")
    else:
        q1, q3 = min_p, max_p
    iqr = q3 - q1
    if n >= 2:
        deciles = statistics.quantiles(monthly_prices, n=10, method="inclusive")
        p10, p90 = deciles[0], deciles[8]
    else:
        p10 = p90 = monthly_prices[0]
    mad = statistics.median(abs(x - median_) for x in monthly_prices)
    trimmed = monthly_prices[int(0.1 * n) : n - int(0.1 * n)] if n >= 10 else monthly_prices
    trimmed_mean = statistics.mean(trimmed)

    print("=" * 60)
    print("MONTHLY PRICE STATISTICS (EUR/month, excluding < EUR500)")
    print("=" * 60)
    print(f"  Total rooms included:      {n}")
    print(f"  Skipped (unparseable):     {skipped_unparseable}")
    print(f"  Skipped (outlier <EUR500): {skipped_outlier}")
    print(f"  Listings with distance:    {len(distance_and_price)}")
    print()
    print("  Parsed from:")
    print(f"    Single monthly prices:   {parse_sources['single_month']}")
    print(f"    Single weekly prices:    {parse_sources['single_week']}")
    print(f"    From-to monthly ranges:  {parse_sources['from_to_month']}")
    print(f"    From-to weekly ranges:   {parse_sources['from_to_week']}")
    print()
    print(f"  Median:                    EUR{median_:,.2f}")
    print(f"  Mean:                      EUR{mean_:,.2f}")
    print(f"  10% trimmed mean:          EUR{trimmed_mean:,.2f}")
    print(f"  Standard deviation:        EUR{stdev_:,.2f}")
    print(f"  Variance (EUR^2):          {variance_:,.2f}")
    print(f"  Median absolute dev.:      EUR{mad:,.2f}")
    print()
    print(f"  Min:                       EUR{min_p:,.2f}")
    print(f"  Max:                       EUR{max_p:,.2f}")
    print(f"  P10 (10th percentile):     EUR{p10:,.2f}")
    print(f"  Q1 (25th percentile):      EUR{q1:,.2f}")
    print(f"  P90 (90th percentile):     EUR{p90:,.2f}")
    print(f"  Q3 (75th percentile):      EUR{q3:,.2f}")
    print(f"  IQR:                       EUR{iqr:,.2f}")
    if mean_ > 0:
        print(f"  Coefficient of variation:  {100 * stdev_ / mean_:.1f}%")

    if distance_and_price:
        dist_values = [d for d, _ in distance_and_price]
        print()
        print(f"  Median distance to centre: {statistics.median(dist_values):.2f} km")
        print(f"  Mean distance to centre:   {statistics.mean(dist_values):.2f} km")
    if cost_per_km_values:
        print(f"  Median cost per km:        EUR{statistics.median(cost_per_km_values):.2f} / km")
        print(f"  Mean cost per km:          EUR{statistics.mean(cost_per_km_values):.2f} / km")

    print("=" * 60)

    if generate_image:
        chart_path = Path(__file__).resolve().parent.parent / "reports" / (
            f"monthly_price_metrics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        )
        try:
            render_metrics_chart(monthly_prices, distance_and_price, chart_path)
            print(f"Saved chart: {chart_path}")
        except Exception as exc:
            print(f"Chart generation skipped: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute listing price stats from listings.db")
    parser.add_argument(
        "--generate-image",
        nargs="?",
        const=True,
        default=False,
        type=parse_bool,
        help="Generate summary PNG chart (default: false).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_stats(generate_image=args.generate_image)


if __name__ == "__main__":
    main()
