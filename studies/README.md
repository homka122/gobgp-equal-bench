# Studies

One directory per investigation: `README.md` with the question, the commits and the conclusion,
`results/` with the raw data, and the scripts only that investigation needs. The results are
frozen; each README names the commit of the stand that made them.

[path-equal](path-equal) is not a study but the set of scenarios such studies run: start
there for a new measurement of `Path.Equal`.

| Study | Question | Answer |
|---|---|---|
| [2026-10-upstream-pr](2026-10-upstream-pr) | What do the `Path.Equal` fixes give on upstream master? | Reordered re-announcements sent on: 100 000 → 0; CPU of such a storm −42…44%; `Path.Equal` 181 → 9 ns, 10 → 0 allocations |
