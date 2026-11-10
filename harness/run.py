"""Runs a gobgpd binary through a scenario file and records CPU and memory.

  run.py --gobgpd PATH --scenario FILE.toml --out DIR

scenarios/example.toml describes every field of a scenario. A field that
holds a list runs every combination. One run of one combination: start gobgpd
and the receivers (sink), let the sender (blaster) load the table, then let
it announce the table again `storm.rounds` times. A phase ends when the
marker route that blaster sends after it reaches every receiver: gobgpd
handles the UPDATEs of a peer in order and sends to a peer through a FIFO
queue, so by then it has handled the whole phase and sent everything the
phase caused.
Nothing polls gobgpd while it works: a /metrics scrape allocates ~160 KiB
inside gobgpd and would show up in the numbers. Go memory stats are read once
before and once after a phase (building that page allocates a little in
gobgpd too; every run records how much as probe_alloc), GC cycles come from
GODEBUG=gctrace=1 and RSS and CPU are read from outside by psutil.
Every run is written to DIR/raw/<name>.json, DIR/env.json records the
machine, the binary and the scenario. blaster and sink come from work/bin
(scripts/build.sh).
"""

import argparse
import hashlib
import ipaddress
import itertools
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tomllib
import time
import urllib.request
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "work" / "bin"
DUT_MEMSTATS = "http://127.0.0.1:10160/debug/pprof/heap?debug=1"
DUT_PROFILE = "http://127.0.0.1:10160/debug/pprof/profile?seconds={}"
SINK_HTTP_PORT = 10180
AFI_SAFI = {"ipv4": "ipv4-unicast", "vpnv4": "l3vpn-ipv4-unicast"}
MEMSTATS = {"TotalAlloc": "total_alloc", "Mallocs": "mallocs", "NumGC": "num_gc", "HeapInuse": "heap_inuse"}
SETTLE_STATS = {"HeapAlloc": "heap_alloc", "HeapInuse": "heap_inuse", "HeapIdle": "heap_idle",
                "HeapReleased": "heap_released", "HeapSys": "heap_sys", "Sys": "sys", "NumGC": "num_gc"}
RESENDS = {  # resend -> (community kind it needs, how many values of it)
    "same": None, "changed": None, "standard-reversed": ("standard", 2), "extended-reversed": ("extended", 2),
    "large-reversed": ("large", 2), "attributes-reordered": ("standard", 1),
}
ORDERS = ("sorted", "random", "reversed")
DEFAULTS = {
    "family": "ipv4", "routes": 10000, "routes_per_update": 1, "receivers": 1, "repetitions": 3,
    "idle_after_load": 0,
    "communities": {"standard": 0, "extended": 0, "large": 0, "order": "sorted", "seed": 1},
    "storm": {"rounds": 0, "resend": "same"},
    "runtime": {"gomaxprocs": 2, "gogc": "", "gomemlimit": "", "gobgpd_cpus": "0,1", "sender_cpus": "2,3", "receiver_cpus": "2,3",
                "cpu_profile_seconds": 0},
}
GCTRACE = re.compile(r"^gc (\d+) @([\d.]+)s .* (\d+)->(\d+)->(\d+) MB")


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode()


def memstats(fields=MEMSTATS, gc=False):
    """runtime.MemStats of gobgpd from the pprof heap page; with gc the page
    runs a GC first."""
    m = {}
    for line in get(DUT_MEMSTATS + ("&gc=1" if gc else "")).splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0] == "#" and parts[1] in fields:
            m[fields[parts[1]]] = float(parts[3])
    return m


def sink(i=0):
    return json.loads(get(f"http://127.0.0.1:{SINK_HTTP_PORT + i}/stats"))


def sink_addrs(first, k):
    """Addresses of k receivers: first, then the next IPv4 addresses."""
    if k == 1:
        return [first]
    a = ipaddress.ip_address(first)
    assert a.version == 4, "several receivers need an IPv4 --sink-addr"
    return [str(a + i) for i in range(k)]


