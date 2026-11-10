# Path.Equal fix on gobgp v3 (November 2026)

The fix measured on upstream master in [2026-10-upstream-pr](../2026-10-upstream-pr), ported to
gobgp v3 and measured on a live gobgpd. Two questions:

1. When a peer announces a route gobgpd already has, is it sent on again, and what does the
   announcement cost? Scenario [repeat](repeat.toml).
2. The fix sorts the attributes and community values of every received route. What does that
   cost on the first load of a big table? Scenario [sets](sets.toml).

Everything needed to check the numbers is in this directory: the scenarios, the
[report](results/before..after.md) with min–max for every cell and the raw data of every run in
[results](results).

## 1. Re-announcements (repeat)

A sender announces 10 000 routes to gobgpd, each in its own UPDATE with 3 standard, 3 extended
and 3 large communities; gobgpd passes them to one receiver. Then the sender announces the same
10 000 routes 10 more times (the *storm*), in one of seven ways:

| Re-announced | What the sender does |
|---|---|
| the same bytes | repeats the UPDATEs as they were |
| with a changed MED | changes MED in every round, so every route really changes |
| standard / extended / large communities reversed | every other round the values of that attribute come in reverse order |
| attributes in another order | every other round the path attributes come in another order |
| changed MED and everything reordered | a new MED in every round together with all four reorderings |

In the four reorderings the route is the same, only its encoding differs. IPv4 unicast and
VPNv4, 3 repetitions of each, a fresh gobgpd for every run.

The storm phase, 100 000 re-announcements, medians of 3 repetitions:

| family | re-announced | sent on: before | after | CPU, s: before | after | Δ | allocated, MiB: before | after | Δ | peak RSS, MiB: before | after | Δ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ipv4 | the same bytes | 0 | 0 | 1.57 | 1.03 | −34% | 693 | 358 | −48% | 116 | 54 | −54% |
| ipv4 | with a changed MED | 100 000 | 100 000 | 2.93 | 2.42 | −17% | 1069 | 732 | −32% | 288 | 271 | −5.9% |
| ipv4 | standard communities reversed | 100 000 | 0 | 2.99 | 1.05 | −65% | 1069 | 358 | −67% | 294 | 54 | −82% |
| ipv4 | extended communities reversed | 100 000 | 0 | 2.94 | 0.98 | −67% | 1069 | 358 | −67% | 289 | 55 | −81% |
| ipv4 | large communities reversed | 100 000 | 0 | 2.91 | 0.98 | −66% | 1069 | 358 | −67% | 296 | 54 | −82% |
| ipv4 | attributes in another order | 100 000 | 0 | 2.91 | 1.04 | −64% | 1032 | 358 | −65% | 289 | 54 | −81% |
| ipv4 | changed MED and everything reordered | 100 000 | 100 000 | 2.95 | 2.43 | −18% | 1032 | 732 | −29% | 285 | 280 | −1.6% |
| vpnv4 | the same bytes | 0 | 0 | 1.76 | 1.12 | −36% | 813 | 429 | −47% | 116 | 57 | −51% |
| vpnv4 | with a changed MED | 100 000 | 100 000 | 3.12 | 2.41 | −23% | 1106 | 720 | −35% | 340 | 305 | −11% |
| vpnv4 | standard communities reversed | 100 000 | 0 | 3.10 | 1.22 | −61% | 1106 | 429 | −61% | 337 | 57 | −83% |
| vpnv4 | extended communities reversed | 100 000 | 0 | 3.07 | 1.18 | −62% | 1106 | 429 | −61% | 339 | 56 | −83% |
| vpnv4 | large communities reversed | 100 000 | 0 | 3.08 | 1.14 | −63% | 1106 | 429 | −61% | 340 | 57 | −83% |
| vpnv4 | attributes in another order | 100 000 | 0 | 3.13 | 1.19 | −62% | 1125 | 429 | −62% | 329 | 56 | −83% |
| vpnv4 | changed MED and everything reordered | 100 000 | 100 000 | 3.12 | 2.43 | −22% | 1125 | 720 | −36% | 330 | 294 | −11% |

