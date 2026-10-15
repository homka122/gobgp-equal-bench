"""Draws the charts and a summary table from the results of run.py and micro.py.

Every bar is the median over the repetitions, the whiskers show min and max.
"""

import argparse
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
COLORS = {"base": "#9e9e9e", "order": "#1f77b4", "alloc1": "#ffbb78", "alloc": "#ff7f0e", "both": "#2ca02c"}
LABELS = {"base": "base", "order": "order fix", "alloc1": "alloc fix, step 1", "alloc": "alloc fix", "both": "both fixes"}
FAMILIES = ["ipv4", "vpnv4"]


def load_runs(raw):
    runs = defaultdict(list)
    for p in sorted(raw.glob("*.json")):
        r = json.loads(p.read_text())
        runs[(r["variant"], r["family"], r["mode"])].append(r)
    return runs


def stat(values):
    return statistics.median(values), min(values), max(values)


def bars(ax, groups, variants, value, fmt="{:.0f}", ylabel=""):
    """Grouped bars: one group per (label, key), one bar per variant."""
    width = 0.8 / len(variants)
    for i, v in enumerate(variants):
        xs, meds, errs = [], [], [[], []]
        for g, (_, key) in enumerate(groups):
            vals = value(v, key)
            if not vals:
                continue
            med, lo, hi = stat(vals)
            xs.append(g + (i - (len(variants) - 1) / 2) * width)
            meds.append(med)
            errs[0].append(med - lo)
            errs[1].append(hi - med)
        b = ax.bar(xs, meds, width, yerr=errs, capsize=3, color=COLORS[v], label=LABELS[v])
        ax.bar_label(b, labels=[fmt.format(m) for m in meds], fontsize=8, padding=2)
    ax.set_xticks(range(len(groups)), [label for label, _ in groups])
    ax.set_ylabel(ylabel)
    ax.margins(y=0.15)
    ax.spines[["top", "right"]].set_visible(False)


def legend(ax):
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.0), ncols=5, frameon=False)


def save(fig, path, title, caption):
    fig.suptitle(title, fontsize=12, fontweight="bold")
    fig.text(0.5, 0.01, caption, ha="center", fontsize=9, style="italic", wrap=True)
    fig.tight_layout(rect=(0, 0.05, 1, 0.95))
    fig.savefig(path, dpi=130)
    plt.close(fig)


def storm(runs, key, field, per_route=False):
    out = []
    for r in runs.get(key, []):
        v = r["storm"][field]
        out.append(v / (r["n"] * r["rounds"]) if per_route else v)
    return out


