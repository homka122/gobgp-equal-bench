# Path.Equal fix on gobgp v3 (November 2026)

The fix measured on upstream master in [2026-10-upstream-pr](../2026-10-upstream-pr), ported to
gobgp v3 and measured on a live gobgpd. The question is the same: when a peer announces a route
gobgpd already has, is it sent on again, and what does the announcement cost?

Everything needed to check the numbers is in this directory: the [scenario](repeat.toml), the
[report](results/before..after.md) and the raw data of every run in [results](results).

## Scenario

A sender announces 10 000 routes to gobgpd, each in its own UPDATE with 3 standard, 3 extended
and 3 large communities; gobgpd passes them to one receiver. Then the sender announces the same
10 000 routes 10 more times (the *storm*), in one of six ways:

| Re-announced | What the sender does |
|---|---|
| the same bytes | repeats the UPDATEs as they were |
| with a changed MED | changes MED in every round, so every route really changes |
| standard / extended / large communities reversed | every other round the values of that attribute come in reverse order |
| attributes in another order | every other round the path attributes come in another order |

In the last four the route is the same, only its encoding differs. IPv4 unicast and VPNv4,
3 repetitions of each, a fresh gobgpd for every run.

## Result

The storm phase: 100 000 re-announcements. Medians of 3 repetitions; min–max are in the
[report](results/before..after.md).

| family | re-announced | sent on: before | after | CPU, s: before | after | Δ | allocated, MiB: before | after | Δ | peak RSS, MiB: before | after | Δ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ipv4 | the same bytes | 0 | 0 | 1.52 | 1.07 | −30% | 692 | 358 | −48% | 105 | 54 | −48% |
| ipv4 | with a changed MED | 100 000 | 100 000 | 3.00 | 2.43 | −19% | 1069 | 732 | −32% | 302 | 272 | −9.9% |
| ipv4 | standard communities reversed | 100 000 | 0 | 2.94 | 1.12 | −62% | 1069 | 358 | −67% | 305 | 55 | −82% |
| ipv4 | extended communities reversed | 100 000 | 0 | 2.99 | 1.14 | −62% | 1069 | 358 | −67% | 300 | 53 | −82% |
| ipv4 | large communities reversed | 100 000 | 0 | 3.01 | 1.08 | −64% | 1069 | 358 | −67% | 296 | 53 | −82% |
| ipv4 | attributes in another order | 100 000 | 0 | 3.07 | 1.09 | −64% | 1032 | 358 | −65% | 298 | 53 | −82% |
| vpnv4 | the same bytes | 0 | 0 | 1.91 | 1.27 | −34% | 814 | 429 | −47% | 120 | 57 | −53% |
| vpnv4 | with a changed MED | 100 000 | 100 000 | 3.17 | 2.47 | −22% | 1107 | 720 | −35% | 347 | 293 | −16% |
| vpnv4 | standard communities reversed | 100 000 | 0 | 3.13 | 1.26 | −60% | 1106 | 429 | −61% | 336 | 58 | −83% |
| vpnv4 | extended communities reversed | 100 000 | 0 | 3.08 | 1.22 | −60% | 1106 | 429 | −61% | 336 | 56 | −83% |
| vpnv4 | large communities reversed | 100 000 | 0 | 3.09 | 1.24 | −60% | 1106 | 429 | −61% | 333 | 57 | −83% |
| vpnv4 | attributes in another order | 100 000 | 0 | 3.18 | 1.33 | −58% | 1125 | 429 | −62% | 332 | 56 | −83% |

- **A re-encoded route** counted as a new one before: all 100 000 re-announcements went on to the
  receiver. After the fix none does; the storm takes 58–64% less CPU and 61–67% less allocated
  memory, and peak RSS stays at the level of the loaded table (53–58 MiB instead of 296–336).
- **The same bytes** were not sent on before either, but handling them is cheaper now:
  −30…34% CPU, −47…48% allocated.
- **A real change** is sent on by both, as it must be; it costs 19–22% less CPU and 32–35% less
  allocated memory.
- **The first load** does not change: 0.26–0.29 s of CPU for both binaries, the differences are
  inside the min–max ranges; 93–94 MiB allocated against 93–96; the same peak RSS.

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
./bench run path/to/gobgpd-before studies/2026-11-gobgp-v3/repeat.toml --name before
./bench run path/to/gobgpd-after  studies/2026-11-gobgp-v3/repeat.toml --name after
./bench compare before after      # results/before..after.md
```

About 1.5 minutes per binary. The results here were made by the stand as it is in the commit
that added this directory, the two binaries one right after the other.

## Measurement and limits

Docker VM with 4 CPUs on an Apple M2. gobgpd is pinned to CPUs 0–1 with
`GOMAXPROCS=2`, the sender and the receiver to CPUs 2–3. CPU and RSS of gobgpd are read from
outside the process, allocated memory is Go `TotalAlloc` read once before and once after a
phase. What a run does and what each column means: the [README of the stand](../../README.md).

One sender, one receiver, 10 000 routes, eBGP, no policies, loopback: the direction and the size
of the effect, not the throughput of a production router.
