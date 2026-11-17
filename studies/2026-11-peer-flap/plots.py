# /// script
# dependencies = ["matplotlib>=3.8"]
# ///
"""Draws the hosts of the stand and the delay of the probes, and writes the
summary table: uv run plots.py [--results DIR]

Reads DIR/1500k/flap/raw/*.json, by default from results/ next to this file,
and writes DIR/topology.png, DIR/delay.png and DIR/summary.md. The chart
shows the flapping run and the steady run with the median delay, and as
bands the time T held each session of A. A cell of the table is the median
of the runs with min-max in brackets.
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
SET = "1500k"
STEADY, FLAP = "#2a78d6", "#eb6834"
INK, MUTED, GRID, SURFACE, BAND = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb", "#eeede9"
GIB = 2**30


def runs(kind):
    out = []
    for f in sorted((RESULTS / SET / "flap" / "raw").glob("*.json")):
        r = json.loads(f.read_text())
        if (r["scenario"]["flap"]["interval"] > 0) == (kind == "flap"):
            out.append({**r["flap"], "rep": r["rep"]})
    return out


def cell(values, fmt):
    med, lo, hi = (fmt(f(values)) for f in (statistics.median, min, max))
    return med if lo == hi else f"{med} ({lo}–{hi})"


def seconds(x):
    return f"{x * 1000:.0f} мс" if x < 1 else f"{x:.1f} с".replace(".", ",")


def summary():
    rows = [
        ("задержка проб, медиана", lambda f: f["latency"]["p50"], seconds),
        ("задержка проб, 99-й перцентиль", lambda f: f["latency"]["p99"], seconds),
        ("задержка проб, максимум", lambda f: f["latency"]["max"], seconds),
        ("пробы дошли до C", lambda f: f["probes"] / f["sent_probes"], lambda x: f"{x:.0%}"),
        # peers_down: sessions T ended; peers_kept: B and C did not end theirs
        ("сессий B и C оборвалось", lambda f: max(f["peers_down"], 0 if f["peers_kept"] else 1), lambda x: f"{x:.0f}"),
        # the first session of A came up before the phase
        ("подъёмов A за 60 с", lambda f: sum(x["up"] > 0 for x in f["sessions"]), lambda x: f"{x:.0f}"),
        ("пиковый RSS gobgpd", lambda f: f["rss_peak"] / GIB, lambda x: f"{x:.1f} ГиБ".replace(".", ",")),
    ]
    steady, flap = runs("steady"), runs("flap")
    lines = ["| | A подключён | A моргает |", "|---|---|---|"]
    for what, get, fmt in rows:
        lines.append(f"| {what} | {cell([get(f) for f in steady], fmt)} | {cell([get(f) for f in flap], fmt)} |")
    return "\n".join(lines) + "\n"


def box(ax, x, y, text, sub, color=INK):
    ax.add_patch(FancyBboxPatch((x - 1.7, y - 0.5), 3.4, 1.0, boxstyle="round,pad=0.02,rounding_size=0.12",
                                facecolor=SURFACE, edgecolor=color, linewidth=1.5))
    ax.text(x, y + 0.2, text, ha="center", va="center", fontsize=12, fontweight="bold", color=INK)
    ax.text(x, y - 0.17, sub, ha="center", va="center", fontsize=8.5, color=MUTED)


def arrow(ax, x0, y0, x1, y1, text, color=MUTED, dashed=False):
    ax.annotate("", (x1, y1), (x0, y0), arrowprops=dict(arrowstyle="-|>", color=color, lw=1.5,
                                                         linestyle=(0, (4, 3)) if dashed else "-"))
    ax.text((x0 + x1) / 2, (y0 + y1) / 2 + 0.17, text, ha="center", va="bottom", fontsize=8.5, color=MUTED)


def topology(path):
    fig, ax = plt.subplots(figsize=(10, 3.8), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    ax.set_xlim(-0.8, 13.8)
    ax.set_ylim(-0.6, 2.6)
    ax.axis("off")
    box(ax, 1.1, 1.9, "B", "отправитель (blaster)\n1,5 млн маршрутов и пробы")
    box(ax, 6.5, 1.9, "T", "gobgpd под тестом\nmaster f7daa917, 2 ядра")
    box(ax, 11.9, 1.9, "C", "приёмник (sink)\nмеряет задержку проб")
    box(ax, 6.5, 0.1, "A", "приёмник (flapper)\nрвёт сессию через 1 с", color=FLAP)
    arrow(ax, 2.85, 1.9, 4.75, 1.9, "100 проб/с")
    arrow(ax, 8.25, 1.9, 10.15, 1.9, "пробы")
    arrow(ax, 6.5, 1.35, 6.5, 0.65, "", color=FLAP, dashed=True)
    ax.text(6.65, 1.0, "вся таблица при каждом подъёме A", ha="left", va="center", fontsize=8.5, color=FLAP)
    fig.savefig(path, dpi=150, bbox_inches="tight")


def held(flap):
    """When T held the sessions of A, from Peer Up to Peer Down in its log,
    in seconds from the start of the phase: T sends A the whole table in
    that time. The flapper saw each session come up a moment after T, so
    its second session places the log on the time of the phase."""
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
    return [(t0 + offset, t1 + offset) for (t0, m0), (t1, m1) in zip(events, events[1:])
            if m0 == "Peer Up" and m1 == "Peer Down"]


def median_run(rs):
    return sorted(rs, key=lambda f: f["latency"]["p50"])[len(rs) // 2]


def delay(path):
    steady, flap = median_run(runs("steady")), median_run(runs("flap"))
    fig, ax = plt.subplots(figsize=(10, 4.2), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    first = True
    for lo, hi in held(flap):
        if hi > 0:
            ax.axvspan(max(lo, 0), min(hi, 60), color=BAND, zorder=0,
                       label="T держит сессию A (Peer Up → Peer Down в его логе)" if first else None)
            first = False
    for f, color, name in ((flap, FLAP, "A моргает"), (steady, STEADY, "A подключён")):
        t, d = zip(*f["probe_series"])
        ax.plot(t, d, color=color, linewidth=2, label=name)
    ax.set_xlim(0, 60)
    ax.set_ylim(0, None)
    ax.set_xlabel("время отправки пробы, с", color=MUTED)
    ax.set_ylabel("задержка пробы B → C, с", color=MUTED)
    ax.set_title(f"Задержка маршрутов B через T, 1 500 000 маршрутов в таблице\n"
                 f"повтор {flap['rep'] + 1} из 3, медианный по задержке", loc="left", color=INK, fontsize=12)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=3, frameon=False, labelcolor=INK)
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