- **A re-encoded route** counted as a new one before: all 100 000 re-announcements went on to the
  receiver. After the fix none does; the storm takes 61–67% less CPU and 61–67% less allocated
  memory, and peak RSS stays at the level of the loaded table (54–57 MiB instead of 289–340).
- **The same bytes** were not sent on before either, but handling them is cheaper now:
  −34…36% CPU, −47…48% allocated.
- **A real change** is sent on by both, as it must be, also when it arrives re-encoded; it costs
  17–23% less CPU and 29–36% less allocated memory.
- **The first load** of these 10 000 routes does not change: 0.25–0.29 s of CPU for both binaries,
  the differences are inside the min–max ranges.

## 2. The cost of sorting on the load (sets)

A sender announces 100 000 IPv4 routes once (the *load*), then the same routes once more with
another MED (the *change*). Nothing repeats, so the load shows only what the sorting costs. Six
community sets per route, written as standard + extended + large values and their order in the
UPDATE; 3 repetitions of each.

| communities per route | load CPU, s: before | after | Δ | load allocated, MiB: before | after | Δ | change CPU, s: before | after | Δ | change allocated, MiB: before | after | Δ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| none | 2.37 | 2.37 | +0.0% | 673 | 671 | −0.3% | 2.53 | 2.21 | −13% | 580 | 477 | −18% |
| 3 + 3 + 3, random | 2.82 | 2.81 | −0.4% | 952 | 936 | −1.7% | 3.52 | 2.84 | −19% | 1083 | 746 | −31% |
| 10 + 10 + 10, sorted | 3.32 | 3.36 | +1.2% | 1399 | 1419 | +1.5% | 4.68 | 3.64 | −22% | 1893 | 1233 | −35% |
| 10 + 10 + 10, random | 3.37 | 3.47 | +3.0% | 1399 | 1419 | +1.5% | 4.68 | 3.73 | −20% | 1893 | 1233 | −35% |
| 10 + 10 + 10, reversed | 3.30 | 3.44 | +4.2% | 1399 | 1419 | +1.5% | 4.72 | 3.81 | −19% | 1893 | 1233 | −35% |
| 0 + 50 + 0, random | 4.71 | 5.06 | +7.4% | 2060 | 2056 | −0.2% | 8.62 | 7.06 | −18% | 2884 | 1903 | −34% |

- **The load** costs the same with up to 9 values per route. With 30 unsorted values it is 3–4%
  more CPU, which is at the edge of the spread: a second run with the binaries measured in the
  opposite order ([report](results/before2..after2.md)) gave +1…3% for these rows. With 50 values
  it is +7% here and +8% in the second run. The memory of the load does not change (−1.7…+1.5%
  allocated, the same peak RSS).
- **The change** is cheaper with every set: 13–22% less CPU, 18–35% less allocated memory.

## Code under test

Two gobgpd 3.24.0 binaries built from a gobgp v3 tree that is not public, with the same Go
(1.27.1) and the same flags:

| | |
|---|---|
| `before` | the tree as it is |
| `after` | the same tree plus one commit: received attributes and community values are put in one order before hashing, and `Path.Equal` compares attributes in place instead of copying them |

The binaries are not published; `env.json` of each run records their sha256.

## Run it

With two gobgpd binaries of your own, from the root of the repository:

```bash
./bench run path/to/gobgpd-before studies/2026-11-gobgp-v3/*.toml --name before
./bench run path/to/gobgpd-after  studies/2026-11-gobgp-v3/*.toml --name after
./bench compare before after      # results/before..after.md
```

About 3 minutes per binary. The results here were made by the stand as it is in the commit that
added them: `before` and `after` one right after the other, then `after2` and `before2`,
the sets scenario alone in the opposite order.

## Measurement and limits

Docker VM with 4 CPUs on an Apple M2. gobgpd is pinned to CPUs 0–1 of the VM with
`GOMAXPROCS=2`, the sender and the receiver to CPUs 2–3. CPU and RSS of gobgpd are read from
outside the process, allocated memory is Go `TotalAlloc` read once before and once after a
phase. What a run does and what each column means: the [README of the stand](../../README.md).

One sender, one receiver, eBGP, no policies, loopback, a laptop: the direction and the size of
the effect, not the throughput of a production router.
