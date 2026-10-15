"""Runs gobgpd variants between blaster and sink and records CPU and memory.

One run: start gobgpd and sink, let blaster load the table, wait until
gobgpd is idle, let blaster re-announce the table `rounds` times, wait again.
Nothing polls gobgpd while it works: a /metrics scrape allocates ~160 KiB
inside gobgpd and would show up in the numbers. Go memory stats are read once
before and once after a phase, GC cycles come from GODEBUG=gctrace=1 and RSS
and CPU are read from outside by psutil. Every run is written to
<out>/raw/<name>.json.
"""

import argparse
import itertools
import json
import os
import platform
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "work" / "bin"
DUT_MEMSTATS = "http://127.0.0.1:10160/debug/pprof/heap?debug=1"
SINK_STATS = "http://127.0.0.1:10180/stats"
AFI_SAFI = {"ipv4": "ipv4-unicast", "vpnv4": "l3vpn-ipv4-unicast"}
MEMSTATS = {"TotalAlloc": "total_alloc", "Mallocs": "mallocs", "NumGC": "num_gc", "HeapInuse": "heap_inuse"}
GCTRACE = re.compile(r"^gc (\d+) @([\d.]+)s .* (\d+)->(\d+)->(\d+) MB")


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode()


def memstats():
    """runtime.MemStats of gobgpd from the pprof heap page."""
    m = {}
    for line in get(DUT_MEMSTATS).splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0] == "#" and parts[1] in MEMSTATS:
            m[MEMSTATS[parts[1]]] = float(parts[3])
    return m


def sink():
    return json.loads(get(SINK_STATS))


