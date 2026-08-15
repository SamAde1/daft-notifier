"""Matplotlib chart renderers for scripts/analyze_market.py.

Kept separate from analyze_market.py so the CLI can run --generate-image
false without importing matplotlib, matching the existing sales_charts.py /
listings_stats.py convention.
"""

from __future__ import annotations

from pathlib import Path


def render_market_dashboard(
    *,
    segment_key: str,
    prices: list[float],
    distance_and_price: list[tuple[float, float]],
    velocity_weeks: list,
    velocity_added: list[int],
    velocity_removed: list[int],
    posting_hours: dict[int, int],
    output_path: Path,
) -> None:
    import matplotlib.pyplot as plt  # pyright: ignore[reportMissingImports]
    import matplotlib.ticker as ticker  # pyright: ignore[reportMissingImports]
    import pandas as pd

    from daft_monitor.analytics import binned_medians

    fig, axes = plt.subplots(2, 2, figsize=(13, 9), constrained_layout=True)
    ax_hist, ax_dist, ax_velocity, ax_hours = axes[0, 0], axes[0, 1], axes[1, 0], axes[1, 1]

    if prices:
        ax_hist.hist(prices, bins=20, color="#5b8ff9", edgecolor="#1f2937", alpha=0.85)
        median_ = sorted(prices)[len(prices) // 2]
        ax_hist.axvline(median_, color="#ef4444", linestyle="--", linewidth=2, label=f"Median {median_:,.0f}")
        ax_hist.legend(fontsize=9)
        ax_hist.xaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    else:
        ax_hist.text(0.5, 0.5, "No priced listings", ha="center", va="center", transform=ax_hist.transAxes)
    ax_hist.set_title("Price Distribution", fontsize=12, fontweight="bold")

    if distance_and_price:
        xs = [d for d, _ in distance_and_price]
        ys = [p for _, p in distance_and_price]
        ax_dist.scatter(xs, ys, s=20, alpha=0.45, color="#60a5fa", edgecolor="none", label="Listings")
        centers, medians = binned_medians(pd.Series(xs), pd.Series(ys), bin_size=2.0)
        if centers:
            ax_dist.plot(centers, medians, color="#ef4444", linewidth=2.2, marker="o", markersize=4, label="Median/2km")
        ax_dist.legend(fontsize=9)
    else:
        ax_dist.text(0.5, 0.5, "No distance data", ha="center", va="center", transform=ax_dist.transAxes)
    ax_dist.set_title("Price vs Distance", fontsize=12, fontweight="bold")
    ax_dist.set_xlabel("km")

    if velocity_weeks:
        width = 0.4
        indices = range(len(velocity_weeks))
        ax_velocity.bar([i - width / 2 for i in indices], velocity_added, width=width, label="Added", color="#22c55e")
        ax_velocity.bar([i + width / 2 for i in indices], velocity_removed, width=width, label="Removed", color="#ef4444")
        ax_velocity.set_xticks(list(indices))
        ax_velocity.set_xticklabels([str(w)[:10] for w in velocity_weeks], rotation=60, ha="right", fontsize=8)
        ax_velocity.legend(fontsize=9)
    else:
        ax_velocity.text(0.5, 0.5, "No event data", ha="center", va="center", transform=ax_velocity.transAxes)
    ax_velocity.set_title("Weekly Supply Added/Removed", fontsize=12, fontweight="bold")

    if posting_hours:
        hours = sorted(posting_hours)
        counts = [posting_hours[h] for h in hours]
        ax_hours.bar(hours, counts, color="#a78bfa", edgecolor="white")
        ax_hours.set_xticks(range(0, 24, 2))
    else:
        ax_hours.text(0.5, 0.5, "No posting-time data", ha="center", va="center", transform=ax_hours.transAxes)
    ax_hours.set_title("Postings by Hour of Day", fontsize=12, fontweight="bold")
    ax_hours.set_xlabel("Hour (UTC)")

    fig.suptitle(f"Market Observation — segment: {segment_key}", fontsize=14, fontweight="bold")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def render_survival_curve(
    *,
    segment_key: str,
    times: list[float],
    survival: list[float],
    sample_count: int,
    output_path: Path,
) -> None:
    import matplotlib.pyplot as plt  # pyright: ignore[reportMissingImports]

    fig, ax = plt.subplots(figsize=(9, 6), constrained_layout=True)
    ax.step(times, survival, where="post", color="#0ea5e9", linewidth=2.2)
    ax.set_ylim(0, 1.05)
    ax.set_xlabel("Days on market")
    ax.set_ylabel("Fraction still listed")
    ax.set_title(f"Time-on-Market Survival — {segment_key} (n={sample_count})", fontsize=13, fontweight="bold")
    ax.grid(True, alpha=0.3)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(fig)
