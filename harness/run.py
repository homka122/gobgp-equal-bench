"""Runs a gobgpd binary through a scenario file and records CPU and memory.

  run.py --gobgpd PATH --scenario FILE.toml --out DIR

scenarios/example.toml describes every field of a scenario. A field that
holds a list runs every combination. One run of one combination: start gobgpd
and the receivers (sink), let the sender (blaster) load the table, then let
it announce the table again `storm.rounds` times, and with [restart] let it
drop the session and come back as a restarting speaker. A phase ends when two
marker routes that blaster sends after it, one after the other, have reached
every receiver: gobgpd handles the UPDATEs of a peer in order and sends to a
peer through a FIFO queue, so by then it has handled the whole phase and sent
everything the phase caused. With receiver_stall the receivers read nothing
after the load, so the storm ends instead when gobgpd has used no CPU for a
second: it has handled every UPDATE of the storm, and what it could not send
waits in its queues. With [flap] a second receiver (flapper, peer A) joins,
and after the load the sender sends probe routes at a steady rate while A
either stays up or drops its session every few seconds, so that gobgpd sends
it the whole table again each time; the first receiver records how long each
probe took through gobgpd.
Nothing polls gobgpd while it works: a /metrics scrape allocates ~160 KiB
inside gobgpd and would show up in the numbers. Go memory stats are read once
before and once after a phase (building that page allocates a little in
gobgpd too; every run records how much as probe_alloc), GC cycles come from
GODEBUG=gctrace=1 and RSS and CPU are read from outside by psutil.
Every run is written to DIR/raw/<name>.json, DIR/env.json records the
machine, the binary and the scenario. blaster, sink and flapper come from
work/bin (scripts/build.sh).
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
FLAPPER_AS = 400
AFI_SAFI = {"ipv4": "ipv4-unicast", "vpnv4": "l3vpn-ipv4-unicast"}
MEMSTATS = {"TotalAlloc": "total_alloc", "Mallocs": "mallocs", "NumGC": "num_gc", "HeapInuse": "heap_inuse"}
SETTLE_STATS = {"HeapAlloc": "heap_alloc", "HeapInuse": "heap_inuse", "HeapIdle": "heap_idle",
                "HeapReleased": "heap_released", "HeapSys": "heap_sys", "Sys": "sys", "NumGC": "num_gc"}
RESENDS = {  # resend -> (community kind it needs, how many values of it)
    "same": None, "changed": None, "changed-community": None,
    "standard-reversed": ("standard", 2), "extended-reversed": ("extended", 2),
    "large-reversed": ("large", 2), "attributes-reordered": ("standard", 1), "changed-reordered": ("standard", 2),
}
ORDERS = ("sorted", "random", "reversed")
DEFAULTS = {
    "family": "ipv4", "routes": 10000, "routes_per_update": 1, "receivers": 1, "receiver_rate": 0, "receiver_stall": False,
    "repetitions": 3, "idle_after_load": 0, "hold_time": 90,
    "communities": {"standard": 0, "extended": 0, "large": 0, "order": "sorted", "seed": 1, "unique": False, "groups": 0},
    "storm": {"rounds": 0, "resend": "same"},
    "restart": {"pause": 0, "restart_time": 15, "llgr_stale_time": 86400, "receiver_llgr": False},
    "flap": {"seconds": 0, "interval": 0, "retry": 1, "probe_rate": 100, "announce": 0, "overlap": False},
    "runtime": {"gomaxprocs": 2, "gogc": "", "gomemlimit": "", "gobgpd_cpus": "0,1", "sender_cpus": "2,3", "receiver_cpus": "2,3",
                "cpu_profile_seconds": 0, "log_level": "warn"},
}
GR_LOG = re.compile(r"raceful|LLGR|[Ss]tale|[Pp]eer [Dd]own|[Pp]eer [Uu]p|EOR|End-of-RIB")
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


def tcp_queues(port=10179):
    """The established TCP connections to or from port, from /proc/net/tcp
    and tcp6: bytes the kernel holds, sent but not acknowledged by the peer
    (tx) and received but not read by the process (rx)."""
    out = []
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(f).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            x = line.split()
            local, remote, state = x[1], x[2], x[3]
            if state == "01" and port in (int(local.rsplit(":", 1)[1], 16), int(remote.rsplit(":", 1)[1], 16)):
                tx, rx = (int(v, 16) for v in x[4].split(":"))
                out.append({"local_port": int(local.rsplit(":", 1)[1], 16),
                            "remote_port": int(remote.rsplit(":", 1)[1], 16), "tx": tx, "rx": rx})
    return out


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
    cfg["restart"] = table(cfg["restart"], DEFAULTS["restart"], "restart.")
    cfg["flap"] = fl = table(cfg["flap"], DEFAULTS["flap"], "flap.")
    intervals = (fl["interval"] if isinstance(fl["interval"], list) else [fl["interval"]]) if fl["seconds"] else [0]
    rs = cfg["restart"]
    pauses = rs["pause"] if isinstance(rs["pause"], list) else [rs["pause"]]
    llgrs = rs["receiver_llgr"] if isinstance(rs["receiver_llgr"], list) else [rs["receiver_llgr"]]
    if any(x > 0 for x in pauses) and not rs["restart_time"]:
        fail("restart.pause needs restart.restart_time > 0")
    sets = cfg["communities"] if isinstance(cfg["communities"], list) else [cfg["communities"]]
    sets = [table(c, DEFAULTS["communities"], "communities.") for c in sets]
    for c in sets:
        choice(c["order"], ORDERS, "communities.order")
        if c["groups"] and (c["unique"] or cfg["routes_per_update"] != 1):
            fail("communities.groups needs routes_per_update = 1 and unique = false")
    rounds = cfg["storm"]["rounds"]
    if cfg["hold_time"] != 0 and not 3 <= cfg["hold_time"] <= 65535:
        fail("hold_time must be 0 or 3 to 65535 s (RFC 4271)")
    if fl["seconds"]:
        if any(x > 0 for x in pauses) or cfg["receiver_stall"]:
            fail("[flap] does not go with [restart] or receiver_stall")
        if not 0 < fl["probe_rate"] * fl["seconds"] <= 65536:
            fail("flap.probe_rate * flap.seconds must be 1 to 65536: every probe is a /32 of its own in 198.19.0.0/16")
        if fl["announce"] and choice(cfg["family"], list(AFI_SAFI), "family") != ["ipv4"]:
            fail("flap.announce needs family = \"ipv4\"")
        if fl["overlap"] and fl["announce"] > cfg["routes"]:
            fail("flap.overlap takes the prefixes of the table: flap.announce must be at most routes")
        # the table takes 10.0.0.0/24 on and the own routes of A 100.0.0.0/24 on,
        # 65536 routes to a first octet; they must not meet, nor reach 127.0.0.0/8
        if fl["announce"] and not fl["overlap"] and (
                10 + (cfg["routes"] - 1) // 65536 >= 100 or 100 + (fl["announce"] - 1) // 65536 >= 127):
            fail("routes or flap.announce too many: the table and the own routes of A would overlap")
    if cfg["receiver_stall"] and (not rounds or any(x > 0 for x in pauses)):
        fail("receiver_stall needs storm.rounds > 0 and no [restart]")
    resends = choice(cfg["storm"]["resend"], list(RESENDS), "storm.resend") if rounds else ["same"]
    receivers = cfg["receivers"] if isinstance(cfg["receivers"], list) else [cfg["receivers"]]
    combos = []
    for family, comm, k, resend, pause, llgr, interval in itertools.product(
            choice(cfg["family"], list(AFI_SAFI), "family"), sets, receivers, resends, pauses, llgrs, intervals):
        if need := RESENDS[resend]:
            if comm[need[0]] < need[1]:
                fail(f"storm.resend = {resend!r} needs communities.{need[0]} >= {need[1]}")
        combos.append({"family": family, "routes": cfg["routes"], "routes_per_update": cfg["routes_per_update"],
                       "receivers": k, "receiver_rate": cfg["receiver_rate"], "receiver_stall": cfg["receiver_stall"],
                       "communities": comm,
                       "rounds": rounds, "resend": resend, "idle_after_load": cfg["idle_after_load"],
                       "hold_time": cfg["hold_time"],
                       "restart": {**rs, "pause": pause, "receiver_llgr": llgr} if pause > 0 else None,
                       "flap": {**fl, "interval": interval} if fl["seconds"] else None})
    return cfg, combos


def label(sc):
    c = sc["communities"]
    return (f"{sc['family']}_{sc['resend'] if sc['rounds'] else 'load'}_"
            f"{c['standard']}-{c['extended']}-{c['large']}-{c['order']}{'-unique' if c['unique'] else ''}"
            f"{f'-groups{g}' if (g := c['groups']) else ''}_{sc['receivers']}rx{'-stall' if sc['receiver_stall'] else ''}"
            + (f"_restart{r['pause']:g}s{'-llgr' if r['receiver_llgr'] else ''}" if (r := sc["restart"]) else "")
            + ((f"_flap{f['interval']:g}s" if f["interval"] else "_steady") if (f := sc["flap"]) else ""))


def dut_config(family, sinks, neighbor_config="", restart=None, flapper=None):
    """The config from live/dut.toml.tmpl with a neighbor per receiver;
    neighbor_config is more lines for the config of every neighbor. With
    restart (scenario [restart]) every neighbor has graceful restart with
    notification and long-lived graceful restart, as gobgpd advertises them
    itself and as it helps a peer that restarts."""
    tmpl = (ROOT / "live" / "dut.toml.tmpl").read_text().replace("{family}", AFI_SAFI[family])
    tmpl = tmpl.replace("{neighbor_config}", neighbor_config.strip().replace("\n", "\n    "))
    gr = afi_gr = ""
    if restart:
        llgr = restart["llgr_stale_time"] > 0
        gr = ("[neighbors.graceful-restart.config]\n"
              "    enabled = true\n    notification-enabled = true\n"
              f"    long-lived-enabled = {str(llgr).lower()}\n    restart-time = {restart['restart_time']}")
        afi_gr = "[neighbors.afi-safis.mp-graceful-restart.config]\n      enabled = true"
        if llgr:
            afi_gr += ("\n    [neighbors.afi-safis.long-lived-graceful-restart.config]\n"
                       f"      enabled = true\n      restart-time = {restart['llgr_stale_time']}")
    tmpl = tmpl.replace("{graceful_restart}", gr).replace("{afi_safi_graceful_restart}", afi_gr)
    i = tmpl.index("[[neighbors]]", tmpl.index("[[neighbors]]") + 1)
    head, sink_block = tmpl[:i], tmpl[i:]
    head = head.replace('"{sink_addr}"', ", ".join(f'"{a}"' for a in sinks))
    # the flapping peer A has an AS of its own: a receiver in the AS of A
    # would not get its routes, they would carry the AS of the receiver
    return head + "\n".join(sink_block.replace("{sink_addr}", a).replace(
        "{peer_as}", str(FLAPPER_AS if a == flapper else 300)) for a in sinks)


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
        flap = sc["flap"]
        addrs = sink_addrs(args.sink_addr, sc["receivers"] + (1 if flap else 0))
        restart = sc["restart"]
        self.a_addr = addrs[-1] if flap else None
        conf.write_text(dut_config(family, addrs, args.neighbor_config, restart, self.a_addr))
        addrs = addrs[:-1] if flap else addrs
        hold = ["-hold", str(sc["hold_time"])]
        self.workdir = workdir
        cmd = [str(args.gobgpd), "-f", str(conf), "--api-hosts", "127.0.0.1:10150",
               "--pprof-host", "127.0.0.1:10160", "--metrics-path", "", "--log-level", rt["log_level"]]
        if rt["gobgpd_cpus"] and sys.platform == "linux":
            cmd = ["taskset", "-c", rt["gobgpd_cpus"]] + cmd
        env = dict(os.environ, GOMAXPROCS=str(rt["gomaxprocs"]), GODEBUG="gctrace=1")
        if rt["gogc"]:
            env["GOGC"] = str(rt["gogc"])
        if rt["gomemlimit"]:
            env["GOMEMLIMIT"] = str(rt["gomemlimit"])
        self.log = open(workdir / "dut.log", "w")
        self.dut = subprocess.Popen(cmd, env=env, stdout=self.log, stderr=subprocess.PIPE, text=True)
        self.blaster, self.sinks, self.flapper = None, [], None
        try:
            self.start_peers(sc, rt, family, addrs, restart, hold)
        except BaseException:
            self.close()  # run_one has no Run to close yet
            raise

    def start_peers(self, sc, rt, family, addrs, restart, hold):
        self.stderr = []  # [arrival time, line]; gctrace lines come as the GC ends
        threading.Thread(target=self.read_stderr, daemon=True).start()
        self.proc = psutil.Process(self.dut.pid)  # taskset execs gobgpd in place
        wait_for(memstats)
        receivers, sender = self.pin(rt["receiver_cpus"]), self.pin(rt["sender_cpus"])
        for i, a in enumerate(addrs):
            host = f"[{a}]" if ":" in a else a
            self.sinks.append(subprocess.Popen(
                receivers + [str(BIN / "sink"), "-family", family, "-local", a, "-remote", f"{host}:10179",
                         "-http", f"127.0.0.1:{SINK_HTTP_PORT + i}", "-id", f"3.3.{i // 256}.{i % 256 + 3}",
                         "-rate", str(sc["receiver_rate"])] + hold
                # the load ends with markers 1 and 2: a stalled receiver reads up to there
                + (["-stall-at", "2"] if sc["receiver_stall"] else [])
                + (["-table"] if restart else []) + (["-llgr"] if restart and restart["receiver_llgr"] else []),
                stderr=self.log))
        c = sc["communities"]
        self.blaster = subprocess.Popen(
            sender + [str(BIN / "blaster"), "-family", family, "-routes", str(sc["routes"]),
                     "-per-update", str(sc["routes_per_update"]), "-resend", sc["resend"],
                     "-standard", str(c["standard"]), "-extended", str(c["extended"]), "-large", str(c["large"]),
                     "-order", c["order"], "-seed", str(c["seed"])] + (["-unique"] if c["unique"] else []) + ["-groups", str(c["groups"])]
            + hold
            + (["-gr-restart-time", str(restart["restart_time"]), "-llgr-stale-time", str(restart["llgr_stale_time"])]
               if restart else []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True)
        assert self.reply()["ready"]
        for i in range(len(addrs)):
            wait_for(lambda: sink(i)["established"])
        if sc["flap"]:
            self.flapper = subprocess.Popen(
                receivers + [str(BIN / "flapper"), "-family", family, "-local", self.a_addr, "-remote", "127.0.0.1:10179",
                             "-routes", str(sc["routes"]), "-as", str(FLAPPER_AS), "-announce", str(sc["flap"]["announce"])]
                + (["-overlap"] if sc["flap"]["overlap"] else []) + hold,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True)
            if not (line := self.flapper.stdout.readline()) or not json.loads(line)["ready"]:
                raise RuntimeError("flapper did not come up, see dut.log")
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
        """Sends a marker, waits until the sinks have it, and does so once
        more. gobgpd sends the UPDATEs it packs in one pass in any order, so
        routes of the phase can follow the first marker. They are sent in
        that pass, and the second marker in a later one."""
        for _ in range(2):
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

    def wait_idle(self, timeout=600):
        """Waits until gobgpd has used under 0.02 s of CPU in a second and
        returns when that second began."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            t, c = time.time(), self.cpu()
            time.sleep(1)
            if self.cpu() - c < 0.02:
                return t
        raise TimeoutError(f"gobgpd did not go idle in {timeout}s")

    def phase(self, command, profile=None, then=(), idle=False):
        """With profile=(path, seconds) a CPU profile of gobgpd covers the
        phase and is written to path. The commands in then follow command
        before the markers. With idle the phase ends when gobgpd goes idle
        instead of with the markers."""
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
            for c in then:
                self.send(c)
            end = self.wait_idle() if idle else self.wait_marker()
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
        if idle:
            delta["tcp_queues"] = tcp_queues()
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

    def restart(self, r):
        """The sender drops the session without a NOTIFICATION, waits
        r["pause"] seconds, connects again as a restarting speaker, announces
        the table of the load and sends End-of-RIB; then the markers. The
        windows split the time from the drop: down until the Restart Time
        runs out or the sender is back, llgr from then until the sender is
        back, up while it announces the table, eor from End-of-RIB to the
        markers. Each window has what the receivers got in it and the peak
        RSS; the times are seconds from the drop. The sinks are sampled every
        0.1 s with the RSS; they keep the table, so the run can tell whether
        it is the table of the load again."""
        samples, stop = [], threading.Event()
        keys = ("nlri", "withdrawn", "llgr_stale", "updates")
        lasts = ("last_announce_unix_nano", "last_withdraw_unix_nano", "last_llgr_stale_unix_nano")

        def counters():
            st = self.sink_stats()
            c = {k: sum(x.get(k, 0) for x in st) for k in keys}
            c["last"] = max(x.get(k, 0) for x in st for k in lasts)
            return c

        def sample():
            while not stop.is_set():
                try:
                    c = counters()
                    samples.append([round(time.time() - start, 3), self.proc.memory_info().rss] + [c[k] for k in keys + ("last",)])
                except (psutil.Error, OSError):
                    pass
                stop.wait(0.1)

        before = self.snapshot()
        table_before = [{k: x.get(k) for k in ("table", "table_llgr_stale", "table_digest")} for x in before["sinks"]]
        at = {}
        start = time.time()
        th = threading.Thread(target=sample, daemon=True)
        th.start()
        try:
            self.send("down")
            at["down"] = (time.time() - start, counters())
            time.sleep(max(0, start + r["pause"] - time.time()))
            at["up"] = (time.time() - start, counters())
            up = self.send("up")
            at["eor"] = (time.time() - start, counters())
            self.send("eor")
            end = self.wait_marker()
            at["end"] = (end - start, counters())
            after = self.snapshot()
        finally:
            stop.set()
            th.join()
        bounds = [("down", at["down"]), ("up", at["up"]), ("eor", at["eor"]), ("end", at["end"])]
        if r["restart_time"] < at["up"][0]:
            t = r["restart_time"]
            before_t = [x for x in samples if x[0] <= t]
            c = dict(zip(keys + ("last",), before_t[-1][2:])) if before_t else at["down"][1]
            bounds.insert(1, ("llgr", (t, c)))
        windows = {}
        for (name, (t0, c0)), (_, (t1, c1)) in zip(bounds, bounds[1:]):
            inside = [x for x in samples if t0 <= x[0] < t1] or [[t0, after["rss"]]]
            w = {"start": round(t0, 3), "end": round(t1, 3), "rss_peak": max(x[1] for x in inside)}
            w.update({k: c1[k] - c0[k] for k in keys})
            # when the last route of the window came, from the drop
            last = c1["last"] / 1e9 - start
            w["last_route"] = round(last, 3) if c1["last"] and last >= t0 and w["nlri"] + w["withdrawn"] else None
            windows[name] = w

        def since(ns):
            return round(ns / 1e9 - start, 3) if ns and ns / 1e9 >= start else None

        sinks = []
        for x, b in zip(after["sinks"], table_before):
            sinks.append({"table": x.get("table", 0), "table_llgr_stale": x.get("table_llgr_stale", 0),
                          "table_digest": x.get("table_digest"), "table_after_load": b,
                          "same_table": x.get("table") == b["table"] and x.get("table_digest") == b["table_digest"]
                          and not x.get("table_llgr_stale"),
                          "last_announce": since(x.get("last_announce_unix_nano")),
                          "last_withdraw": since(x.get("last_withdraw_unix_nano")),
                          "last_llgr_stale": since(x.get("last_llgr_stale_unix_nano"))})
        return {"pause": r["pause"], "seconds": end - start, "windows": windows, "sinks": sinks,
                "rss_peak": max([x[1] for x in samples] + [after["rss"]]), "rss_end": after["rss"],
                "cpu": after["cpu"] - before["cpu"], "up_connect_seconds": up.get("connect_seconds"),
                "up_sent_seconds": up["seconds"], "up_sent_updates": up["updates"],
                "series": [x for x in samples if x[0] <= end - start], "series_fields": ["t", "rss"] + list(keys) + ["last_unix_nano"],
                "log": self.gr_log()}

    def flap(self, f):
        """The sender sends f["probe_rate"] probe routes a second for
        f["seconds"] seconds; meanwhile peer A (flapper) stays up or, with
        f["interval"], drops its session that many seconds after it came up,
        or after its own f["announce"] routes went out, and connects again,
        every f["retry"] seconds while gobgpd refuses.
        Then the markers. Each probe carries the time it was sent; the first
        receiver records when it came. Times are seconds from the start."""
        samples, stop = [], threading.Event()

        def sample():
            while not stop.is_set():
                try:
                    samples.append([round(time.time() - start, 3), self.proc.memory_info().rss])
                except psutil.Error:
                    pass
                stop.wait(0.1)

        def progress():
            # one line every 5 s: the probes of the last 5 s, the sessions of
            # A so far and the sessions gobgpd dropped other than A's
            seen = 0
            while not stop.wait(5):
                try:
                    got = json.loads(get(f"http://127.0.0.1:{SINK_HTTP_PORT}/probes"))
                    lat = [(c - s) / 1e9 for s, c in got[seen:]]
                    seen = len(got)
                    up, other = self.peer_events()
                    rss = self.proc.memory_info().rss
                except (OSError, psutil.Error) as e:
                    print(f"  · {time.time() - start:5.0f}s no progress: {e}", flush=True)
                    continue
                print(f"  · {time.time() - start:5.0f}s probes {len(got)}, last 5 s: {len(lat)} came, max delay "
                      f"{max(lat, default=0):.2f}s; A up {up} times; other peers down {other}; "
                      f"rss {rss / 2**20:.0f} MiB", flush=True)

        before = self.snapshot()
        start = time.time()
        th = threading.Thread(target=sample, daemon=True)
        th.start()
        pr = threading.Thread(target=progress, daemon=True)
        pr.start()
        try:
            self.flapper.stdin.write(f"flap {f['interval']} {f['seconds']} {f['retry']}\n")
            self.flapper.stdin.flush()
            try:
                sent = self.send(f"probe {f['probe_rate']} {f['seconds']}")
            except (RuntimeError, OSError):
                # gobgpd dropped the session of the sender: that is a result
                sent = {"updates": f["probe_rate"] * f["seconds"]}  # planned, not sent
            line = self.flapper.stdout.readline()
            if not line:
                raise RuntimeError("flapper exited, see dut.log")
            sessions = json.loads(line)["sessions"]
            # the sender and the receivers end themselves when their session
            # ends; the markers also wait out the stall the phase ends in
            def alive():
                return self.blaster.poll() is None and all(x.poll() is None for x in self.sinks)
            end = time.time()
            if alive():
                try:
                    end = self.wait_marker()
                except (RuntimeError, OSError):
                    pass
            kept = alive()
            try:
                c = sink(0)
                c = {"announced": c["nlri"] - before["sinks"][0]["nlri"],
                     "withdrawn": c["withdrawn"] - before["sinks"][0]["withdrawn"]}
            except OSError:
                c = None  # gobgpd dropped the session of the receiver
            t = self.proc.cpu_times()
            after = {"cpu": t.user + t.system, "rss": self.proc.memory_info().rss}
        finally:
            stop.set()
            th.join()
            pr.join()
        try:
            probes = json.loads(get(f"http://127.0.0.1:{SINK_HTTP_PORT}/probes"))
        except OSError:
            probes = []  # gobgpd dropped the session of the receiver
        series = sorted([round(s / 1e9 - start, 3), round((c - s) / 1e9, 4)] for s, c in probes)
        lat = sorted(x[1] for x in series)

        def q(p):
            return lat[min(len(lat) - 1, int(p * len(lat)))] if lat else None

        def since(ns):
            return round(ns / 1e9 - start, 3) if ns else None

        return {"seconds": end - start, "sent_probes": sent["updates"], "probes": len(series),
                "latency": {"p50": q(0.5), "p90": q(0.9), "p99": q(0.99), "max": lat[-1] if lat else None,
                            "over_1s": sum(x > 1 for x in lat), "over_100ms": sum(x > 0.1 for x in lat)},
                "probe_series": series, "series_fields": ["sent", "latency"],
                "sessions": [{"up": since(x["up_unix_nano"]), "full": since(x.get("full_unix_nano")),
                              "announced": since(x.get("announced_unix_nano")),
                              "down": since(x.get("down_unix_nano")), "routes": x["routes"], "attempts": x["attempts"],
                              "dropped_by_peer": x.get("dropped_by_peer", False)} for x in sessions],
                # what the routes of A cost the receiver: the routes it got
                # announced and withdrawn, the probes and markers left out
                "receiver_routes": c,
                "peers_kept": kept,
                "peers_down": self.peer_events()[1],
                "rss_peak": max([x[1] for x in samples] + [after["rss"]]), "rss_end": after["rss"],
                "rss_series": [x for x in samples if x[0] <= end - start],
                "cpu": after["cpu"] - before["cpu"], "log": self.gr_log()}

    def peer_events(self):
        """From the gobgpd log: how many times the session of A came up, and
        how many sessions of the other peers ended."""
        up = other = 0
        for line in (self.workdir / "dut.log").read_text().splitlines():
            if '"Peer Up"' in line and f'"{self.a_addr}"' in line:
                up += 1
            elif '"Peer Down"' in line and f'"{self.a_addr}"' not in line:
                other += 1
        return up, other

    def gr_log(self):
        """The lines of the gobgpd log about the sessions and graceful
        restart."""
        lines = (self.workdir / "dut.log").read_text().splitlines() + [line for _, line in self.stderr]
        return [line[:400] for line in lines if GR_LOG.search(line) and not line.startswith("gc ")][:200]

    def close(self):
        for p in [self.blaster, *self.sinks, self.flapper, self.dut]:
            if p:
                p.kill()
                p.wait()
        self.log.close()


