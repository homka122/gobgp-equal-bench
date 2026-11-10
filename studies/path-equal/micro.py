"""Runs the go test benchmarks of micro/ in a gobgp source tree.

The benchmark file is copied into internal/pkg/table of the tree for the
time of the run and compiled into a test binary: micro/bench_v3_test.go for
gobgp v3, micro/bench_v4_test.go for v4, by the module path in go.mod. The
benchmarks call internal functions, so they do not compile in a tree where
those have changed.
Output: a file in the go test format; benchstat compares two of them.
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--src", type=Path, required=True, help="gobgp source tree, checked out at the commit to measure")
    p.add_argument("--bench", type=Path, help="benchmark file; default: by the gobgp major version")
    p.add_argument("--pattern", default="^BenchmarkStand", help="go test -bench pattern")
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--benchtime", default="300ms")
    p.add_argument("--out", type=Path, required=True, help="file to write, go test format")
    args = p.parse_args()

    src = args.src.expanduser().resolve()
    pkg = src / "internal" / "pkg" / "table"
    major = (src / "go.mod").read_text().split(maxsplit=2)[1].rsplit("/", 1)[-1]
    bench = args.bench or HERE / "micro" / f"bench_{major}_test.go"
    if not bench.exists():
        sys.exit(f"no {bench.name}: micro/ has no benchmarks for gobgp {major}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    copy = pkg / "zz_stand_bench_test.go"
    with tempfile.TemporaryDirectory() as tmp:
        test = Path(tmp) / "table.test"
        copy.write_bytes(bench.read_bytes())
        try:
            res = subprocess.run(["go", "test", "-c", "-o", str(test), "."], cwd=pkg, capture_output=True, text=True)
        finally:
            copy.unlink()
        if res.returncode:
            sys.exit(f"{bench.name} does not compile in {src}:\n{res.stdout}{res.stderr}")
        lines = []
        for i in range(args.count):
            print(f"go test benchmarks, count {i + 1}/{args.count}", flush=True)
            res = subprocess.run(
                [str(test), "-test.run", "^$", "-test.bench", args.pattern, "-test.benchmem",
                 "-test.benchtime", args.benchtime], cwd=pkg, check=True, capture_output=True, text=True)
            lines += [line for line in res.stdout.splitlines()
                      if line.startswith("Benchmark") or (i == 0 and line != "PASS" and not line.startswith("ok"))]
    args.out.write_text("\n".join(lines) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
