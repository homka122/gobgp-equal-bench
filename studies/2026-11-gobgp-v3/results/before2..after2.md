# gobgpd: after2 against before2

| | gobgpd | revision | sha256 |
|---|---|---|---|
| before2 | gobgpd version 3.24.0 | not in the binary | `a04bc8d43122` |
| after2 | gobgpd version 3.24.0 | not in the binary | `a7e9642e4e73` |

2026-11-10 09:25:53 +0000, Linux-6.8.0-117-generic-aarch64-with-glibc2.36, 4 CPUs. gobgpd runs on CPUs 0,1 with GOMAXPROCS=2, the sender on CPUs 2,3, the receivers on 2,3 (pinned on Linux only).

A cell is the median over the repetitions, min–max in brackets. Δ is the difference of the medians in % of before2. The two binaries were measured one after the other: a Δ within the min–max ranges is noise. Communities are the numbers of standard + extended + large values per route and their order. Phase load: the table is announced for the first time; storm: it is announced again, as “resend” says. Sent on: routes that reached the receivers.

## sets

100000 routes, 1 per UPDATE, 1 storm rounds, 3 and 3 repetitions.

| family | communities | receivers | resend | phase | sent on: before2 | after2 | CPU, s: before2 | after2 | Δ | wall, s: before2 | after2 | allocated, MiB: before2 | after2 | Δ | peak RSS, MiB: before2 | after2 | Δ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ipv4 | none | 1 | changed | load | 100000 | 100000 | 2.37 (2.37–2.43) | 2.41 (2.31–2.44) | +1.7% | 1.59 (1.57–1.63) | 1.61 (1.57–1.61) | 673 | 671 | -0.3% | 226 (226–227) | 230 (228–231) | +1.7% |
| ipv4 | none | 1 | changed | storm | 100000 | 100000 | 2.64 (2.50–3.29) | 2.32 (2.20–2.33) | -12% | 1.71 (1.64–2.08) | 1.50 (1.50–1.52) | 580 | 477 | -18% | 349 (343–360) | 346 (330–357) | -1.0% |
| ipv4 | 3 + 3 + 3, random | 1 | changed | load | 100000 | 100000 | 2.93 (2.84–3.10) | 2.83 (2.80–2.84) | -3.4% | 1.88 (1.83–2.01) | 1.84 (1.82–1.86) | 952 | 936 | -1.7% | 354 (353–359) | 349 (347–354) | -1.3% |
| ipv4 | 3 + 3 + 3, random | 1 | changed | storm | 100000 | 100000 | 3.36 (3.33–3.61) | 2.76 (2.75–2.81) | -18% | 2.16 (2.14–2.29) | 1.83 (1.81–1.83) | 1083 | 746 | -31% | 579 (576–580) | 547 (531–565) | -5.7% |
| ipv4 | 10 + 10 + 10, sorted | 1 | changed | load | 100000 | 100000 | 3.54 (3.43–3.93) | 3.29 (3.28–3.45) | -7.1% | 2.25 (2.18–2.48) | 2.17 (2.13–2.20) | 1399 | 1420 (1419–1420) | +1.5% | 550 (537–557) | 545 (541–547) | -1.0% |
| ipv4 | 10 + 10 + 10, sorted | 1 | changed | storm | 100000 | 100000 | 4.77 (4.60–5.03) | 3.67 (3.60–3.76) | -23% | 2.95 (2.89–3.09) | 2.35 (2.32–2.39) | 1893 | 1233 (1233–1234) | -35% | 954 (913–965) | 839 (838–850) | -12% |
| ipv4 | 10 + 10 + 10, random | 1 | changed | load | 100000 | 100000 | 3.37 (3.36–3.52) | 3.47 (3.39–3.53) | +3.0% | 2.19 (2.16–2.22) | 2.21 (2.19–2.25) | 1399 | 1419 | +1.5% | 553 (541–564) | 534 (510–548) | -3.4% |
| ipv4 | 10 + 10 + 10, random | 1 | changed | storm | 100000 | 100000 | 4.75 (4.69–4.79) | 3.77 (3.72–3.79) | -21% | 2.98 (2.93–2.99) | 2.41 (2.40–2.42) | 1893 | 1233 | -35% | 935 (930–966) | 843 (833–883) | -9.8% |
| ipv4 | 10 + 10 + 10, reversed | 1 | changed | load | 100000 | 100000 | 3.47 (3.24–3.50) | 3.51 (3.49–3.53) | +1.2% | 2.20 (2.10–2.22) | 2.21 (2.21–2.23) | 1399 | 1420 (1419–1420) | +1.5% | 539 (535–573) | 527 (520–542) | -2.1% |
| ipv4 | 10 + 10 + 10, reversed | 1 | changed | storm | 100000 | 100000 | 4.72 (4.53–4.75) | 3.84 (3.83–3.89) | -19% | 2.93 (2.92–2.94) | 2.44 (2.42–2.49) | 1893 | 1233 | -35% | 930 (915–943) | 805 (797–864) | -13% |
| ipv4 | 0 + 50 + 0, random | 1 | changed | load | 100000 | 100000 | 4.78 (4.55–4.82) | 5.16 (5.03–5.21) | +7.9% | 3.06 (2.95–3.07) | 3.27 (3.22–3.27) | 2060 | 2059 | -0.0% | 936 (895–969) | 959 (899–961) | +2.4% |
| ipv4 | 0 + 50 + 0, random | 1 | changed | storm | 100000 | 100000 | 7.86 (7.72–7.87) | 6.73 (6.72–7.19) | -14% | 5.18 (5.10–5.22) | 4.54 (4.54–4.72) | 2884 | 1902 (1902–1903) | -34% | 1619 (1493–1648) | 1451 (1380–1479) | -10% |