def parse_micro(dir):
    """{variant: {benchmark: {unit: [values]}}}"""
    res = {}
    for p in sorted(dir.glob("*.txt")):
        d = defaultdict(lambda: defaultdict(list))
        for line in p.read_text().splitlines():
            if not line.startswith("Benchmark"):
                continue
            f = line.split()
            name = re.sub(r"-\d+$", "", f[0])
            for value, unit in zip(f[2::2], f[3::2]):
                d[name][unit].append(float(value))
        res[p.stem] = d
    return res


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results", type=Path, default=ROOT / "results")
    args = p.parse_args()
    out = args.results / "plots"
    out.mkdir(exist_ok=True)
    runs = load_runs(args.results / "raw")
    n = next(iter(runs.values()))[0]
    total = n["n"] * n["rounds"]
    reorder = [(f"{fam}\n{m}", (fam, m)) for fam in FAMILIES for m in ("reorder-comm", "reorder-attrs")]
    summary = []

    # 1. forwarded routes
    fig, ax = plt.subplots(figsize=(9, 4.5))
    bars(ax, reorder, ["base", "order"], lambda v, k: storm(runs, (v, *k), "sink_nlri"),
         ylabel="routes sent to the downstream peer")
    legend(ax)
    save(fig, out / "1-forwarded.png", "Same routes, different order: what goes downstream",
         f"The peer re-announces {n['n']} unchanged routes {n['rounds']} times, every other time with the "
         "communities (or the attributes) in another order.")

    # 2. CPU in a reorder storm
    fig, ax = plt.subplots(figsize=(9, 4.5))
    bars(ax, reorder, ["base", "order"], lambda v, k: storm(runs, (v, *k), "cpu"), fmt="{:.2f}",
         ylabel="gobgpd CPU time, s")
    legend(ax)
    save(fig, out / "2-cpu-reorder.png", "CPU of gobgpd in the same storm",
         f"{total} re-announcements, every other one reordered. CPU is user + system time of gobgpd.")

    # 3. cost when the order is stable
    change = [(fam, (fam, "change")) for fam in FAMILIES]
    fig, ax = plt.subplots(figsize=(7, 4.5))
    bars(ax, change, ["base", "order"], lambda v, k: storm(runs, (v, *k), "cpu"), fmt="{:.2f}",
         ylabel="gobgpd CPU time, s")
    legend(ax)
    save(fig, out / "3-cpu-change.png", "Cost of the order fix when nothing is reordered",
         f"{total} re-announcements in canonical order, each one changes a community and is sent downstream.")

    # 4. Equal micro benchmark
    micro = parse_micro(args.results / "micro")
    if micro:
        eq = [(fam, f"BenchmarkStandEqual/{fam}") for fam in FAMILIES]
        mv = ["base", "alloc1", "alloc"]
        fig, axs = plt.subplots(1, 3, figsize=(13, 4.5))
        for ax, unit, yl, fmt in zip(axs, ["ns/op", "B/op", "allocs/op"],
                                     ["ns per call", "bytes per call", "allocations per call"],
                                     ["{:.0f}", "{:.0f}", "{:.0f}"]):
            bars(ax, eq, mv, lambda v, k: micro.get(v, {}).get(k, {}).get(unit, []), fmt=fmt, ylabel=yl)
        legend(axs[1] if len(axs) == 3 else axs[0])
        save(fig, out / "4-equal-micro.png", "Path.Equal on a re-announced route",
             "go test benchmark: Equal on two received copies of the same route. "
             "Step 1: no copy of the attributes. Step 2: no serialization of IPv4/IPv6 prefixes.")

    # 5. allocations in a storm of unchanged routes
    same = [(fam, (fam, "same")) for fam in FAMILIES]
    fig, axs = plt.subplots(1, 3, figsize=(13, 4.5))
    bars(axs[0], same, ["base", "alloc"], lambda v, k: storm(runs, (v, *k), "total_alloc", True),
         ylabel="bytes allocated per re-announcement")
    bars(axs[1], same, ["base", "alloc"], lambda v, k: storm(runs, (v, *k), "num_gc"), ylabel="GC cycles")
    bars(axs[2], same, ["base", "alloc"], lambda v, k: storm(runs, (v, *k), "cpu"), fmt="{:.2f}",
         ylabel="gobgpd CPU time, s")
    legend(axs[1] if len(axs) == 3 else axs[0])
    save(fig, out / "5-alloc-storm.png", "Storm of unchanged re-announcements",
         f"The peer re-announces {n['n']} routes {n['rounds']} times without any change. "
         "Measured in the live gobgpd: memory allocated, GC cycles and CPU for the whole storm.")

    # 6. all variants in every storm
    modes = [(m, ("ipv4", m)) for m in ("same", "change", "reorder-comm", "reorder-attrs")]
    fig, ax = plt.subplots(figsize=(11, 4.5))
    bars(ax, modes, ["base", "order", "alloc", "both"], lambda v, k: storm(runs, (v, *k), "cpu"), fmt="{:.2f}",
         ylabel="gobgpd CPU time, s")
    legend(ax)
    save(fig, out / "6-all-variants.png", "Both fixes together, IPv4",
         f"CPU of gobgpd for {total} re-announcements in each mode. The order fix removes the work for "
         "reordered routes, the alloc fix makes every comparison cheaper.")

    # 7. initial load
    variants = ["base", "order", "alloc", "both"]
    fams = [(fam, fam) for fam in FAMILIES]

    def load(field):
        return lambda v, fam: [r["load"][field] / 2**20 for (vv, f, _), rs in runs.items()
                               if vv == v and f == fam for r in rs]

    fig, axs = plt.subplots(1, 2, figsize=(13, 4.5))
    bars(axs[0], fams, variants, load("rss_peak"), ylabel="peak RSS, MiB")
    bars(axs[1], fams, variants, load("total_alloc"), ylabel="allocated during load, MiB")
    legend(axs[1] if len(axs) == 3 else axs[0])
    save(fig, out / "7-initial-load.png", "Initial load of the table",
         f"The peer announces {n['n']} new routes once. Path.Equal is not called for new routes, "
         "so the fixes do not change the memory of the initial load.")

    # summary table
    summary.append("| family | mode | variant | CPU, s | alloc/route, B | GC | routes downstream |")
    summary.append("|---|---|---|---|---|---|---|")
    for fam in FAMILIES:
        for mode in ["same", "change", "reorder-comm", "reorder-attrs"]:
            for v in variants:
                k = (v, fam, mode)
                if k not in runs:
                    continue
                summary.append(
                    f"| {fam} | {mode} | {v} | {stat(storm(runs, k, 'cpu'))[0]:.2f} | "
                    f"{stat(storm(runs, k, 'total_alloc', True))[0]:.0f} | {stat(storm(runs, k, 'num_gc'))[0]:.0f} | "
                    f"{stat(storm(runs, k, 'sink_nlri'))[0]:.0f} |")
    (args.results / "summary.md").write_text("\n".join(summary) + "\n")
    print(f"wrote {out} and {args.results / 'summary.md'}")


if __name__ == "__main__":
    main()