def wait_for(cond, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if cond():
                return
        except OSError:
            pass
        time.sleep(0.1)
    raise TimeoutError(cond.__name__ if hasattr(cond, "__name__") else "condition")


class Run:
    def __init__(self, variant, family, mode, args, workdir):
        self.variant, self.family, self.mode, self.args = variant, family, mode, args
        conf = workdir / "dut.toml"
        conf.write_text((ROOT / "live" / "dut.toml.tmpl").read_text().replace("{family}", AFI_SAFI[family]))
        sink_addr = args.sink_addr
        conf.write_text(conf.read_text().replace("{sink_addr}", sink_addr))
        cmd = [str(BIN / variant / "gobgpd"), "-f", str(conf), "--api-hosts", "127.0.0.1:10150",
               "--pprof-host", "127.0.0.1:10160", "--metrics-path", "", "--log-level", "warn"]
        if args.dut_cpus and sys.platform == "linux":
            cmd = ["taskset", "-c", args.dut_cpus] + cmd
        env = dict(os.environ, GOMAXPROCS=str(args.gomaxprocs), GODEBUG="gctrace=1")
        self.log = open(workdir / "dut.log", "w")
        self.dut = subprocess.Popen(cmd, env=env, stdout=self.log, stderr=subprocess.PIPE, text=True)
        self.stderr = []  # [arrival time, line]; gctrace lines come as the GC ends
        threading.Thread(target=self.read_stderr, daemon=True).start()
        self.proc = psutil.Process(self.dut.pid)  # taskset execs gobgpd in place
        wait_for(memstats)
        peers = self.peer_cpus()
        host = f"[{sink_addr}]" if ":" in sink_addr else sink_addr
        self.sink = subprocess.Popen(peers + [str(BIN / "sink"), "-family", family, "-local", sink_addr,
                                              "-remote", f"{host}:10179"], stderr=self.log)
        self.blaster = subprocess.Popen(
            peers + [str(BIN / "blaster"), "-family", family, "-mode", mode, "-n", str(args.n),
                     "-per-update", str(args.per_update)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True)
        assert self.reply()["ready"]
        wait_for(lambda: sink()["established"])

    def peer_cpus(self):
        if self.args.peer_cpus and sys.platform == "linux":
            return ["taskset", "-c", self.args.peer_cpus]
        return []

    def reply(self):
        line = self.blaster.stdout.readline()
        if not line:
            raise RuntimeError(f"blaster exited, see dut.log")
        return json.loads(line)

    def cpu(self):
        t = self.proc.cpu_times()
        return t.user + t.system

    def snapshot(self):
        s = memstats()
        s["cpu"] = self.cpu()
        s["rss"] = self.proc.memory_info().rss
        s["time"] = time.time()
        s.update({"sink_" + k: v for k, v in sink().items() if k not in ("last_update_unix_nano", "established")})
        return s

    def wait_idle(self, quiet=2.0, step=0.25, busy=0.04):
        """Waits until the sink counters stop and gobgpd uses less than `busy`
        of a CPU. Returns the time and the CPU of the last busy moment, so that
        the idle wait itself is not counted."""
        last, cpu, since = None, self.cpu(), time.time()
        busy_at, busy_cpu = since, cpu
        while time.time() - since < quiet:
            time.sleep(step)
            st = sink()
            cur, c = (st["nlri"], st["withdrawn"]), self.cpu()
            if cur != last or c - cpu > busy * step:
                since = busy_at = time.time()
                busy_cpu = c
            last, cpu = cur, c
        return busy_at, busy_cpu

    def read_stderr(self):
        for line in self.dut.stderr:
            self.stderr.append((time.time(), line.rstrip("\n")))

    def gc_cycles(self, start, first, last):
        """GC cycles number first..last from gctrace:
        [seconds since start, heap MB before, heap MB after]."""
        out = []
        for t, line in list(self.stderr):
            m = GCTRACE.match(line)
            if m and first <= int(m.group(1)) <= last:
                out.append([round(t - start, 3), int(m.group(3)), int(m.group(5))])
        return out

    def phase(self, command):
        samples, stop = [], threading.Event()

        def sample():
            while not stop.is_set():
                try:
                    samples.append([round(time.time() - start, 3), self.proc.memory_info().rss])
                except psutil.Error:
                    pass
                stop.wait(0.1)

        before = self.snapshot()
        start = time.time()
        th = threading.Thread(target=sample)
        th.start()
        self.blaster.stdin.write(command + "\n")
        self.blaster.stdin.flush()
        sent = self.reply()
        end, end_cpu = self.wait_idle()
        stop.set()
        th.join()
        after = self.snapshot()
        delta = {k: after[k] - before[k] for k in before if k not in ("rss", "heap_inuse", "time")}
        delta["cpu"] = end_cpu - before["cpu"]
        delta["seconds"] = end - start
        delta["rss_end"] = after["rss"]
        delta["heap_inuse_end"] = after["heap_inuse"]
        delta["rss_peak"] = max([s[1] for s in samples] + [after["rss"]])
        delta["sent_updates"] = sent["updates"]
        delta["rss_series"] = [s for s in samples if s[0] <= delta["seconds"]]
        delta["gc_series"] = self.gc_cycles(start, before["num_gc"] + 1, after["num_gc"])
        return delta

    def close(self):
        for p in (self.blaster, self.sink, self.dut):
            p.kill()
            p.wait()
        self.log.close()


def run_one(variant, family, mode, args):
    with tempfile.TemporaryDirectory() as d:
        r = Run(variant, family, mode, args, Path(d))
        try:
            load = r.phase("load")
            storm = r.phase(f"storm {args.rounds}")
        except Exception:
            logs = (Path(d) / "dut.log").read_text().splitlines() + [line for _, line in r.stderr]
            print("\n".join(line for line in logs if not line.startswith("gc "))[-2000:], file=sys.stderr)
            raise
        finally:
            r.close()
    check(load, storm, mode, args)
    return {"variant": variant, "family": family, "mode": mode, "n": args.n, "rounds": args.rounds,
            "per_update": args.per_update, "load": load, "storm": storm}


def check(load, storm, mode, args):
    """Fails the run if the traffic was not what the scenario says."""
    per_round = -(-args.n // args.per_update)
    assert load["sent_updates"] == per_round, load["sent_updates"]
    assert storm["sent_updates"] == per_round * args.rounds, storm["sent_updates"]
    assert load["sink_nlri"] == args.n, f"sink got {load['sink_nlri']} routes on load, want {args.n}"
    if mode == "change":
        want = args.n * args.rounds
        assert storm["sink_nlri"] == want, f"sink got {storm['sink_nlri']} routes in the storm, want {want}"
    if mode == "same":
        assert storm["sink_nlri"] == 0, f"sink got {storm['sink_nlri']} routes for unchanged routes"


def write_env(args):
    def out(*cmd, cwd=None):
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True).stdout.strip()

    cpu = platform.machine()
    if Path("/proc/cpuinfo").exists():
        cpu = next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                    if line.startswith("model name")), cpu)
    env = {
        "date": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "platform": platform.platform(),
        "cpu": cpu,
        "cpus": os.cpu_count(),
        "memory_bytes": psutil.virtual_memory().total,
        "go": out("go", "version"),
        "args": {k: str(v) for k, v in vars(args).items()},
        "variants": {v.name: out("git", "log", "--format=%H %s", "-1", cwd=v)
                     for v in sorted((ROOT / "work" / "variants").iterdir())},
    }
    (args.out / "env.json").write_text(json.dumps(env, indent=2) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--variants", default="base,order,alloc,both")
    p.add_argument("--families", default="ipv4,vpnv4")
    p.add_argument("--modes", default="same,change,reorder-comm,reorder-attrs")
    p.add_argument("--n", type=int, default=10000)
    p.add_argument("--rounds", type=int, default=10)
    p.add_argument("--per-update", type=int, default=1)
    p.add_argument("--reps", type=int, default=5)
    p.add_argument("--gomaxprocs", type=int, default=2)
    p.add_argument("--dut-cpus", default="0,1", help="taskset for gobgpd (Linux only)")
    p.add_argument("--peer-cpus", default="2,3", help="taskset for blaster and sink (Linux only)")
    p.add_argument("--sink-addr", default="::1", help="address of the sink; 127.0.0.2 works on Linux")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--out", type=Path, default=ROOT / "results")
    args = p.parse_args()

    raw = args.out / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    write_env(args)
    variants = args.variants.split(",")
    cells = list(itertools.product(args.families.split(","), args.modes.split(",")))
    rng = random.Random(args.seed)
    total = len(cells) * len(variants) * args.reps
    done = 0
    print("warm-up run", flush=True)
    run_one(variants[0], *cells[0], args)
    for rep in range(args.reps):
        for family, mode in cells:
            for variant in rng.sample(variants, len(variants)):
                res = run_one(variant, family, mode, args)
                res["rep"] = rep
                name = f"{variant}-{family}-{mode}-{rep}.json"
                (raw / name).write_text(json.dumps(res))
                done += 1
                s = res["storm"]
                print(f"[{done}/{total}] {name}: storm cpu {s['cpu']:.2f}s, "
                      f"alloc {s['total_alloc'] / 2**20:.0f} MiB, gc {s['num_gc']:.0f}, "
                      f"forwarded {s['sink_nlri']:.0f}", flush=True)


if __name__ == "__main__":
    main()
