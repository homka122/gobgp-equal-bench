"""Writes the table of the graceful restart runs: python3 gr_summary.py [--results DIR]

Reads DIR/<variant>/gr-restart*/raw/*.json, by default from results/ next to
this file, and writes DIR/gr-summary.md. A cell is the median of the runs with
min-max in brackets. The counts are what the receiver got from the drop of
the session on, the markers of the bench left out; the times are seconds
from the drop.
"""

import argparse
import json
import statistics
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
MIB = 2**20
VARIANTS = {"master": "master", "pr3933": "PR 3933"}
SCENARIOS = {
    "gr-restart": "100 000 routes, the receiver keeps up",
    "gr-restart-slow-receiver": "100 000 routes, the receiver reads 20 000 routes per second",
    "gr-restart-slow-receiver-500k": "500 000 routes, the receiver reads 20 000 routes per second",
}


def cell(values, fmt="{:.0f}"):
    values = [v for v in values if v is not None]
    if not values:
        return "–"
    med, lo, hi = (fmt.format(f(values)) for f in (statistics.median, min, max))
    return med if lo == hi else f"{med} ({lo}–{hi})"


def totals(r):
    w = r["restart"]["windows"].values()
    return {k: sum(x[k] for x in w) for k in ("nlri", "withdrawn", "llgr_stale")}


def table(results):
    out = []
    for scenario, title in SCENARIOS.items():
        found = {v: sorted((results / v / scenario / "raw").glob("*.json")) for v in VARIANTS}
        if not any(found.values()):
            continue
        out += [f"## {scenario}: {title}", "",
                "| pause, s | receiver LLGR | gobgpd | back after, s | peak RSS on the load, MiB | peak RSS after the drop, MiB "
                "| with LLGR_STALE | withdrawn | announced without LLGR_STALE | last route, s | table as after the load |",
                "|---|---|---|---|---|---|---|---|---|---|---|"]
        runs = {}
        for v, files in found.items():
            for f in files:
                r = json.loads(f.read_text())
                rs = r["scenario"]["restart"]
                runs.setdefault((rs["pause"], rs["receiver_llgr"]), {}).setdefault(v, []).append(r)
        for (pause, llgr), by in sorted(runs.items()):
            for v, rr in by.items():
                t = [totals(r) for r in rr]
                rs = [r["restart"] for r in rr]
                last = [max(x for s in r["sinks"] for x in (s["last_announce"], s["last_withdraw"], s["last_llgr_stale"])
                            if x is not None) if any(r["windows"][k]["nlri"] + r["windows"][k]["withdrawn"] for k in r["windows"])
                        else None for r in rs]
                same = sum(all(s["same_table"] for s in r["sinks"]) for r in rs)
                out.append(" | ".join([
                    f"| {pause:g}", "yes" if llgr else "no", VARIANTS[v],
                    cell([r["windows"]["up"]["start"] + r["up_connect_seconds"] for r in rs], "{:.1f}"),
                    cell([r["load"]["rss_peak"] / MIB for r in rr]),
                    cell([r["rss_peak"] / MIB for r in rs]),
                    cell([x["llgr_stale"] for x in t]), cell([x["withdrawn"] for x in t]),
                    cell([x["nlri"] - x["llgr_stale"] for x in t]),
                    cell(last, "{:.1f}"), f"{same} of {len(rs)} |"]))
        out.append("")
    return "\n".join(out)


def main():
    global RESULTS
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--results", type=Path, default=RESULTS)
    RESULTS = p.parse_args().results
    text = "# Graceful restart of the sender\n\n" + table(RESULTS)
    (RESULTS / "gr-summary.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
