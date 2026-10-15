"""Runs the micro benchmarks from micro/bench_test.go on every variant.

The benchmark file is copied into internal/pkg/table of each variant and
compiled into a test binary. Variants are interleaved within every count so
that drift of the machine does not end up in the difference.
Output: <out>/micro/<variant>.txt in the go test format (benchstat reads it).
"""

import argparse
import random
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "work"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--variants", default="base,order,alloc1,alloc,both")
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--benchtime", default="1s")
    p.add_argument("--bench", default="BenchmarkStand")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--out", type=Path, default=ROOT / "results")
    args = p.parse_args()

    out = args.out / "micro"
    out.mkdir(parents=True, exist_ok=True)
    variants = args.variants.split(",")
    for v in variants:
        pkg = WORK / "variants" / v / "internal" / "pkg" / "table"
        shutil.copy(ROOT / "micro" / "bench_test.go", pkg / "zz_stand_bench_test.go")
        subprocess.run(["go", "test", "-c", "-o", str(WORK / "bin" / v / "table.test"), "."], cwd=pkg, check=True)
        (out / f"{v}.txt").write_text("")
    rng = random.Random(args.seed)
    for i in range(args.count):
        for v in rng.sample(variants, len(variants)):
            print(f"count {i + 1}/{args.count}: {v}", flush=True)
            res = subprocess.run(
                [str(WORK / "bin" / v / "table.test"), "-test.run", "^$", "-test.bench", args.bench,
                 "-test.benchmem", "-test.benchtime", args.benchtime],
                cwd=WORK / "variants" / v / "internal" / "pkg" / "table",
                check=True, capture_output=True, text=True)
            lines = res.stdout.splitlines()
            if i > 0:  # keep the goos/goarch/pkg/cpu header once
                lines = [line for line in lines if line.startswith("Benchmark")]
            else:
                lines = [line for line in lines if line not in ("PASS",)]
            with open(out / f"{v}.txt", "a") as f:
                f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
