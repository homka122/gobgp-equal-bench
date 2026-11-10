"""Compares the results of two gobgpd binaries: report.py BASE_DIR HEAD_DIR.

Every directory with run.py results (raw/*.json) that both have under the
same relative path becomes a table: a line per scenario and phase.

A cell is the median over the repetitions with min-max in brackets, Δ is the
difference of the two medians in % of the first. The two binaries were
measured one after the other, so a Δ within the min-max ranges is noise.
"""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

MIB = 2**20
median = statistics.median


def cell(values, fmt):
    med, lo, hi = fmt.format(median(values)), fmt.format(min(values)), fmt.format(max(values))
    return med if lo == hi else f"{med} ({lo}–{hi})"


def delta(b, h):
    if not median(b):
        return "–"
    x = 100 * (median(h) - median(b)) / median(b)
    return f"{x:+.0f}%" if abs(x) >= 10 else f"{x:+.1f}%"


def scenario_key(sc):
    c = sc["communities"]
    total = c["standard"] + c["extended"] + c["large"]
    communities = f"{c['standard']} + {c['extended']} + {c['large']}, {c['order']}" if total else "none"
    return sc["family"], communities, sc["receivers"], sc["resend"] if sc["rounds"] else "–"


def parts(d):
    """Relative path -> scenario -> runs, for every run.py output under d."""
    out = {}
    for raw in sorted(d.rglob("raw")):
        runs = defaultdict(list)
        for f in sorted(raw.glob("*.json")):
            r = json.loads(f.read_text())
            runs[scenario_key(r["scenario"])].append(r)
        if runs:
            out[raw.parent.relative_to(d).as_posix()] = runs
    return out


def env(d, part):
    f = d / part / "env.json"
    return json.loads(f.read_text()) if f.exists() else {}


def header(dirs, part):
    base, head = (d.name for d in dirs)
    lines = [f"# gobgpd: {head} against {base}", "", "| | gobgpd | revision | sha256 |", "|---|---|---|---|"]
    for d in dirs:
        g = env(d, part).get("gobgpd", {})
        revision = g.get("revision", "")[:10] + (", modified" if g.get("modified") == "true" else "")
        lines.append(f"| {d.name} | {g.get('version', '?')} | {revision or 'not in the binary'} | `{g.get('sha256', '?')[:12]}` |")
    e = env(dirs[1], part)
    if e:
        a = e["scenario"]["runtime"]
        lines += ["", f"{e['date']}, {e['platform']}, {e['cpus']} CPUs. gobgpd runs on CPUs {a['gobgpd_cpus']} with "
                      f"GOMAXPROCS={a['gomaxprocs']}, the sender on CPUs {a['sender_cpus']}, the receivers on "
                      f"{a['receiver_cpus']} (pinned on Linux only)."]
    return lines + [
        "",
        "A cell is the median over the repetitions, min–max in brackets. Δ is the difference of the medians in "
        f"% of {base}. The two binaries were measured one after the other: a Δ within the min–max ranges is noise. "
        "Communities are the numbers of standard + extended + large values per route and their order. "
        "Phase load: the table is announced for the first time; storm: it is announced again, as “resend” says. "
        "Sent on: routes that reached the receivers.",
        "",
    ]


def table(part, rb_all, rh_all, base, head):
    sc = next(iter(rb_all.values()))[0]["scenario"]
    lines = [
        f"## {part}",
        "",
        f"{sc['routes']} routes, {sc['routes_per_update']} per UPDATE, {sc['rounds']} storm rounds, "
        f"{len(next(iter(rb_all.values())))} and {len(next(iter(rh_all.values())))} repetitions.",
        "",
        f"| family | communities | receivers | resend | phase | sent on: {base} | {head} | CPU, s: {base} | {head} | Δ "
        f"| wall, s: {base} | {head} | allocated, MiB: {base} | {head} | Δ | peak RSS, MiB: {base} | {head} | Δ |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key in rb_all:
        rb, rh = rb_all[key], rh_all.get(key)
        if not rh:
            continue
        for phase in ("load", "storm"):
            if not rb[0][phase] or not rh[0][phase]:
                continue
            cols = []
            for get, fmt, with_delta in (
                (lambda r: r[phase]["sink_nlri"], "{:.0f}", False),
                (lambda r: r[phase]["cpu"], "{:.2f}", True),
                (lambda r: r[phase]["seconds"], "{:.2f}", False),
                (lambda r: r[phase]["total_alloc"] / MIB, "{:.0f}", True),
                (lambda r: r[phase]["rss_peak"] / MIB, "{:.0f}", True),
            ):
                b, h = [get(r) for r in rb], [get(r) for r in rh]
                cols += [cell(b, fmt), cell(h, fmt)] + ([delta(b, h)] if with_delta else [])
            lines.append(f"| {' | '.join(map(str, key))} | {phase} | {' | '.join(cols)} |")
    lines.append("")
    if all(r["idle"] for rs in (*rb_all.values(), *rh_all.values()) for r in rs):
        lines += [
            "Once the load is done: the heap in use after a forced GC, and the RSS after the idle time.",
            "",
            f"| family | communities | receivers | resend | live heap, MiB: {base} | {head} | Δ | settled RSS, MiB: {base} | {head} |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for key in rb_all:
            rb, rh = rb_all[key], rh_all.get(key)
            if not rh:
                continue
            heap = lambda rs: [r["idle"]["after_gc"]["heap_inuse"] / MIB for r in rs]
            rss = lambda rs: [r["idle"]["settled"]["rss"] / MIB for r in rs]
            lines.append(f"| {' | '.join(map(str, key))} | {cell(heap(rb), '{:.0f}')} | {cell(heap(rh), '{:.0f}')} | "
                         f"{delta(heap(rb), heap(rh))} | {cell(rss(rb), '{:.0f}')} | {cell(rss(rh), '{:.0f}')} |")
        lines.append("")
    return lines


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("base", type=Path, help="results directory of the baseline")
    p.add_argument("head", type=Path, help="results directory of the binary to compare with it")
    p.add_argument("--out", type=Path, help="default: <base>..<head>.md next to the directories")
    args = p.parse_args()
    dirs = (args.base.resolve(), args.head.resolve())
    pb, ph = parts(dirs[0]), parts(dirs[1])
    common = [part for part in pb if part in ph]
    if not common:
        raise SystemExit(f"no results under the same path in {dirs[0]} and {dirs[1]}")
    out = header(dirs, common[0])
    for part in common:
        out += table(part, pb[part], ph[part], dirs[0].name, dirs[1].name)
    path = args.out or dirs[1].parent / f"{dirs[0].name}..{dirs[1].name}.md"
    path.write_text("\n".join(out))
    print(f"report: {path}")


if __name__ == "__main__":
    main()
