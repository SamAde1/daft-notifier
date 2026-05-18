from __future__ import annotations

import statistics
from pathlib import Path


def _sort_counties_for_output(counties: set[str]) -> list[str]:
    ordered = sorted([county for county in counties if county != "Other"])
    if "Other" in counties:
        ordered.append("Other")
    return ordered


def render_regional_chart(
    *,
    search_name: str,
    county_prices: dict[str, list[float]],
    county_distances: dict[str, list[float]],
    budget: float,
    output_path: Path,
) -> None:
    import matplotlib.pyplot as plt  # pyright: ignore[reportMissingImports]
    import matplotlib.ticker as ticker  # pyright: ignore[reportMissingImports]

    counties = _sort_counties_for_output({county for county, prices in county_prices.items() if prices})
    if not counties:
        return

    cmap = plt.get_cmap("tab10")
    bar_colors = [cmap(index % 10) for index, _ in enumerate(counties)]

    fig, axes = plt.subplots(2, 2, figsize=(13, 9), constrained_layout=True)
    ax_median, ax_box, ax_count, ax_dist = axes[0, 0], axes[0, 1], axes[1, 0], axes[1, 1]

    # -- Median asking price by county with budget line --
    medians = [statistics.median(county_prices[county]) for county in counties]
    x_max = max(max(medians), budget) * 1.18
    bars = ax_median.barh(counties, medians, color=bar_colors, edgecolor="white", height=0.55)
    ax_median.axvline(
        budget,
        color="#D32F2F",
        linestyle="--",
        linewidth=1.5,
        label=f"Budget EUR{budget / 1000:.0f}k",
    )
    for bar, median_value in zip(bars, medians):
        ax_median.text(
            median_value + x_max * 0.01,
            bar.get_y() + bar.get_height() / 2,
            f"EUR{median_value / 1000:.0f}k",
            va="center",
            fontsize=10,
            fontweight="bold",
        )
    ax_median.set_xlim(0, x_max)
    ax_median.set_title("Median Asking Price", fontsize=12, fontweight="bold")
    ax_median.xaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"EUR{x / 1000:.0f}k"))
    ax_median.legend(loc="lower right", fontsize=9)
    ax_median.invert_yaxis()

    # -- Price spread box plot --
    bp = ax_box.boxplot(
        [county_prices[county] for county in counties],
        tick_labels=counties,
        patch_artist=True,
        vert=True,
        widths=0.5,
        medianprops={"color": "black", "linewidth": 1.5},
    )
    for patch, color in zip(bp["boxes"], bar_colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.55)
    ax_box.axhline(budget, color="#D32F2F", linestyle="--", linewidth=1.5)
    ax_box.set_title("Price Spread", fontsize=12, fontweight="bold")
    ax_box.set_ylabel("EUR")
    ax_box.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"EUR{x / 1000:.0f}k"))

    # -- Listings available + budget affordability --
    counts = [len(county_prices[county]) for county in counties]
    budget_hits = [sum(1 for price in county_prices[county] if price <= budget) for county in counties]
    budget_pcts = [(hits / count * 100) if count > 0 else 0 for hits, count in zip(budget_hits, counts)]
    bars = ax_count.bar(counties, counts, color=bar_colors, edgecolor="white", width=0.55)
    max_count = max(counts) if counts else 0
    for bar, pct, count in zip(bars, budget_pcts, counts):
        ax_count.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + max(max_count * 0.03, 0.5),
            f"{count}\n({pct:.0f}% <= budget)",
            ha="center",
            va="bottom",
            fontsize=9,
        )
    ax_count.set_title("Listings Available", fontsize=12, fontweight="bold")
    ax_count.set_ylabel("Count")
    ax_count.set_ylim(0, max(max_count * 1.3, 1))

    # -- Median distance to city centre --
    dist_counties = [county for county in counties if county_distances.get(county)]
    med_dists = [statistics.median(county_distances[county]) for county in dist_counties]
    if dist_counties:
        dist_color_map = {county: color for county, color in zip(counties, bar_colors)}
        d_colors = [dist_color_map[county] for county in dist_counties]
        d_max = max(med_dists) * 1.25
        bars = ax_dist.barh(dist_counties, med_dists, color=d_colors, edgecolor="white", height=0.55)
        for bar, distance_value in zip(bars, med_dists):
            ax_dist.text(
                distance_value + d_max * 0.01,
                bar.get_y() + bar.get_height() / 2,
                f"{distance_value:.1f} km",
                va="center",
                fontsize=10,
                fontweight="bold",
            )
        ax_dist.set_xlim(0, d_max)
        ax_dist.set_title("Median Distance to City Centre", fontsize=12, fontweight="bold")
        ax_dist.set_xlabel("km")
        ax_dist.invert_yaxis()
    else:
        ax_dist.set_title("Median Distance to City Centre", fontsize=12, fontweight="bold")
        ax_dist.text(
            0.5,
            0.5,
            "No distance data",
            ha="center",
            va="center",
            transform=ax_dist.transAxes,
            fontsize=12,
            color="#999",
        )

    fig.suptitle(
        f"{search_name}\nBudget: EUR{budget / 1000:.0f}k",
        fontsize=14,
        fontweight="bold",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(fig)
