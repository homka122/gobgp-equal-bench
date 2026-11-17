# /// script
# dependencies = ["matplotlib>=3.8"]
# ///
"""Draws the memory of gobgpd over the storm and writes the summary table:
uv run plots.py [--results DIR]

Reads DIR/<variant>/<scenario>/raw/*.json, by default from results/ next to
this file, and writes DIR/rss.png and DIR/summary.md. The variants are the
directories base, pr2869 and master. A line of the chart is the run with the
median peak; a cell of the table is the median of the runs with min-max in
brackets.
"""

import argparse
import json
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
MIB = 2**20
VARIANTS = {  # directory -> (name in the chart and the table, colour)
    "base": ("before PR 2869", "#2a78d6"),
    "pr2869": ("PR 2869, buffer channels", "#eb6834"),
    "master": ("master", "#1baf7a"),
}
SCENARIOS = {  # directory -> title of the panel
    "flood": "flood: the receiver keeps up",
    "slow-receiver": "slow-receiver: the receiver reads 20 000 routes per second",
}
RESENDS = {"same": "byte-identical", "changed-community": "community changed"}
INK, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


def runs(variant, scenario, resend):
    out = []
    for f in sorted((RESULTS / variant / scenario / "raw").glob("*.json")):
        r = json.loads(f.read_text())
        if r["scenario"]["resend"] == resend:
            out.append(r)
    return out


def cell(values, fmt="{:.0f}"):
    med, lo, hi = (fmt.format(f(values)) for f in (statistics.median, min, max))
    return med if lo == hi else f"{med} ({lo}–{hi})"


def summary():
    lines = [
        "| scenario | re-announcements | gobgpd | peak RSS, MiB | delivered to the receiver | sender done, s | storm done, s |",
        "|---|---|---|---|---|---|---|",
    ]
    for scenario in SCENARIOS:
        for resend, what in RESENDS.items():
            for variant, (name, _) in VARIANTS.items():
                rs = [r["storm"] for r in runs(variant, scenario, resend)]
                if not rs:
                    continue
                lines.append(
                    f"| {scenario} | {what} | {name} | {cell([r['rss_peak'] / MIB for r in rs])} | "
                    f"{cell([r['sink_nlri'] for r in rs])} | {cell([r['sent_seconds'] for r in rs], '{:.1f}')} | "
                    f"{cell([r['seconds'] for r in rs], '{:.1f}')} |"
                )
    load = [r["load"]["rss_end"] / MIB for v in VARIANTS for s in SCENARIOS for x in RESENDS for r in runs(v, s, x)]
    lines += ["", f"RSS before the storm: {min(load):.0f}–{max(load):.0f} MiB in all runs."]
    (RESULTS / "summary.md").write_text("\n".join(lines) + "\n")


def chart():
    plt.rcParams.update({"font.size": 10, "font.family": "DejaVu Sans", "text.color": INK,
                         "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED})
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6), sharey=True, facecolor=SURFACE,
                             gridspec_kw={"width_ratios": [1, 2.2], "wspace": 0.06})
    lines = {}  # (scenario, variant) -> seconds, MiB of the repetition with the median peak
    for scenario in SCENARIOS:
        for variant in VARIANTS:
            rs = sorted(runs(variant, scenario, "changed-community"), key=lambda r: r["storm"]["rss_peak"])
            if rs:
                storm = rs[len(rs) // 2]["storm"]
                t = [0.0] + [s[0] for s in storm["rss_series"]] + [storm["seconds"]]
                rss = [storm["rss_series"][0][1]] + [s[1] for s in storm["rss_series"]] + [storm["rss_end"]]
                lines[scenario, variant] = t, [x / MIB for x in rss]
    top = max(max(rss) for _, rss in lines.values())
    peaks = {sc: [max(rss) for (x, _), (_, rss) in lines.items() if x == sc] for sc in SCENARIOS}
    for ax, (scenario, title) in zip(axes, SCENARIOS.items()):
        ax.set_facecolor(SURFACE)
        for variant, (name, colour) in VARIANTS.items():
            if (scenario, variant) not in lines:
                continue
            t, rss = lines[scenario, variant]
            ax.plot(t, rss, color=colour, linewidth=2, solid_capstyle="round", solid_joinstyle="round", label=name)
            # The label goes to the peak, under the line when another line is
            # above it. The lowest line of a panel gets the label at its end.
            at = max(range(len(rss)), key=rss.__getitem__)
            text = f"{max(rss):,.0f} MiB".replace(",", " ")
            where = {"xytext": (8, 5)}
            if max(rss) < top / 8:
                if max(rss) == min(peaks[scenario]):
                    at = len(rss) - 1
            elif max(rss) < max(peaks[scenario]):
                where = {"xytext": (-8, -6), "ha": "right", "va": "top"}
            ax.plot(t[at], rss[at], "o", color=colour, markersize=8, markeredgecolor=SURFACE, markeredgewidth=2)
            ax.annotate(text, (t[at], rss[at]), textcoords="offset points", color=INK, fontsize=9, **where)
        ax.set_title(title, loc="left", fontsize=10, color=INK, pad=10)
        ax.set_xlabel("seconds since the storm began")
        ax.set_xlim(left=0)
        ax.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(nbins=7, integer=True, steps=[1, 2, 5, 10]))
        ax.set_yscale("log")
        ax.set_ylim(30, 3000)
        ax.yaxis.set_major_locator(matplotlib.ticker.FixedLocator([50, 100, 1000, 2000]))
        ax.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())
        ax.grid(axis="y", color=GRID, linewidth=1)
        ax.set_axisbelow(True)
        ax.tick_params(length=0)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(GRID)
    axes[0].set_ylabel("gobgpd RSS, MiB, log scale")
    axes[0].yaxis.set_major_formatter(lambda v, _: f"{v:,.0f}".replace(",", " "))
    handles, labels = axes[1].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper left", bbox_to_anchor=(0.06, 0.93), ncols=3, frameon=False, fontsize=9,
               handlelength=1.6, columnspacing=2.2)
    fig.suptitle("gobgpd under 1 000 000 re-announcements of 10 000 routes", x=0.065, y=0.99, ha="left",
                 fontsize=12, fontweight="bold", color=INK)
    fig.subplots_adjust(left=0.065, right=0.985, top=0.80, bottom=0.12)
    fig.savefig(RESULTS / "rss.png", dpi=160, facecolor=SURFACE)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--results", type=Path, default=RESULTS, help="directory with the results of the variants")
    RESULTS = p.parse_args().results.resolve()
    summary()
    chart()
