# Path.Equal measurements

The scenarios and the `go test` benchmarks used to measure the `Path.Equal` fix and the sorting
of attributes and communities. The dated studies next to this directory are earlier runs of the
same questions on particular commits.

From the root of the repository:

```bash
./bench run path/to/gobgpd-base studies/path-equal/*.toml --name base
./bench run path/to/gobgpd-head studies/path-equal/*.toml --name head
./bench compare base head
```

| Scenario | What happens | Question |
|---|---|---|
| [repeat.toml](repeat.toml) | 10 000 routes announced 10 more times: unchanged, changed, and the same route in four other encodings; IPv4 and VPNv4 | are repeats of the same route sent on, and what do they cost? |
| [sets.toml](sets.toml) | 100 000 routes with 0 to 50 community values per route: the load, then every route changes once | what does sorting cost on the load, and what does the change gain? |
| [startup.toml](startup.toml) | 100 000 routes loaded once, then idle | does the memory of the table change? |

## go test benchmarks

They need the gobgp sources, not a binary:

```bash
python3 studies/path-equal/micro.py --src path/to/gobgp --out results/base/micro.txt
```

This runs [micro/](micro) inside a gobgp source tree checked out at the commit to measure;
`benchstat` compares two such files. `micro.py` copies the benchmark file into
`internal/pkg/table` for the time of the run: it calls internal functions, so there is one file
for gobgp v3 and one for v4, and it does not compile in a tree where those functions have changed.

- `StandEqual`: one `Path.Equal` call on two received copies of a route.
- `StandProcessMessage`: an UPDATE with one prefix turned into a path; the sort happens here.
- `StandStorm`: a repeated announcement through ProcessMessage, AdjRib and TableManager.