def load_scenario(path):
    """The scenario file with the defaults filled in: (settings, combinations).
    A combination is one concrete scenario; lists in family, receivers,
    storm.resend and several [[communities]] tables multiply."""
    def fail(msg):
        sys.exit(f"{path}: {msg}; see scenarios/example.toml")

    def table(given, defaults, where):
        if not isinstance(given, dict):
            fail(f"{where} must be a table")
        if unknown := sorted(given.keys() - defaults.keys()):
            fail(f"unknown field {where}{unknown[0]}, known: {', '.join(defaults)}")
        return {**defaults, **given}

    def choice(value, allowed, where):
        values = value if isinstance(value, list) else [value]
        for v in values:
            if v not in allowed:
                fail(f"{where} = {v!r}, must be one of: {', '.join(map(str, allowed))}")
        return values

    with open(path, "rb") as f:
        cfg = table(tomllib.load(f), DEFAULTS, "")
    cfg["storm"] = table(cfg["storm"], DEFAULTS["storm"], "storm.")
    cfg["runtime"] = table(cfg["runtime"], DEFAULTS["runtime"], "runtime.")
    sets = cfg["communities"] if isinstance(cfg["communities"], list) else [cfg["communities"]]
    sets = [table(c, DEFAULTS["communities"], "communities.") for c in sets]
    for c in sets:
        choice(c["order"], ORDERS, "communities.order")
    rounds = cfg["storm"]["rounds"]
    resends = choice(cfg["storm"]["resend"], list(RESENDS), "storm.resend") if rounds else ["same"]
    receivers = cfg["receivers"] if isinstance(cfg["receivers"], list) else [cfg["receivers"]]
    combos = []
    for family, comm, k, resend in itertools.product(choice(cfg["family"], list(AFI_SAFI), "family"), sets, receivers, resends):
        if need := RESENDS[resend]:
            if comm[need[0]] < need[1]:
                fail(f"storm.resend = {resend!r} needs communities.{need[0]} >= {need[1]}")
        combos.append({"family": family, "routes": cfg["routes"], "routes_per_update": cfg["routes_per_update"],
                       "receivers": k, "communities": comm, "rounds": rounds, "resend": resend,
                       "idle_after_load": cfg["idle_after_load"]})
    return cfg, combos


def label(sc):
    c = sc["communities"]
    return (f"{sc['family']}_{sc['resend'] if sc['rounds'] else 'load'}_"
            f"{c['standard']}-{c['extended']}-{c['large']}-{c['order']}_{sc['receivers']}rx")


