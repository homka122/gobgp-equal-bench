| scenario | re-announcements | gobgpd | peak RSS, MiB | delivered to the receiver | sender done, s | storm done, s |
|---|---|---|---|---|---|---|
| flood | byte-identical | before PR 2869 | 173 (153–209) | 0 | 7.7 (7.6–7.8) | 8.3 (8.1–8.3) |
| flood | byte-identical | PR 2869, buffer channels | 67 (66–68) | 0 | 4.5 (4.4–4.6) | 4.7 (4.6–4.8) |
| flood | byte-identical | master | 59 | 0 | 8.6 (8.3–9.7) | 8.9 (8.6–10.0) |
| flood | community changed | before PR 2869 | 1841 (1803–1873) | 1000000 | 5.5 (5.5–5.8) | 18.3 (18.1–19.4) |
| flood | community changed | PR 2869, buffer channels | 73 (72–74) | 257094 (227714–260057) | 5.3 (5.3–5.9) | 5.7 (5.5–6.2) |
| flood | community changed | master | 60 (59–60) | 1000000 | 15.4 (14.1–16.0) | 15.9 (14.6–16.5) |
| slow-receiver | community changed | before PR 2869 | 1873 (1857–1917) | 1000000 | 5.6 (5.5–5.6) | 50.0 |
| slow-receiver | community changed | PR 2869, buffer channels | 83 (76–85) | 193532 (189973–227206) | 5.0 (4.9–5.0) | 9.6 (9.5–11.3) |
| slow-receiver | community changed | master | 1276 (1247–1326) | 1000000 | 13.2 (13.1–14.7) | 50.0 |

RSS before the storm: 40–50 MiB in all runs.
