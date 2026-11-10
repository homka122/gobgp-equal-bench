# gobgpd bench

Runs a gobgpd binary between two test peers and measures what it spends on the routes it
receives, stores and sends on.

```
sender ──BGP──▶ gobgpd under test ──BGP──▶ receivers
```

## Run it

You need Docker with 4 CPUs and a gobgpd binary built for Linux. In the gobgp tree:

```bash
GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -o gobgpd ./cmd/gobgpd
```

Then give the binary and a scenario to the bench:

```bash
./bench run path/to/gobgpd scenarios/example.toml --name mine
```

The results go to `results/mine/example/`. To compare two binaries, run both with the same
scenario under two names and then:

```bash
./bench compare base head        # writes results/base..head.md
```

Notes:

- With Docker on macOS the binary has to be under a path the Docker VM shares, such as your home.
- `--local` runs without Docker; that needs Linux, Go and [uv](https://docs.astral.sh/uv/).
- One `run` takes several scenario files; each gets its own directory in the results.

## A scenario

A scenario says what the sender announces and how. It is a small file:

```toml
family      = "ipv4"
routes      = 100000     # routes in the table
receivers   = 1          # peers gobgpd sends the routes on to
repetitions = 3

[communities]            # community values every route carries
standard = 10
extended = 10
large    = 10
order    = "random"      # sorted | random | reversed

[storm]                  # after the load the routes are announced again
rounds = 1
resend = "changed"       # same | changed | standard-reversed | ...
```

[scenarios/example.toml](scenarios/example.toml) lists every field with its meaning and default.
A field with a list of values runs every combination, so one file can cover a whole comparison.

## What a run does

Every run starts a fresh gobgpd (AS 200) with its peers on the same machine:

- the **sender** (`blaster`, AS 100) builds every UPDATE itself, so it controls what a repeated
  announcement looks like byte by byte;
- a **receiver** (`sink`, AS 300) counts the routes gobgpd sends it and drops them.

A run has two phases:

1. **load**: the sender announces the table for the first time;
2. **storm**: it announces the same routes again, `storm.rounds` times, as `storm.resend` says.

A phase ends when a marker route sent after it reaches every receiver: gobgpd handles the UPDATEs
of a peer in order, so by then it has done all the work of the phase. The run fails unless the
receivers got exactly what the scenario says.

## What is measured

For each phase, for the gobgpd process:

| In the report | What it is |
|---|---|
| CPU, s | CPU time gobgpd used, user + system, read from outside the process |
| wall, s | time from the first UPDATE until the marker reached the receivers |
| allocated, MiB | memory gobgpd allocated in the phase, freed or not (Go `TotalAlloc`) |
| peak RSS, MiB | the most memory the process held, sampled every 0.1 s |
| sent on | routes that reached the receivers |
| live heap, MiB | with `idle_after_load`: the heap in use after the load and a forced GC, what the table itself takes |

Nothing polls gobgpd while a phase runs. On Linux gobgpd is pinned to CPUs 0–1 with
`GOMAXPROCS=2`, and the sender and the receivers share CPUs 2–3; the `[runtime]` section of a
scenario changes that, for a machine with more CPUs. Every run is one JSON file in `results/<name>/<scenario>/raw`
with more than the report shows (GC cycles, the RSS over time, the CPU of the peers); `env.json`
next to it records the machine, the binary and the scenario.

The report shows medians over the repetitions with min–max. Two binaries are measured one after
the other, so a difference inside those ranges is noise; on a laptop that is a few percent of
CPU, and more repetitions narrow it.

## Limits

One sender, eBGP, no policies, loopback: the direction and the size of an effect, not the
throughput of a production router.

## Layout

| | |
|---|---|
| `bench` | the command |
| `scenarios/` | the example scenario |
| `live/` | the sender, the receiver and the minimal BGP speaker they share; the config of gobgpd |
| `harness/` | `run.py` runs the scenarios, `report.py` compares two results |
| `studies/` | investigations made with the bench, one directory each: scenarios, results, conclusions |
| `work/`, `results/` | scratch, not in git |
