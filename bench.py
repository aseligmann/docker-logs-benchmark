#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Benchmark daemon-side cost of `docker logs` filtering (--since / --tail).

Measures dockerd CPU, logical/physical reads, and peak RSS from inside the
OrbStack VM via a privileged --pid=host helper container, comparing:
  full read | --since T | --tail N | --tail N --since T
where N is sized so tail+since returns every line since T.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import random
import re
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

LABEL = "dlb"
HELPER_NAME = "dlb-helper"
HELPER_PORT = 8377
HELPER_IMAGE = "python:3.12-alpine"
GEN_IMAGE = "busybox"
SEQ_WIDTH = 12
TS_LEN = 30  # RFC3339NanoFixed: 2026-06-10T08:01:02.123456789Z
TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{9}Z")
CHUNK = 1 << 20

HELPER_SRC = r"""
import http.server, json, os, time
from urllib.parse import urlparse, parse_qs

def find_dockerd():
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % entry) as f:
                if f.read().strip() == "dockerd":
                    return int(entry)
        except OSError:
            continue
    return None

PID = [find_dockerd()]

def pid():
    if PID[0] is None or not os.path.exists("/proc/%d" % PID[0]):
        PID[0] = find_dockerd()
    if PID[0] is None:
        raise RuntimeError("dockerd process not found in host pid namespace")
    return PID[0]

def read_sample():
    p = pid()
    with open("/proc/%d/stat" % p) as f:
        stat = f.read()
    rest = stat[stat.rindex(")") + 2 :].split()
    io = {}
    with open("/proc/%d/io" % p) as f:
        for line in f:
            k, v = line.split(":")
            io[k.strip()] = int(v)
    status = {}
    with open("/proc/%d/status" % p) as f:
        for line in f:
            if line.startswith(("VmRSS", "VmHWM")):
                k, v = line.split(":")
                status[k] = int(v.split()[0])
    return {
        "pid": p,
        "utime_ticks": int(rest[11]),
        "stime_ticks": int(rest[12]),
        "clk_tck": os.sysconf("SC_CLK_TCK"),
        "rchar": io["rchar"],
        "wchar": io["wchar"],
        "read_bytes": io["read_bytes"],
        "write_bytes": io["write_bytes"],
        "vm_rss_kb": status["VmRSS"],
        "vm_hwm_kb": status["VmHWM"],
        "monotonic": time.monotonic(),
    }

def log_files(cid):
    base = "/proc/%d/root/var/lib/docker/containers/%s" % (pid(), cid)
    out = []
    for sub in ("", "local-logs"):
        d = os.path.join(base, sub) if sub else base
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if "-json.log" in name or name.startswith("container.log"):
                rel = "%s/%s" % (sub, name) if sub else name
                out.append({"name": rel, "size": os.path.getsize(os.path.join(d, name))})
    return out

class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path == "/sample":
                self._send(200, read_sample())
            elif url.path == "/logfiles":
                cid = parse_qs(url.query)["cid"][0]
                self._send(200, {"files": log_files(cid)})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": repr(e)})

    def do_POST(self):
        try:
            if self.path == "/clear_hwm":
                with open("/proc/%d/clear_refs" % pid(), "w") as f:
                    f.write("5")
                self._send(200, {"ok": True})
            elif self.path == "/drop_caches":
                os.sync()
                with open("/proc/sys/vm/drop_caches", "w") as f:
                    f.write("3")
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": repr(e)})

    def log_message(self, *args):
        pass

http.server.HTTPServer(("0.0.0.0", 8377), Handler).serve_forever()
"""


def docker(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], check=check, capture_output=capture, text=capture
    )


def docker_out(*args: str) -> str:
    return docker(*args).stdout.strip()


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# Helper container / sampling