def run_one(sc, rt, args, profile=None):
    started = time.time()
    with tempfile.TemporaryDirectory() as d:
        r = Run(sc, rt, args, Path(d))
        try:
            print(f"  · load of {sc['routes']} routes", flush=True)
            load = r.phase("load", (profile, rt["cpu_profile_seconds"]) if profile else None,
                           then=("eor",) if sc["restart"] else ())
            print(f"  · load done in {load['seconds']:.1f}s, peak rss {load['rss_peak'] / 2**20:.0f} MiB", flush=True)
            idle = r.settle(sc["idle_after_load"]) if sc["idle_after_load"] else None
            storm = r.phase(f"storm {sc['rounds']}", idle=sc["receiver_stall"]) if sc["rounds"] else None
            restart = r.restart(sc["restart"]) if sc["restart"] else None
            flap = r.flap(sc["flap"]) if sc["flap"] else None
        except Exception:
            logs = (Path(d) / "dut.log").read_text().splitlines() + [line for _, line in r.stderr]
            print("\n".join(line for line in logs if not line.startswith("gc "))[-2000:], file=sys.stderr)
            raise
        finally:
            r.close()
    check(load, storm, sc)
    if flap and flap["peers_kept"]:
        assert flap["probes"] == flap["sent_probes"], f"the receiver got {flap['probes']} of {flap['sent_probes']} probes"
    return {"scenario": sc, "probe_alloc": r.probe_alloc, "load": load, "idle": idle, "storm": storm,
            "restart": restart, "flap": flap, "started": started, "ended": time.time()}