def dut_config(family, sinks):
    """The config from live/dut.toml.tmpl with a neighbor per receiver."""
    tmpl = (ROOT / "live" / "dut.toml.tmpl").read_text().replace("{family}", AFI_SAFI[family])
    i = tmpl.index("[[neighbors]]", tmpl.index("[[neighbors]]") + 1)
    head, sink_block = tmpl[:i], tmpl[i:]
    head = head.replace('"{sink_addr}"', ", ".join(f'"{a}"' for a in sinks))
    return head + "\n".join(sink_block.replace("{sink_addr}", a) for a in sinks)


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
    def __init__(self, sc, rt, args, workdir):
        self.rt = rt
        family = sc["family"]
        conf = workdir / "dut.toml"
        addrs = sink_addrs(args.sink_addr, sc["receivers"])
        conf.write_text(dut_config(family, addrs))
        cmd = [str(args.gobgpd), "-f", str(conf), "--api-hosts", "127.0.0.1:10150",
               "--pprof-host", "127.0.0.1:10160", "--metrics-path", "", "--log-level", "warn"]
        if rt["gobgpd_cpus"] and sys.platform == "linux":
            cmd = ["taskset", "-c", rt["gobgpd_cpus"]] + cmd
        env = dict(os.environ, GOMAXPROCS=str(rt["gomaxprocs"]), GODEBUG="gctrace=1")
        if rt["gogc"]:
            env["GOGC"] = str(rt["gogc"])
        if rt["gomemlimit"]:
            env["GOMEMLIMIT"] = str(rt["gomemlimit"])
        self.log = open(workdir / "dut.log", "w")
        self.dut = subprocess.Popen(cmd, env=env, stdout=self.log, stderr=subprocess.PIPE, text=True)
        self.stderr = []  # [arrival time, line]; gctrace lines come as the GC ends
        threading.Thread(target=self.read_stderr, daemon=True).start()
        self.proc = psutil.Process(self.dut.pid)  # taskset execs gobgpd in place
        wait_for(memstats)
        receivers, sender = self.pin(rt["receiver_cpus"]), self.pin(rt["sender_cpus"])
        self.sinks = []
        for i, a in enumerate(addrs):
            host = f"[{a}]" if ":" in a else a
            self.sinks.append(subprocess.Popen(
                receivers + [str(BIN / "sink"), "-family", family, "-local", a, "-remote", f"{host}:10179",
                         "-http", f"127.0.0.1:{SINK_HTTP_PORT + i}", "-id", f"3.3.{i // 256}.{i % 256 + 3}"],
                stderr=self.log))
        c = sc["communities"]
        self.blaster = subprocess.Popen(
            sender + [str(BIN / "blaster"), "-family", family, "-routes", str(sc["routes"]),
                     "-per-update", str(sc["routes_per_update"]), "-resend", sc["resend"],
                     "-standard", str(c["standard"]), "-extended", str(c["extended"]), "-large", str(c["large"]),
                     "-order", c["order"], "-seed", str(c["seed"])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True)
        assert self.reply()["ready"]
        for i in range(len(addrs)):
            wait_for(lambda: sink(i)["established"])
        self.marker = 0
        # what one memstats() call allocates inside gobgpd; the snapshot
        # before a phase adds this much to the phase
        a, b = memstats(), memstats()
        self.probe_alloc = b["total_alloc"] - a["total_alloc"]

    @staticmethod
    def pin(cpus):
        return ["taskset", "-c", cpus] if cpus and sys.platform == "linux" else []

    def reply(self):
        line = self.blaster.stdout.readline()
        if not line:
            raise RuntimeError(f"blaster exited, see dut.log")
        return json.loads(line)

    def cpu(self):
        t = self.proc.cpu_times()
        return t.user + t.system

    def sink_stats(self):
        return [sink(i) for i in range(len(self.sinks))]

    def snapshot(self):
        s = memstats()
        t = self.proc.cpu_times()
        s["cpu"], s["cpu_user"], s["cpu_sys"] = t.user + t.system, t.user, t.system
        s["rss"] = self.proc.memory_info().rss
        s["time"] = time.time()
        # the peers: is the sender or a receiver the slowest stage?
        t = psutil.Process(self.blaster.pid).cpu_times()
        s["blaster_cpu"] = t.user + t.system
        s["sinks_cpu"] = sum(sum(psutil.Process(x.pid).cpu_times()[:2]) for x in self.sinks)
        sinks = self.sink_stats()
        for k in ("updates", "nlri", "withdrawn", "marker"):
            s["sink_" + k] = sum(x[k] for x in sinks)
        s["sinks"] = sinks
        return s

    def send(self, command):
        self.blaster.stdin.write(command + "\n")
        self.blaster.stdin.flush()
        return self.reply()

    def wait_marker(self, timeout=600):
        """Sends the next marker and waits until the sink has it."""
        self.marker += 1
        self.send(f"mark {self.marker}")
        deadline = time.time() + timeout
        while min(x["marker"] for x in self.sink_stats()) < self.marker:
            if time.time() > deadline:
                raise TimeoutError(f"marker {self.marker} did not reach the sink in {timeout}s")
            time.sleep(0.02)
        return time.time()

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

    def phase(self, command, profile=None):
        """With profile=(path, seconds) a CPU profile of gobgpd covers the
        phase and is written to path."""
        samples, stop = [], threading.Event()

        def sample():
            while not stop.is_set():
                try:
                    samples.append([round(time.time() - start, 3), self.proc.memory_info().rss])
                except psutil.Error:
                    pass
                stop.wait(0.1)

        prof = None
        if profile:
            prof = threading.Thread(target=self.profile, args=profile, daemon=True)
            prof.start()
            time.sleep(0.3)  # the handler starts the profile right away
        before = self.snapshot()
        start = time.time()
        th = threading.Thread(target=sample, daemon=True)
        th.start()
        try:
            sent = self.send(command)
            end = self.wait_marker()
            after = self.snapshot()
        finally:
            stop.set()  # a failed phase must not leave the sampler running
            th.join()
        if prof:
            prof.join()
            assert self.profile_end > end, "the profile ended before the load, raise runtime.cpu_profile_seconds"
        delta = {k: after[k] - before[k] for k in before if k not in ("rss", "heap_inuse", "time", "sinks")}
        delta["sink_nlri_each"] = [a["nlri"] - b["nlri"] for a, b in zip(after["sinks"], before["sinks"])]
        delta["seconds"] = end - start
        delta["rss_end"] = after["rss"]
        delta["heap_inuse_end"] = after["heap_inuse"]
        delta["rss_peak"] = max([s[1] for s in samples] + [after["rss"]])
        delta["sent_updates"] = sent["updates"]
        delta["sent_seconds"] = sent["seconds"]
        delta["rss_series"] = [s for s in samples if s[0] <= delta["seconds"]]
        delta["gc_series"] = self.gc_cycles(start, before["num_gc"] + 1, after["num_gc"])
        return delta

    def profile(self, path, seconds):
        with urllib.request.urlopen(DUT_PROFILE.format(seconds), timeout=seconds + 30) as r:
            path.write_bytes(r.read())
        self.profile_end = time.time()

    def settle(self, seconds):
        """Memory after the load: the heap right after a forced GC is what the
        table takes; RSS after `seconds` of idle time and another GC is how
        much of the peak gobgpd gives back. RSS is sampled in between."""
        start = time.time()
        after_gc = memstats(SETTLE_STATS, gc=True)
        after_gc["rss"] = self.proc.memory_info().rss
        series = []
        while time.time() - start < seconds:
            series.append([round(time.time() - start, 3), self.proc.memory_info().rss])
            time.sleep(0.5)
        settled = memstats(SETTLE_STATS, gc=True)
        settled["rss"] = self.proc.memory_info().rss
        return {"seconds": seconds, "after_gc": after_gc, "settled": settled, "rss_series": series}

    def close(self):
        for p in [self.blaster] + self.sinks + [self.dut]:
            p.kill()
            p.wait()
        self.log.close()


def run_one(sc, rt, args, profile=None):
    started = time.time()
    with tempfile.TemporaryDirectory() as d:
        r = Run(sc, rt, args, Path(d))
        try:
            load = r.phase("load", (profile, rt["cpu_profile_seconds"]) if profile else None)
            idle = r.settle(sc["idle_after_load"]) if sc["idle_after_load"] else None
            storm = r.phase(f"storm {sc['rounds']}") if sc["rounds"] else None
        except Exception:
            logs = (Path(d) / "dut.log").read_text().splitlines() + [line for _, line in r.stderr]
            print("\n".join(line for line in logs if not line.startswith("gc "))[-2000:], file=sys.stderr)
            raise
        finally:
            r.close()
    check(load, storm, sc)
    return {"scenario": sc, "probe_alloc": r.probe_alloc, "load": load, "idle": idle, "storm": storm,
            "started": started, "ended": time.time()}


def check(load, storm, sc):
    """Fails the run if the traffic was not what the scenario says. A gobgpd
    either sends every reordered re-announcement on or none of them."""
    n = sc["routes"]
    per_round = -(-n // sc["routes_per_update"])
    assert load["sent_updates"] == per_round, load["sent_updates"]
    for got in load["sink_nlri_each"]:
        assert got == n, f"a receiver got {got} routes on the load, want {n}"
    for phase in (load, storm):
        if phase:
            assert phase["sink_withdrawn"] == 0, f"the receivers got {phase['sink_withdrawn']} withdrawals"
    if storm is None:
        return
    assert storm["sent_updates"] == per_round * sc["rounds"], storm["sent_updates"]
    every = n * sc["rounds"]
    want = {"same": (0,), "changed": (every,)}.get(sc["resend"], (0, every))
    for got in storm["sink_nlri_each"]:
        assert got in want, f"a receiver got {got} routes in the storm, want {' or '.join(map(str, want))}"


def gobgpd_info(path):
    """Fails unless the binary runs here; returns what identifies it."""
    if not path.is_file():
        sys.exit(f"{path} is not a file; with Docker the path must be one the Docker VM shares with the host")
    try:
        version = subprocess.run([str(path), "--version"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as e:
        sys.exit(f"{path} does not run here ({e}); build it for this system, for the Docker run:\n"
                 f"  GOOS=linux GOARCH={platform.machine().replace('aarch64', 'arm64').replace('x86_64', 'amd64')} "
                 "CGO_ENABLED=0 go build -o gobgpd ./cmd/gobgpd")
    build = subprocess.run(["go", "version", "-m", str(path)], capture_output=True, text=True).stdout
    vcs = dict(line.split()[1].split("=", 1) for line in build.splitlines() if line.split()[:1] == ["build"] and "vcs" in line)
    return {"path": str(path), "version": version, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "revision": vcs.get("vcs.revision", ""), "modified": vcs.get("vcs.modified", "")}


def check_cpus(rt):
    if sys.platform != "linux":
        return
    have = os.sched_getaffinity(0)
    pins = {k: rt[k] for k in ("gobgpd_cpus", "sender_cpus", "receiver_cpus") if rt[k]}
    want = {int(c) for cpus in pins.values() for c in cpus.split(",")}
    if not want <= have:
        sys.exit(f"the scenario pins {', '.join(f'{k} = {v}' for k, v in pins.items())}, but only the CPUs "
                 f"{sorted(have)} are available: give the VM more CPUs or change these fields in [runtime]")


def write_env(args, cfg, info):
    def out(*cmd):
        return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()

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
        "gobgpd": info,
        "scenario_file": args.scenario.name,
        "scenario": cfg,
    }
    (args.out / "env.json").write_text(json.dumps(env, indent=2) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gobgpd", type=Path, required=True, help="the gobgpd binary to measure")
    p.add_argument("--scenario", type=Path, required=True, help="scenario file, see scenarios/example.toml")
    p.add_argument("--out", type=Path, required=True, help="directory for the results")
    p.add_argument("--sink-addr", default="127.0.0.2" if sys.platform == "linux" else "::1",
                   help="address of the first receiver; several receivers need Linux")
    args = p.parse_args()
    cfg, combos = load_scenario(args.scenario)
    rt = cfg["runtime"]
    check_cpus(rt)
    args.gobgpd = args.gobgpd.resolve()
    info = gobgpd_info(args.gobgpd)
    raw = args.out / "raw"
    shutil.rmtree(raw, ignore_errors=True)
    raw.mkdir(parents=True)
    prof = args.out / "prof"
    if rt["cpu_profile_seconds"]:
        prof.mkdir(exist_ok=True)
    write_env(args, cfg, info)
    total = len(combos) * cfg["repetitions"]
    done = 0
    print("warm-up run", flush=True)
    run_one(combos[0], rt, args)
    for rep in range(cfg["repetitions"]):
        for i, sc in enumerate(combos):
            name = f"{i + 1:02d}_{label(sc)}_rep{rep}"
            res = run_one(sc, rt, args, prof / f"{name}.pprof" if rt["cpu_profile_seconds"] else None)
            res["rep"] = rep
            (raw / f"{name}.json").write_text(json.dumps(res))
            done += 1
            st = res["storm"] or res["load"]
            print(f"[{done}/{total}] {name}: {'storm' if res['storm'] else 'load'} cpu {st['cpu']:.2f}s, "
                  f"allocated {st['total_alloc'] / 2**20:.0f} MiB, peak rss {st['rss_peak'] / 2**20:.0f} MiB, "
                  f"sent on {st['sink_nlri']:.0f}", flush=True)


if __name__ == "__main__":
    main()
