# gobgp Path.Equal bench

A reproducible bench for two independent changes to [GoBGP](https://github.com/osrg/gobgp)
that affect how a re-announced route is compared with the one already in the table.

| Fix | Commits | What it does |
|---|---|---|
| **order** | [`ae767535`](https://github.com/homka122/gobgp/commit/ae767535baa74c83427d5b754805d55bf6541a32) | Sorts the path attributes by type code and the COMMUNITIES values when an UPDATE is received. Before, the same route with its attributes or communities in another order counted as a new route: it got a new age and was sent again to all peers. |
| **alloc** | [`81912c5c`](https://github.com/homka122/gobgp/commit/81912c5c99fc5585cf684bd7324a49c417da57e3), [`3ae8df9c`](https://github.com/homka122/gobgp/commit/3ae8df9c9788f8b8b630c6f87cfca6eee7687a28) | `Path.Equal` without allocations: it reads the attributes without copying them (step 1) and compares IPv4/IPv6 prefixes without serializing them (step 2). |

Both are on the [`path-equal`](https://github.com/homka122/gobgp/tree/path-equal) branch, on top of
osrg/gobgp master `2966efd1`. They touch different files, so the bench builds each one on its own:

| Variant | Code |
|---|---|
| `base` | `2966efd1`, upstream master |
| `order` | base + order fix |
| `alloc1` | base + alloc fix step 1 (micro benchmarks only) |
| `alloc` | base + alloc fix |
| `both` | base + both fixes |

## Results

Full run in the Docker container: Linux arm64 VM with 4 CPUs on an Apple M2, Go 1.27.2.
10 000 prefixes re-announced 10 times (100 000 re-announcements), 5 repetitions per cell.
Raw data, environment and all numbers: [results/full](results/full)
([summary.md](results/full/summary.md), [env.json](results/full/env.json), [raw/](results/full/raw)).

### Order fix

**When a peer changes the order, base sends every route again; with the fix nothing goes out.**
This holds for both IPv4 and VPNv4, and whether the communities or the attributes are reordered.

![forwarded routes](results/full/plots/1-forwarded.png)

**gobgpd spends 32–39% less CPU on such a storm.** The route is not re-advertised, so there is
nothing to build and send downstream.

![CPU in a reorder storm](results/full/plots/2-cpu-reorder.png)

**When the order is stable, the fix costs nothing measurable.**
Each route in this storm really changes and is sent downstream.

![CPU when nothing is reordered](results/full/plots/3-cpu-change.png)

### Alloc fix

**`Path.Equal` no longer allocates for IPv4/IPv6 routes: 10 → 0 allocations, 292 → 71 ns.**
VPNv4 gets step 1 only, 12 → 8 allocations and 321 → 153 ns: its NLRI still goes through
`Serialize`, on purpose, because it carries the label outside the route key.

![Equal micro benchmark](results/full/plots/4-equal-micro.png)

**In a live storm of unchanged routes gobgpd allocates 12% less per route on IPv4 and 9% less on
VPNv4.** It also runs fewer GC cycles and spends less CPU (−16% on IPv4).

The share is modest because `Equal` is one step of many per UPDATE: the rest is reading and
parsing the message, the FSM and the tables. The `go test` benchmark of the table chain alone
(`ProcessMessage → AdjRib → TableManager`) shows the same direction:

| IPv4 | base | alloc | |
|---|---|---|---|
| ns/op | 2177 | 1522 | −30% |
| B/op | 1471 | 944 | −36% |
| allocs/op | 59 | 39 | −34% |

![alloc storm](results/full/plots/5-alloc-storm.png)

### Together

**The fixes add up.** For reordered routes the order fix removes most of the work, and the alloc
fix makes the comparison that is left cheaper: 2.31 → 1.09 s CPU on the `reorder-comm` storm.

![all variants](results/full/plots/6-all-variants.png)

### What the fixes do not change

**The memory of the initial load is the same in all variants.** `Path.Equal` is only called when
a route already exists, so loading a new table does not reach it.

![initial load](results/full/plots/7-initial-load.png)

## How it works

```
blaster ──BGP──▶ gobgpd <variant> ──BGP──▶ sink
AS 100            AS 200                  AS 300
```

- **blaster** is a minimal BGP speaker that builds every UPDATE itself, so it controls the order
  of the attributes and of the communities on the wire. It announces N prefixes (one per UPDATE),
  then re-announces all of them R times in one of four modes:
  - `same`: byte-for-byte the same UPDATEs;
  - `change`: one community value changes every round, so every route really changes;
  - `reorder-comm`: every other round the communities come in reverse order;
  - `reorder-attrs`: every other round COMMUNITIES comes before the other attributes.
- **sink** is the downstream peer: it counts the routes gobgpd sends it.
- **gobgpd** is the variant under test. In the container it is pinned to CPUs 0–1 with
  `GOMAXPROCS=2`; blaster and sink run on CPUs 2–3.
- **harness/run.py** runs one variant per run: start, load the table, wait until gobgpd is idle,
  run the storm, wait again. It reads:
  - CPU (user + system) and RSS of gobgpd from outside with psutil;
  - `runtime.MemStats` from the pprof page of gobgpd, once before and once after each phase;
  - every GC cycle from `GODEBUG=gctrace=1`.

  Nothing polls gobgpd while it works: one `/metrics` scrape allocates ~160 KiB inside gobgpd and
  would be counted as its garbage. CPU is counted up to the last moment gobgpd was busy, not
  including the idle wait.

  The run fails if blaster did not send exactly what the mode says, if the sink did not get all N
  routes on load, or if it did not get exactly N×R routes in `change` mode or 0 in `same` mode.
- Variants are interleaved in a shuffled order within every repetition, so drift of the machine
  is not mistaken for a difference between variants. Each chart shows the median over the
  repetitions, with min–max whiskers.
- **harness/micro.py** runs `go test` benchmarks from [micro/bench_test.go](micro/bench_test.go)
  on every variant: `Path.Equal` on two received copies of a route, and the
  `ProcessMessage → AdjRib → TableManager` chain on a storm of re-announcements.

## Run it

Needs Docker. Everything, including the clone and the build of gobgp, happens in the container.

```bash
make docker-quick   # ~5 min: 2k prefixes x 4 rounds, 2 repetitions -> results/quick
make docker-all     # ~40 min: 10k prefixes x 10 rounds, 5 repetitions -> results/full
```

On macOS, keep the machine awake (`caffeinate -i make docker-all`): a sleeping VM stalls a run.

Without Docker (Linux or macOS, needs Go and [uv](https://docs.astral.sh/uv/)):

```bash
scripts/build.sh    # clones gobgp into work/, builds all variants, blaster and sink
make quick          # on macOS only 127.0.0.1 and ::1 work, which are the defaults
```

The commits under test are pinned in [versions.env](versions.env).

## Limitations

- One peer, 10 000 prefixes, one prefix per UPDATE, all on loopback. The numbers show the
  direction and the size of the effect, not the throughput of a production router.
- The routes go into the global table with no policies. A route changed by an import policy
  still takes the old, copying path in `Path.Equal`.
- The order fix only covers routes received over BGP, not routes added through the gRPC API.