def check(load, storm, sc):
    """Fails the run if the traffic was not what the scenario says. A gobgpd
    either sends every reordered re-announcement on or none of them; a changed
    route must be sent on however it is encoded. A gobgpd may merge the
    changes of a route that wait in its queues, so of the changes of a route
    it sends on at least one and at most all."""
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
    if sc["receiver_stall"]:
        for got in storm["sink_nlri_each"]:
            assert got == 0, f"a stalled receiver got {got} routes in the storm"
        return
    if sc["resend"] in ("changed", "changed-community", "changed-reordered"):
        for got in storm["sink_nlri_each"]:
            assert n <= got <= every, f"a receiver got {got} routes in the storm, want {n} to {every}"
        return
    want = (0,) if sc["resend"] == "same" else (0, every)
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
        "neighbor_config": args.neighbor_config,
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
    p.add_argument("--neighbor-config", default="",
                   help="lines to add to the config of every neighbor, for options of this gobgpd")
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
            if fl := res["flap"]:
                lt, ss = fl["latency"], fl["sessions"]
                dumps = [x["full"] - x["up"] for x in ss if x["full"] is not None]
                print(f"[{done}/{total}] {name}: probes {fl['probes']}/{fl['sent_probes']}, latency p50 {lt['p50'] or 0:.3f}s "
                      f"p99 {lt['p99'] or 0:.3f}s max {lt['max'] or 0:.3f}s, over 1 s {lt['over_1s']}; A sessions {len(ss)}, "
                      f"full table in {min(dumps, default=0):.1f}-{max(dumps, default=0):.1f}s; peers kept {fl['peers_kept']}, "
                      f"other sessions down {fl['peers_down']}; "
                      + (f"C got +{rc['announced']}/-{rc['withdrawn']}; " if (rc := fl["receiver_routes"]) else "")
                      + f"peak rss {fl['rss_peak'] / 2**20:.0f} MiB, cpu {fl['cpu']:.1f}s", flush=True)
                continue
            if rs := res["restart"]:
                w = rs["windows"]
                print(f"[{done}/{total}] {name}: load peak rss {res['load']['rss_peak'] / 2**20:.0f} MiB; restart peak rss "
                      f"{rs['rss_peak'] / 2**20:.0f} MiB, " + ", ".join(
                          f"{k} +{v['nlri']}/-{v['withdrawn']}/llgr {v['llgr_stale']}" for k, v in w.items())
                      + f", done {rs['seconds']:.1f}s, same table {all(x['same_table'] for x in rs['sinks'])}", flush=True)
                continue
            st = res["storm"] or res["load"]
            print(f"[{done}/{total}] {name}: {'storm' if res['storm'] else 'load'} cpu {st['cpu']:.2f}s, "
                  f"allocated {st['total_alloc'] / 2**20:.0f} MiB, peak rss {st['rss_peak'] / 2**20:.0f} MiB, "
                  f"sent on {st['sink_nlri']:.0f}"
                  + (f", in the kernel: sent unacknowledged {sum(q['tx'] for q in qs) / 2**20:.1f} MiB, "
                     f"received unread {sum(q['rx'] for q in qs) / 2**20:.1f} MiB"
                     if (qs := st.get("tcp_queues")) is not None else ""), flush=True)


if __name__ == "__main__":
    main()
