# /// script
# dependencies = ["matplotlib>=3.8"]
# ///
"""Draws the hosts of the stand and the delay of the probes, and writes the
summary table: uv run plots.py [--results DIR]

Reads DIR/<scenario>/raw/*.json for the scenarios control, own and overlap,
by default from results/ next to this file, and writes DIR/topology.png,
DIR/delay.png and DIR/summary.md. The chart has a panel per scenario with the
flapping run of the median delay and, as bands, the time T held each session
of A. A cell of the table is the median of the runs with min-max in brackets.
"""

import argparse
import json
import statistics
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
SCENARIOS = {"control": "A announces nothing", "own": "A announces prefixes of its own",
             "overlap": "A announces better paths for prefixes of B"}
FLAP = "#eb6834"
INK, MUTED, GRID, SURFACE, BAND = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb", "#eeede9"
GIB = 2**30


def runs(scenario, kind):
    out = []
    for f in sorted((RESULTS / scenario / "raw").glob("*.json")):
        r = json.loads(f.read_text())
        if (r["scenario"]["flap"]["interval"] > 0) == (kind == "flap"):
            out.append({**r["flap"], "rep": r["rep"]})
    return out


def cell(values, fmt):
    if None in values:
        return "—"
    med, lo, hi = (fmt(f(values)) for f in (statistics.median, min, max))
    return med if lo == hi else f"{med} ({lo}–{hi})"


def seconds(x):
    return f"{x * 1000:.0f} ms" if x < 1 else f"{x:.1f} s"


def count(x):
    return f"{x:,.0f}".replace(",", " ")


def ups(f):
    # the first session of A came up before the phase
    return sum(x["up"] > 0 for x in f["sessions"])


def summary():
    rows = [
        ("probe delay, median", lambda f: f["latency"]["p50"], seconds),
        ("probe delay, 99th percentile", lambda f: f["latency"]["p99"], seconds),
        ("probe delay, max", lambda f: f["latency"]["max"], seconds),
        ("probes held up over 100 ms, per time A came up", lambda f: f["latency"]["over_100ms"] / ups(f) if ups(f) else None,
         count),
        ("probes that reached C", lambda f: f["probes"] / f["sent_probes"], lambda x: f"{x:.0%}"),
        ("sessions of B and C that ended", lambda f: max(f["peers_down"], 0 if f["peers_kept"] else 1), count),
        ("times A came up in 120 s", ups, count),
        # the first session came up before the phase, without a table to build
        ("time T held a session of A, median of those that ended", lambda f: statistics.median(
            hi - lo for lo, hi in held(f) if lo > 0 and hi is not None) if len(f["sessions"]) > 1 else None, seconds),
        # the probes and markers left out; with better paths also the moves back to B
        ("routes announced to C in the phase", lambda f: f["receiver_routes"]["announced"], count),
        ("routes withdrawn from C in the phase", lambda f: f["receiver_routes"]["withdrawn"], count),
        ("peak RSS of gobgpd", lambda f: f["rss_peak"] / GIB, lambda x: f"{x:.1f} GiB"),
        ("CPU of gobgpd in the phase", lambda f: f["cpu"], lambda x: f"{x:.0f} s"),
    ]
    cols = [("A stays up, own prefixes", runs("own", "steady")), ("A flaps, announces nothing", runs("control", "flap")),
            ("A flaps, own prefixes", runs("own", "flap")), ("A flaps, better paths", runs("overlap", "flap"))]
    lines = ["| | " + " | ".join(name for name, _ in cols) + " |", "|---" * (len(cols) + 1) + "|"]
    for what, get, fmt in rows:
        lines.append(f"| {what} | " + " | ".join(cell([get(f) for f in rs], fmt) for _, rs in cols) + " |")
    return "\n".join(lines) + "\n"


def box(ax, x, y, text, sub, color=INK):
    ax.add_patch(FancyBboxPatch((x - 1.7, y - 0.5), 3.4, 1.0, boxstyle="round,pad=0.02,rounding_size=0.12",
                                facecolor=SURFACE, edgecolor=color, linewidth=1.5))
    ax.text(x, y + 0.2, text, ha="center", va="center", fontsize=12, fontweight="bold", color=INK)
    ax.text(x, y - 0.17, sub, ha="center", va="center", fontsize=8.5, color=MUTED)