class Sampler:
    def __init__(self) -> None:
        self.base = f"http://127.0.0.1:{HELPER_PORT}"

    def _req(self, path: str, method: str = "GET", timeout: float = 60.0) -> dict:
        req = urllib.request.Request(self.base + path, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def ensure_helper(self) -> None:
        state = docker(
            "inspect", "-f", "{{.State.Status}}", HELPER_NAME, check=False
        ).stdout.strip()
        if state != "running":
            docker("rm", "-f", HELPER_NAME, check=False)
            print(f"starting helper container {HELPER_NAME} ...")
            docker(
                "run", "-d", "--name", HELPER_NAME, f"--label={LABEL}=1",
                "--privileged", "--pid=host",
                "-p", f"127.0.0.1:{HELPER_PORT}:{HELPER_PORT}",
                HELPER_IMAGE, "python3", "-c", HELPER_SRC,
            )
        deadline = time.monotonic() + 120
        while True:
            try:
                s = self._req("/sample", timeout=5)
                print(f"helper ready, dockerd pid {s['pid']}")
                return
            except (urllib.error.URLError, OSError, TimeoutError):
                if time.monotonic() > deadline:
                    logs = docker("logs", HELPER_NAME, check=False).stderr
                    raise RuntimeError(f"helper did not become healthy; logs:\n{logs}") from None
                time.sleep(0.5)

    def sample(self) -> dict:
        return self._req("/sample")

    def clear_hwm(self) -> None:
        self._req("/clear_hwm", method="POST")

    def drop_caches(self) -> None:
        self._req("/drop_caches", method="POST")

    def log_files(self, cid: str) -> list[dict]:
        return self._req(f"/logfiles?cid={cid}")["files"]

    def idle_baseline(self, pairs: int = 5, interval: float = 2.0) -> dict:
        deltas = []
        for _ in range(pairs):
            a = self.sample()
            time.sleep(interval)
            b = self.sample()
            deltas.append(sample_delta(a, b))
        return {
            "pairs": pairs,
            "interval_s": interval,
            "cpu_s_median": statistics.median(d["cpu_s"] for d in deltas),
            "cpu_s_max": max(d["cpu_s"] for d in deltas),
            "rchar_median": statistics.median(d["rchar"] for d in deltas),
            "rchar_max": max(d["rchar"] for d in deltas),
            "deltas": deltas,
        }


def sample_delta(before: dict, after: dict) -> dict:
    ticks = (after["utime_ticks"] + after["stime_ticks"]) - (
        before["utime_ticks"] + before["stime_ticks"]
    )
    return {
        "cpu_s": ticks / after["clk_tck"],
        "rchar": after["rchar"] - before["rchar"],
        "wchar": after["wchar"] - before["wchar"],
        "read_bytes": after["read_bytes"] - before["read_bytes"],
        "write_bytes": after["write_bytes"] - before["write_bytes"],
        "peak_rss_kb": after["vm_hwm_kb"],
        "rss_before_kb": before["vm_rss_kb"],
        "rss_after_kb": after["vm_rss_kb"],
        "elapsed_s": after["monotonic"] - before["monotonic"],
    }


# ---------------------------------------------------------------------------
# Setup: generators + cutoff scan


def gen_container_name(config: str, suffix: str) -> str:
    return f"dlb-gen{suffix}-{config}"


def rotate_opts(max_size: str, max_file: int) -> list[str]:
    return ["--log-opt", f"max-size={max_size}", "--log-opt", f"max-file={max_file}"]


def awk_program(lines: int, line_bytes: int) -> str:
    pad = line_bytes - SEQ_WIDTH - 2  # space + newline
    if pad < 1:
        raise SystemExit(f"--line-bytes too small (need > {SEQ_WIDTH + 2})")
    return (
        f'BEGIN {{ pad=""; while (length(pad) < {pad}) pad = pad "x"; '
        f'for (i = 1; i <= {lines}; i++) printf "%0{SEQ_WIDTH}d %s\\n", i, pad }}'
    )


def generator_labels(lines: int, line_bytes: int, config: str, log_opts: list[str]) -> dict:
    return {
        LABEL: "1",
        f"{LABEL}.lines": str(lines),
        f"{LABEL}.line_bytes": str(line_bytes),
        f"{LABEL}.config": config,
        f"{LABEL}.log_opts": ",".join(log_opts),
    }


def existing_generator_ok(name: str, want_labels: dict) -> bool:
    out = docker("inspect", name, check=False)
    if out.returncode != 0:
        return False
    info = json.loads(out.stdout)[0]
    labels = info["Config"]["Labels"] or {}
    state = info["State"]
    if state["Status"] != "exited" or state["ExitCode"] != 0:
        return False
    return all(labels.get(k) == v for k, v in want_labels.items())


def make_generator(
    config: str, suffix: str, lines: int, line_bytes: int, log_opts: list[str]
) -> str:
    name = gen_container_name(config, suffix)
    labels = generator_labels(lines, line_bytes, config, log_opts)
    if existing_generator_ok(name, labels):
        print(f"[{config}] reusing existing generator container {name}")
        return name
    docker("rm", "-f", name, check=False)
    label_args = [f"--label={k}={v}" for k, v in labels.items()]
    print(f"[{config}] generating {lines} lines x {line_bytes}B in {name} ...")
    t0 = time.monotonic()
    docker(
        "run", "-d", "--name", name, *label_args, *log_opts,
        GEN_IMAGE, "awk", awk_program(lines, line_bytes),
    )
    code = docker_out("wait", name)
    if code != "0":
        raise SystemExit(f"generator {name} exited with code {code}")
    print(f"[{config}] generation done in {time.monotonic() - t0:.1f}s")
    return name


def scan_cutoffs(container: str, total: int, percentiles: list[int]) -> dict:
    """One timestamped pass: full gap check + per-percentile cutoff table.

    Timestamps are kept as opaque RFC3339NanoFixed strings; same-width UTC
    strings compare lexicographically, and --since is inclusive, so the
    expected first line for cutoff T is the first line of the run of lines
    sharing T's nanosecond timestamp.
    """
    targets = {p: min(total - 1, (p * total) // 100) for p in percentiles}
    print(f"[{container}] scanning timestamps for cutoffs at {percentiles}% ...")
    proc = subprocess.Popen(
        ["docker", "logs", "--timestamps", container],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    cutoffs: dict[str, dict] = {}
    run_start = 0
    prev_ts = ""
    i = 0
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.decode()
        ts = line[:TS_LEN]
        if not TS_RE.fullmatch(ts):
            raise SystemExit(f"line {i}: unexpected timestamp format: {ts!r}")
        if ts < prev_ts:
            raise SystemExit(f"line {i}: non-monotonic timestamp {ts} < {prev_ts}")
        seq = int(line[TS_LEN + 1 : TS_LEN + 1 + SEQ_WIDTH])
        if seq != i + 1:
            raise SystemExit(
                f"line {i}: sequence gap (got {seq}, expected {i + 1}) - "
                "log lines were lost (rotation capacity too small?)"
            )
        if ts != prev_ts:
            run_start = i
            prev_ts = ts
        for p, k in targets.items():
            if i == k:
                cutoffs[str(p)] = {"T": ts, "expected_first": run_start}
        i += 1
    if proc.wait() != 0:
        raise SystemExit(f"docker logs --timestamps failed: {proc.stderr.read().decode()}")
    if i != total:
        raise SystemExit(f"[{container}] expected {total} lines, scanned {i}")
    for c in cutoffs.values():
        count = total - c["expected_first"]
        c["expected_count"] = count
        c["n_tail"] = max(math.ceil(1.1 * count), count + 1000)
    print(f"[{container}] scan OK: {i} lines, no gaps")
    return cutoffs


def container_id(name: str) -> str:
    return docker_out("inspect", "-f", "{{.Id}}", name)


def config_log_opts(config: str, args) -> list[str]:
    # The daemon may default to rotation (OrbStack: max-size=20m,max-file=5),
    # so "single" must explicitly request one big file.
    if config == "single":
        return rotate_opts(args.single_max_size, 1)
    if config == "rotated":
        return rotate_opts(args.rotate_max_size, args.rotate_max_file)
    if config == "local":
        return [
            "--log-driver", "local",
            "--log-opt", f"max-size={args.local_max_size}",
            "--log-opt", f"max-file={args.local_max_file}",
            "--log-opt", "compress=true",
        ]
    raise SystemExit(f"unknown config {config!r} (expected single, rotated, local)")


def do_setup(args, sampler: Sampler) -> dict:
    state_path = state_file(args)
    suffix = "-smoke" if args.smoke_variant else ""
    configs = {}
    if state_path.exists():
        prev = json.loads(state_path.read_text())
        if prev.get("lines") == args.lines and prev.get("line_bytes") == args.line_bytes:
            configs = prev.get("configs", {})
    for config in args.configs:
        log_opts = config_log_opts(config, args)
        name = make_generator(config, suffix, args.lines, args.line_bytes, log_opts)
        cutoffs = scan_cutoffs(name, args.lines, args.percentiles)
        files = sampler.log_files(container_id(name))
        on_disk = sum(f["size"] for f in files)
        cfg_state = {
            "container": name,
            "total": args.lines,
            "log_opts": log_opts,
            "cutoffs": cutoffs,
            "log_files": files,
        }
        print(f"[{config}] {len(files)} log file(s), {on_disk / 1e6:.1f} MB on disk")
        if config == "single" and len(files) != 1:
            raise SystemExit(
                f"[single] expected exactly 1 log file, found {len(files)}; "
                "raise --single-max-size"
            )
        if config == "rotated":
            capacity = parse_size(args.rotate_max_size) * args.rotate_max_file
            if on_disk > 0.9 * capacity:
                print(f"[{config}] WARNING: <10% rotation headroom; raise --rotate-max-file")
        configs[config] = cfg_state
    state = {
        "created": utc_stamp(),
        "lines": args.lines,
        "line_bytes": args.line_bytes,
        "configs": configs,
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2))
    print(f"state written to {state_path}")
    return state


def parse_size(s: str) -> int:
    units = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}
    if s[-1].lower() in units:
        return int(s[:-1]) * units[s[-1].lower()]
    return int(s)


def state_file(args) -> Path:
    return Path(args.out) / "state.json"


def state_matches(state: dict, args) -> bool:
    if not state:
        return False
    if state.get("lines") != args.lines or state.get("line_bytes") != args.line_bytes:
        return False
    for config in args.configs:
        cfg = state.get("configs", {}).get(config)
        if cfg is None:
            return False
        if not all(str(p) in cfg["cutoffs"] for p in args.percentiles):
            return False
        if not existing_generator_ok(
            cfg["container"],
            generator_labels(args.lines, args.line_bytes, config, cfg["log_opts"]),
        ):
            return False
    return True


# ---------------------------------------------------------------------------
# Measured runs


@dataclass
class Cell:
    config: str
    scenario: str
    percentile: int | None
    cmd: list[str]
    expected_first: int
    expected_last: int
    expected_count: int

    @property
    def key(self) -> str:
        p = f"@p{self.percentile}" if self.percentile is not None else ""
        return f"{self.config}/{self.scenario}{p}"


def build_cells(state: dict, configs: list[str], percentiles: list[int]) -> list[Cell]:
    cells = []
    for config in configs:
        cfg = state["configs"][config]
        c, total = cfg["container"], cfg["total"]
        cells.append(Cell(config, "full", None, ["docker", "logs", c], 1, total, total))
        for p in percentiles:
            cut = cfg["cutoffs"][str(p)]
            t, n = cut["T"], cut["n_tail"]
            ef, ec = cut["expected_first"], cut["expected_count"]
            tail_first = max(1, total - n + 1)
            tail_count = min(n, total)
            cells.append(
                Cell(config, "since", p, ["docker", "logs", "--since", t, c], ef + 1, total, ec)
            )
            cells.append(
                Cell(
                    config, "tail", p,
                    ["docker", "logs", "--tail", str(n), c],
                    tail_first, total, tail_count,
                )
            )
            cells.append(
                Cell(
                    config, "tail_since", p,
                    ["docker", "logs", "--tail", str(n), "--since", t, c],
                    ef + 1, total, ec,
                )
            )
    return cells


@dataclass
class StreamResult:
    count: int = 0
    first_seq: int | None = None
    last_seq: int | None = None
    gap_ok: bool | None = None
    bytes_read: int = 0


def consume_stream(stream, gap_check: bool) -> StreamResult:
    res = StreamResult()
    head = b""
    tail = b""
    leftover = b""
    expected = None
    gap_ok = True
    while True:
        chunk = stream.read(CHUNK)
        if not chunk:
            break
        res.bytes_read += len(chunk)
        res.count += chunk.count(b"\n")
        if not head:
            head = chunk[:4096]
        tail = (tail + chunk)[-4096:]
        if gap_check:
            buf = leftover + chunk
            lines = buf.split(b"\n")
            leftover = lines.pop()
            for ln in lines:
                seq = int(ln[:SEQ_WIDTH])
                if expected is not None and seq != expected:
                    gap_ok = False
                expected = seq + 1
    if head:
        res.first_seq = int(head[:SEQ_WIDTH])
        last_line = tail.rstrip(b"\n").rsplit(b"\n", 1)[-1]
        res.last_seq = int(last_line[:SEQ_WIDTH])
    if gap_check:
        res.gap_ok = gap_ok
    return res


def run_measured(cell: Cell, cache_mode: str, sampler: Sampler, gap_check: bool) -> dict:
    sampler.clear_hwm()
    if cache_mode == "cold":
        sampler.drop_caches()
    before = sampler.sample()
    t0 = time.monotonic()
    proc = subprocess.Popen(cell.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr_chunks: list[bytes] = []
    drain = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()))
    drain.start()
    stream = consume_stream(proc.stdout, gap_check)
    rc = proc.wait()
    wall = time.monotonic() - t0
    drain.join()
    after = sampler.sample()

    issues = []
    if rc != 0:
        issues.append(f"exit code {rc}: {b''.join(stderr_chunks).decode(errors='replace')[:500]}")
    if stream.count != cell.expected_count:
        issues.append(f"count {stream.count} != expected {cell.expected_count}")
    if stream.first_seq != cell.expected_first:
        issues.append(f"first seq {stream.first_seq} != expected {cell.expected_first}")
    if stream.last_seq != cell.expected_last:
        issues.append(f"last seq {stream.last_seq} != expected {cell.expected_last}")
    if stream.gap_ok is False:
        issues.append("sequence gaps in output")

    return {
        "cell": cell.key,
        "config": cell.config,
        "scenario": cell.scenario,
        "percentile": cell.percentile,
        "cmd": cell.cmd,
        "cache_mode": cache_mode,
        "wall_s": wall,
        "exit_code": rc,
        "line_count": stream.count,
        "first_seq": stream.first_seq,
        "last_seq": stream.last_seq,
        "stream_bytes": stream.bytes_read,
        "gap_checked": gap_check,
        "valid": not issues,
        "issues": issues,
        "before": before,
        "after": after,
        "delta": sample_delta(before, after),
    }


def check_preconditions(force: bool) -> None:
    problems = []
    out = docker_out("ps", "--format", f'{{{{.Names}}}}\t{{{{.Label "{LABEL}"}}}}')
    foreign = [ln.split("\t")[0] for ln in out.splitlines() if ln and not ln.endswith("\t1")]
    if foreign:
        problems.append(f"other containers are running ({', '.join(foreign)})")
    pg = subprocess.run(
        ["pgrep", "-f", "bench.py run|docker logs"], capture_output=True, text=True
    )
    own = {os.getpid(), os.getppid()}
    others = [pid for pid in pg.stdout.split() if int(pid) not in own]
    if others:
        problems.append(f"other benchmark/docker-logs processes running (pids {others})")
    for msg in problems:
        if force:
            print(f"WARNING: {msg}; results may be noisy")
        else:
            raise SystemExit(f"refusing to start: {msg} (use --force to override)")


def env_info() -> dict:
    return {
        "docker_version": docker_out("version", "--format", "{{.Server.Version}}"),
        "docker_os": docker_out("info", "--format", "{{.OperatingSystem}}"),
        "logging_driver": docker_out("info", "--format", "{{.LoggingDriver}}"),
        "host_platform": platform.platform(),
    }


def do_run(args, sampler: Sampler, state: dict) -> Path:
    check_preconditions(args.force)
    cells = build_cells(state, args.configs, args.percentiles)
    modes = ["warm", "cold"] if args.cache == "both" else [args.cache]
    print("measuring idle baseline ...")
    baseline = sampler.idle_baseline(
        pairs=args.baseline_pairs, interval=args.baseline_interval
    )
    print(
        f"idle noise floor: cpu {baseline['cpu_s_median']:.3f}s/"
        f"{baseline['interval_s']:.0f}s, rchar {baseline['rchar_median'] / 1e6:.1f} MB"
    )
    runs = []
    rng = random.Random(20260610)
    total_runs = len(cells) * args.reps * len(modes)
    done = 0
    for mode in modes:
        if mode == "warm":
            for config in args.configs:
                c = state["configs"][config]["container"]
                print(f"[warm-up] full read of {c} ...")
                subprocess.run(
                    ["docker", "logs", c], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, check=True,
                )
        gap_checked: set[str] = set()
        for rep in range(args.reps):
            order = cells[:]
            rng.shuffle(order)
            for cell in order:
                gap_check = cell.key not in gap_checked
                gap_checked.add(cell.key)
                rec = run_measured(cell, mode, sampler, gap_check)
                rec["rep"] = rep
                runs.append(rec)
                done += 1
                d = rec["delta"]
                flag = "" if rec["valid"] else "  INVALID: " + "; ".join(rec["issues"])
                print(
                    f"[{done}/{total_runs}] {mode:4s} {cell.key:28s} "
                    f"wall {rec['wall_s']:7.3f}s cpu {d['cpu_s']:6.2f}s "
                    f"rchar {d['rchar'] / 1e6:8.1f}MB read {d['read_bytes'] / 1e6:8.1f}MB"
                    f"{flag}"
                )
    session = {
        "started": utc_stamp(),
        "env": env_info(),
        "params": {
            "lines": args.lines,
            "line_bytes": args.line_bytes,
            "reps": args.reps,
            "cache": args.cache,
            "configs": args.configs,
            "percentiles": args.percentiles,
        },
        "state": state,
        "baseline": baseline,
        "runs": runs,
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"session-{session['started']}.json"
    json_path.write_text(json.dumps(session, indent=2))
    md_path = json_path.with_suffix(".md")
    md_path.write_text(render_report(session))
    invalid = [r for r in runs if not r["valid"]]
    print(f"\nwrote {json_path}\nwrote {md_path}")
    if invalid:
        print(f"WARNING: {len(invalid)} invalid run(s):")
        for r in invalid:
            print(f"  {r['cache_mode']} {r['cell']} rep {r['rep']}: {'; '.join(r['issues'])}")
    return json_path


# ---------------------------------------------------------------------------
# Reporting


def fmt_stat(values: list[float], scale: float = 1.0, prec: int = 2) -> str:
    vals = [v / scale for v in values]
    med = statistics.median(vals)
    if len(vals) == 1:
        return f"{med:.{prec}f}"
    return f"{med:.{prec}f} ({min(vals):.{prec}f}-{max(vals):.{prec}f})"


def render_report(session: dict) -> str:
    runs = session["runs"]
    p = session["params"]
    b = session["baseline"]
    lines = [
        "# docker logs filtering benchmark",
        "",
        f"- started: {session['started']}",
        f"- docker {session['env']['docker_version']} on {session['env']['docker_os']}, "
        f"driver {session['env']['logging_driver']}",
        f"- log: {p['lines']} lines x {p['line_bytes']} B, reps {p['reps']}, "
        f"cache {p['cache']}",
        f"- idle noise floor (per {b['interval_s']:.0f}s): "
        f"cpu {b['cpu_s_median']:.3f}s (max {b['cpu_s_max']:.3f}), "
        f"rchar {b['rchar_median'] / 1e6:.1f} MB (max {b['rchar_max'] / 1e6:.1f})",
        "",
        "Metrics are dockerd deltas per run. `rchar` = logical bytes read (primary);",
        "`read_bytes` = block-layer reads, only meaningful for cold cache;",
        "`peak_rss` = VmHWM after a pre-run reset; wall includes client transport.",
        "",
    ]
    modes = sorted({r["cache_mode"] for r in runs})
    scenario_order = {"full": 0, "since": 1, "tail": 2, "tail_since": 3}
    for mode in modes:
        for config in p["configs"]:
            sel = [r for r in runs if r["cache_mode"] == mode and r["config"] == config]
            if not sel:
                continue
            lines.append(f"## {mode} cache - {config} config")
            lines.append("")
            lines.append(
                "| scenario | wall_s | cpu_s | rchar_MB | read_bytes_MB | peak_rss_MB |"
                " lines out | valid |"
            )
            lines.append("|---|---|---|---|---|---|---|---|")
            groups: dict[tuple, list[dict]] = {}
            for r in sel:
                groups.setdefault((r["scenario"], r["percentile"]), []).append(r)
            for (scenario, pct), rs in sorted(
                groups.items(), key=lambda kv: (scenario_order[kv[0][0]], kv[0][1] or 0)
            ):
                name = scenario if pct is None else f"{scenario}@p{pct}"
                ds = [r["delta"] for r in rs]
                valid = "yes" if all(r["valid"] for r in rs) else "**NO**"
                lines.append(
                    f"| {name} "
                    f"| {fmt_stat([r['wall_s'] for r in rs])} "
                    f"| {fmt_stat([d['cpu_s'] for d in ds])} "
                    f"| {fmt_stat([d['rchar'] for d in ds], 1e6, 1)} "
                    f"| {fmt_stat([d['read_bytes'] for d in ds], 1e6, 1)} "
                    f"| {fmt_stat([d['peak_rss_kb'] for d in ds], 1e3, 1)} "
                    f"| {rs[0]['line_count']} "
                    f"| {valid} |"
                )
            lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Smoke test


def do_smoke(args) -> None:
    print("=== smoke test: 20k lines, 1 rep, p50/p99, warm+cold ===")
    sampler = Sampler()
    sampler.ensure_helper()

    s1 = sampler.sample()
    time.sleep(1)
    docker("ps")  # poke dockerd so utime moves
    s2 = sampler.sample()
    assert s2["utime_ticks"] + s2["stime_ticks"] >= s1["utime_ticks"] + s1["stime_ticks"], (
        "dockerd cpu ticks not monotonic"
    )
    sampler.clear_hwm()
    s3 = sampler.sample()
    assert s3["vm_hwm_kb"] <= s3["vm_rss_kb"] + 8192, (
        f"clear_refs did not reset VmHWM ({s3['vm_hwm_kb']} kB vs rss {s3['vm_rss_kb']} kB)"
    )
    print("helper OK: pid found, cpu monotonic, clear_hwm works")

    state = do_setup(args, sampler)
    rotated = state["configs"].get("rotated")
    if rotated:
        assert len(rotated["log_files"]) > 1, "rotation did not occur in smoke config"
        print(f"rotation OK: {len(rotated['log_files'])} files")

    json_path = do_run(args, sampler, state)
    session = json.loads(json_path.read_text())
    invalid = [r for r in session["runs"] if not r["valid"]]
    assert not invalid, f"{len(invalid)} invalid runs"
    cold_full = [
        r for r in session["runs"]
        if r["cache_mode"] == "cold" and r["scenario"] == "full"
    ]
    assert any(r["delta"]["read_bytes"] > 0 for r in cold_full), (
        "cold full read shows no block-layer reads - drop_caches ineffective?"
    )
    print("cold-cache read_bytes > 0: drop_caches effective")

    for config in args.configs:
        docker("rm", "-f", gen_container_name(config, "-smoke"), check=False)
    print("=== smoke test PASSED (helper container left running) ===")


# ---------------------------------------------------------------------------
# CLI


def add_config_opts(
    p: argparse.ArgumentParser, rotate_max_size: str = "20m", rotate_max_file: int = 16
) -> None:
    p.add_argument("--rotate-max-size", default=rotate_max_size)
    p.add_argument("--rotate-max-file", type=int, default=rotate_max_file)
    p.add_argument("--single-max-size", default="1g")
    p.add_argument("--local-max-size", default="50m")
    p.add_argument("--local-max-file", type=int, default=10)


def add_common(p: argparse.ArgumentParser, lines: int, out: str) -> None:
    p.add_argument("--lines", type=int, default=lines)
    p.add_argument("--line-bytes", type=int, default=200)
    p.add_argument("--configs", type=lambda s: s.split(","), default=["single", "rotated"])
    p.add_argument("--percentiles", type=lambda s: [int(x) for x in s.split(",")],
                   default=[50, 90, 99])
    p.add_argument("--out", default=out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_setup = sub.add_parser("setup", help="generate log containers and cutoff table")
    add_common(p_setup, 1_000_000, "results")
    add_config_opts(p_setup)

    p_run = sub.add_parser("run", help="run the benchmark matrix")
    add_common(p_run, 1_000_000, "results")
    add_config_opts(p_run)
    p_run.add_argument("--reps", type=int, default=5)
    p_run.add_argument("--cache", choices=["warm", "cold", "both"], default="both")
    p_run.add_argument("--force", action="store_true")
    p_run.add_argument("--baseline-pairs", type=int, default=5)
    p_run.add_argument("--baseline-interval", type=float, default=2.0)

    p_report = sub.add_parser("report", help="regenerate markdown from a session json")
    p_report.add_argument("session_json")

    sub.add_parser("cleanup", help="remove all benchmark containers")

    p_smoke = sub.add_parser("smoke", help="end-to-end test at small scale")
    add_common(p_smoke, 20_000, "results/smoke")
    p_smoke.set_defaults(percentiles=[50, 99])
    add_config_opts(p_smoke, rotate_max_size="1m", rotate_max_file=6)

    args = ap.parse_args()
    args.smoke_variant = args.cmd == "smoke"

    if args.cmd == "setup":
        sampler = Sampler()
        sampler.ensure_helper()
        do_setup(args, sampler)
    elif args.cmd == "run":
        sampler = Sampler()
        sampler.ensure_helper()
        state_path = state_file(args)
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if not state_matches(state, args):
            print("no matching setup state; running setup first")
            state = do_setup(args, sampler)
        do_run(args, sampler, state)
    elif args.cmd == "report":
        session = json.loads(Path(args.session_json).read_text())
        md_path = Path(args.session_json).with_suffix(".md")
        md_path.write_text(render_report(session))
        print(f"wrote {md_path}")
    elif args.cmd == "cleanup":
        ids = docker_out("ps", "-aq", "--filter", f"label={LABEL}").split()
        if ids:
            docker("rm", "-f", *ids)
        print(f"removed {len(ids)} container(s)")
    elif args.cmd == "smoke":
        args.reps = 1
        args.cache = "both"
        args.force = False
        args.baseline_pairs = 3
        args.baseline_interval = 1.0
        do_smoke(args)


if __name__ == "__main__":
    main()
