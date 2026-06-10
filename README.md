# docker-log-benchmark

Measures the daemon-side cost of `docker logs` filtering with the json-file
logging driver, comparing four scenarios on the same container log:

1. `docker logs` (full output)
2. `docker logs --since T`
3. `docker logs --tail N`
4. `docker logs --tail N --since T`

N is sized per cutoff so that scenario 4 returns every line since T
(N = max(1.1 * lines_after_T, lines_after_T + 1000)), which the harness
verifies on every run by checking line counts and embedded sequence numbers.
The hypothesis: `--since` alone scans the log file(s) from the beginning,
while `--tail` seeks from the end, so tail+since is much cheaper for
"recent logs" queries.

## Requirements

- macOS with OrbStack (or any Docker where a privileged `--pid=host`
  container can see dockerd), json-file logging driver
- `uv` (the script is a self-contained PEP 723 script, stdlib only)

## Usage

```sh
uv run bench.py smoke     # end-to-end self-test at 20k lines (~1 min)
uv run bench.py setup     # generate 1M-line log containers + cutoff table
uv run bench.py run       # full matrix (auto-runs setup if needed)
uv run bench.py report results/session-<ts>.json   # regenerate markdown
uv run bench.py cleanup   # remove all benchmark containers (label dlb=1)
```

Key `run` flags: `--reps 5`, `--cache warm|cold|both`,
`--percentiles 50,90,99`, `--configs single,rotated`, `--force`.

The matrix per rotation config: `full`, plus `since` / `tail` / `tail_since`
at each `--since` cutoff percentile (T at 50%, 90%, 99% through the log).
Two configs: `single` (one file, forced via `max-size=1g max-file=1` because
the daemon may default to rotation - OrbStack ships `max-size=20m max-file=5`)
and `rotated`
(`max-size=20m`, `max-file=16` - sized with headroom above the ~270 MB
on-disk footprint of the 200 MB stream so no lines rotate away; the setup
scan aborts if line 1 is missing).

Results land in `results/session-<ts>.json` (raw samples) and a rendered
`.md` summary with median (min-max) per cell.

## How it measures

dockerd runs inside the OrbStack VM, so a long-lived privileged `--pid=host`
helper container (`dlb-helper`, `python:3.12-alpine`) serves a tiny HTTP API
on `127.0.0.1:8377`. The harness samples it before/after each `docker logs`
invocation. Sampling goes over a published port rather than `docker exec`
because each exec round-trips through dockerd and would pollute the counters
being measured.

Per-run metrics (deltas on the dockerd process):

- `cpu_s` - utime+stime from `/proc/<pid>/stat`
- `rchar` - logical bytes read (`/proc/<pid>/io`); the primary metric for
  "how much of the log file did dockerd read", valid warm or cold
- `read_bytes` - block-layer reads; only meaningful in cold mode (the
  harness syncs and drops the VM page cache before each cold run)
- `peak_rss` - `VmHWM` after a pre-run reset via `/proc/<pid>/clear_refs`
  (before/after RSS deltas are meaningless for a Go daemon)
- `wall_s` - end-to-end client time

An idle-noise baseline (sample pairs with no docker activity) is recorded at
session start and reported as the noise floor.

## Caveats

- Wall-clock time includes OrbStack's unix-socket forwarding of the log
  stream to the Mac; treat it as a secondary, end-to-end metric. Daemon
  `cpu_s` and `rchar` are the primary metrics.
- "Cold" drops the VM page cache, but macOS may still cache the VM disk
  image host-side, so cold reads can be faster than true disk-cold.
  `read_bytes` attribution to dockerd is unaffected.
- Timestamps are handled as opaque RFC3339NanoFixed strings throughout
  (fixed-width, so lexicographic order == chronological order). `--since`
  is inclusive, and burst-written lines share nanosecond timestamps, so
  cutoff expectations account for timestamp ties.
- Measured runs never use `--timestamps`; only the one setup scan does.