def arrow(ax, x0, y0, x1, y1, text, color=MUTED, dashed=False):
    ax.annotate("", (x1, y1), (x0, y0), arrowprops=dict(arrowstyle="-|>", color=color, lw=1.5,
                                                         linestyle=(0, (4, 3)) if dashed else "-"))
    ax.text((x0 + x1) / 2, (y0 + y1) / 2 + 0.17, text, ha="center", va="bottom", fontsize=8, color=MUTED)


def topology(path):
    fig, ax = plt.subplots(figsize=(10, 3.8), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    ax.set_xlim(-0.8, 13.8)
    ax.set_ylim(-0.6, 2.6)
    ax.axis("off")
    box(ax, 1.1, 1.9, "B", "sender (blaster)\n1.5 M routes and probes")
    box(ax, 6.5, 1.9, "T", "gobgpd under test\nmaster f7daa917, 2 CPUs")
    box(ax, 11.9, 1.9, "C", "receiver (sink)\nrecords the probe delay")
    box(ax, 6.5, 0.1, "A", "flapper, AS 400\nannounces 100 000 routes", color=FLAP)
    arrow(ax, 2.85, 1.9, 4.75, 1.9, "100 probes/s")
    arrow(ax, 8.25, 1.9, 10.15, 1.9, "probes and\nroutes of A")
    arrow(ax, 6.3, 1.35, 6.3, 0.65, "", color=FLAP, dashed=True)
    arrow(ax, 6.7, 0.65, 6.7, 1.35, "", color=FLAP)
    ax.text(6.9, 1.0, "whole table to A, its routes back to T", ha="left", va="center", fontsize=8.5, color=FLAP)
    fig.savefig(path, dpi=150, bbox_inches="tight")


def held(flap):
    """When T held the sessions of A, from Peer Up to Peer Down in its log,
    in seconds from the start of the phase. The flapper saw each session come
    up a moment after T, so its second session places the log on the time of
    the phase. A session still up at the end of the phase ends at None."""
    events = []
    for line in flap["log"]:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("Key") == "127.0.0.3" and e["msg"] in ("Peer Up", "Peer Down"):
            events.append((datetime.fromisoformat(e["time"].replace("Z", "+00:00")).timestamp(), e["msg"]))
    ups = [t for t, m in events if m == "Peer Up"]
    offset = flap["sessions"][1]["up"] - ups[1]
    out = [(t0 + offset, t1 + offset) for (t0, m0), (t1, m1) in zip(events, events[1:])
           if m0 == "Peer Up" and m1 == "Peer Down"]
    if events and events[-1][1] == "Peer Up":
        out.append((events[-1][0] + offset, None))
    return out


def median_run(rs):
    return sorted(rs, key=lambda f: f["latency"]["p50"])[len(rs) // 2]


def delay(path):
    fig, axes = plt.subplots(len(SCENARIOS), 1, figsize=(10, 9.5), facecolor=SURFACE, sharex=True, sharey=True)
    for ax, (scenario, title) in zip(axes, SCENARIOS.items()):
        flap = median_run(runs(scenario, "flap"))
        ax.set_facecolor(SURFACE)
        first = True
        for lo, hi in held(flap):
            hi = 120 if hi is None else hi
            if hi > 0:
                ax.axvspan(max(lo, 0), min(hi, 120), color=BAND, zorder=0,
                           label="T holds the session of A (Peer Up → Peer Down in its log)" if first else None)
                first = False
        t, d = zip(*flap["probe_series"])
        ax.plot(t, d, color=FLAP, linewidth=1.5, label="probe delay B → C")
        ax.set_title(f"{title} (run {flap['rep'] + 1} of 3, the one with the median delay)",
                     loc="left", color=INK, fontsize=11)
        ax.set_ylabel("delay, s", color=MUTED)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(GRID)
        ax.tick_params(colors=MUTED)
    axes[-1].set_xlim(0, 120)
    axes[-1].set_ylim(0, None)
    axes[-1].set_xlabel("probe sent at, s", color=MUTED)
    axes[-1].legend(loc="upper center", bbox_to_anchor=(0.5, -0.22), ncol=2, frameon=False, labelcolor=INK)
    fig.suptitle("Delay of the routes of B through T while A flaps, 1 500 000 routes in the table",
                 x=0.07, ha="left", color=INK, fontsize=12)
    fig.savefig(path, dpi=150, bbox_inches="tight")


def main():
    global RESULTS
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--results", type=Path, default=RESULTS)
    RESULTS = p.parse_args().results
    topology(RESULTS / "topology.png")
    delay(RESULTS / "delay.png")
    (RESULTS / "summary.md").write_text(summary())


if __name__ == "__main__":
    main()
