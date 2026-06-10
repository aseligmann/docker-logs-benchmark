# docker-log-benchmark

Measures the daemon-side cost of `docker logs` filtering with the json-file
and local logging drivers, comparing four scenarios on the same container
log:

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
  container can see dockerd)
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
`--percentiles 50,90,99`, `--configs single,rotated,local`, `--force`.

The matrix per config: `full`, plus `since` / `tail` / `tail_since` at each
`--since` cutoff percentile (T at 50%, 90%, 99% through the log). Configs:

- `single`: json-file, one file (forced via `max-size=1g max-file=1`
  because the daemon may default to rotation - OrbStack ships
  `max-size=20m max-file=5`)
- `rotated`: json-file, `max-size=20m max-file=16` - sized with headroom
  above the ~270 MB on-disk footprint of the 200 MB stream so no lines
  rotate away; the setup scan aborts if line 1 is missing
- `local`: the `local` driver with `max-size=50m max-file=10 compress=true`

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

## Results

All measurements 2026-06-10 on an Apple Silicon Mac, Docker 28.5.2 under
OrbStack (arm64 VM). Log: 1,000,000 lines x 200 B. Each configuration was
run with both warm and cold VM page cache, 5 reps each; tables show warm
medians with cold-cache block I/O in dedicated columns. All runs validated
(correct line counts, first/last sequence numbers, no gaps); idle noise
floor was zero.

### Configuration A: json-file driver, no compression

- driver `json-file` (the default), no compression
- ~270 MB on disk after json wrapping
- two layouts measured: `single` (`max-size=1g max-file=1`, one file) and
  `rotated` (`max-size=20m max-file=16`, 14 files used)

To replicate (setup runs automatically on first `run`; all other
parameters - 1M lines x 200 B, percentiles 50/90/99, 5 reps - are the
defaults; run the commands one at a time with nothing else using the
docker daemon):

```sh
uv run bench.py run --cache warm --configs single,rotated
uv run bench.py run --cache cold --configs single,rotated
```

Single-file layout (`rchar` = bytes dockerd read):

| scenario | lines out | rchar_MB | cold read_MB | cpu_s | wall_s |
|---|---|---|---|---|---|
| full | 1,000,000 | 269.9 | 270.0 | 3.22 | 2.05 |
| since@p50 | 500,000 | 269.9 | 270.0 | 2.48 | 1.86 |
| since@p90 | 100,000 | 269.9 | 270.0 | 1.81 | 1.53 |
| since@p99 | 10,000 | 269.9 | 270.0 | 1.65 | 1.48 |
| tail@p50 (N=550k) | 550,000 | 296.9 | 148.6 | 1.86 | 1.24 |
| tail@p90 (N=110k) | 110,001 | 59.4 | 31.3 | 0.38 | 0.27 |
| tail@p99 (N=11k) | 11,000 | 5.9 | 3.1 | 0.04 | 0.05 |
| tail_since@p50 | 500,000 | 296.9 | 148.6 | 1.79 | 1.21 |
| tail_since@p90 | 100,000 | 59.4 | 29.8 | 0.36 | 0.27 |
| tail_since@p99 | 10,000 | 5.9 | 3.1 | 0.04 | 0.05 |

The rotated layout (14 used files x 20 MB) was statistically identical to
the single-file layout in every cell, warm and cold.

#### Findings (json-file)

- **`--since` alone always scans the whole log.** dockerd read all 270 MB
  at every cutoff (p50/p90/p99 identical, confirmed cold via block-layer
  `read_bytes`). CPU drops from 3.2 s (full) to 1.65 s (since@p99) only
  because the filtered lines are not formatted and shipped; the read and
  json-decode of every line still happens.
- **`--tail` seeks from the end and reads only what it covers.** Cost
  scales with N, not with log size.
- **`--tail N --since T` costs the same as `--tail N` and, with N sized
  large enough, returns exactly the `--since T` output.** For the
  last-1%-of-the-log query this was ~40x cheaper in dockerd CPU (0.04 s vs
  1.65 s) and ~45x cheaper in I/O (5.9 MB vs 269.9 MB) than `--since`
  alone. This is the cheap way to ask for "recent logs since T".
- **`--tail` reads its window twice.** `rchar` is consistently ~2x the
  output volume (e.g. 59.4 MB read for 29.7 MB of lines): one backwards
  chunked scan to locate the line N-from-the-end, then a forward pass to
  stream. Cold `read_bytes` shows only ~1x because the forward pass hits
  the page cache the backwards scan just populated.
- **Log rotation is free for readers.** The reader treats the rotated set
  as one logical log: `--tail` walks backwards across file boundaries and
  full/`--since` reads concatenate oldest-first. 14 files x 20 MB matched
  a single 270 MB file in every metric.
- **Rotation deletes silently, and OrbStack rotates by default**
  (`max-size=20m max-file=5`, i.e. ~100 MB retained, even with no log
  opts set). Once a file is rotated away, no `docker logs` variant can
  return its lines, and there is no error: `--tail N` with N larger than
  what is retained, or `--since T` with T older than the oldest retained
  line, silently return only what is left. So "N large enough to cover
  everything since T" guarantees completeness only while
  `max-size x max-file` comfortably exceeds the bytes logged since T.
- **Memory never differentiates.** dockerd per-run peak RSS was flat at
  ~50 MB across all scenarios; reads are streamed with bounded buffers.

### Configuration B: local driver with compression

- driver `local` with `max-size=50m`, `max-file=10`, `compress=true`
  (i.e. the daemon.json equivalent of
  `"log-driver": "local", "log-opts": {"max-size": "50m", "max-file": "10",
  "compress": "true"}`)
- ~228 MB of log data stored as 4 gzip archives (~1.3 MB each, 50 MB
  uncompressed) plus a ~28 MB uncompressed active file = **33 MB on disk**
- `cold write_MB` = bytes dockerd wrote during the read; this is the
  archive-decompression-to-temp-files cost (see findings)

To replicate (the `local` config's driver and log-opts are built into the
harness; sizes can be overridden with `--local-max-size/--local-max-file`):

```sh
uv run bench.py run --cache warm --configs local
uv run bench.py run --cache cold --configs local
```

| scenario | lines out | rchar_MB | cold read_MB | cold write_MB | cpu_s | wall_s |
|---|---|---|---|---|---|---|
| full | 1,000,000 | 233.2 | 33.3 | 200.1 | 2.09 | 1.90 |
| since@p50 | 500,000 | 130.6 | 30.8 | 100.1 | 1.04 | 0.86 |
| since@p90 | 100,000 | 28.0 | 28.2 | 0.0 | 0.21 | 0.21 |
| since@p99 | 10,000 | 28.0 | 28.2 | 0.0 | 0.10 | 0.09 |
| tail@p50 (N=550k) | 550,000 | 132.4 | 30.7 | 100.1 | 1.31 | 1.14 |
| tail@p90 (N=110k) | 110,001 | 26.0 | 25.2 | 0.0 | 0.27 | 0.25 |
| tail@p99 (N=11k) | 11,000 | 2.6 | 2.7 | 0.0 | 0.02 | 0.04 |
| tail_since@p50 | 500,000 | 132.4 | 30.7 | 100.1 | 1.30 | 1.16 |
| tail_since@p90 | 100,000 | 26.0 | 25.2 | 0.0 | 0.24 | 0.22 |
| tail_since@p99 | 10,000 | 2.6 | 2.7 | 0.0 | 0.03 | 0.04 |

#### Findings (local driver)

- **`--since` is no longer a full scan: whole archives are skipped.** When
  the local driver compresses a rotated file, it records the file's last
  entry timestamp in the gzip header; on a `--since` read, dockerd skips
  any archive whose newest entry is older than T without touching its
  contents. Measured: p90/p99 cutoffs (which fall inside the active file)
  read only the 28 MB active file and decompressed nothing; the p50 cutoff
  decompressed exactly the 2 of 4 archives that overlap it (100 MB
  written, the older 2 untouched).
- **But within a needed file, `--since` still scans from the file start.**
  since@p99 needs only the last 10k lines yet reads the whole 28 MB active
  file. `tail+since` remains the cheap form: 2.6 MB read and 0.03 s CPU vs
  28 MB and 0.10 s - a smaller gap than json-file's 45x, but still ~10x
  I/O and ~3x CPU. The gap grows with `max-size` (a bigger active file
  makes plain `--since` proportionally worse).
- **Reading into compressed archives makes dockerd write to disk.** Each
  needed archive is decompressed to a temp file under the container's log
  directory before being read: a full read writes 200 MB, a p50 read
  writes 100 MB - on every such `docker logs` invocation, warm or cold
  (decompressed data is not reused across reads). Operationally this means
  transient disk usage up to the uncompressed size of the archives being
  read, and read commands that generate write I/O.
- **`--tail` does not double-read on the local driver.** Entries are
  length-prefixed and length-suffixed binary frames, so the reader walks
  backwards frame by frame and streams from where it lands: tail@p99 read
  2.6 MB for 2.5 MB of output (1x), where json-file read 2x its output.
  Like json-file, the backwards walk crosses rotated-file boundaries
  transparently (decompressing archives as it descends into them).
- **Decoding is ~35% cheaper than json-file.** Full read: 2.09 s vs
  3.22 s CPU for the same 1M lines (binary frames vs per-line JSON).
- **No correctness differences appeared.** All 100 runs validated: same
  line counts, boundaries, and gap-free output as json-file; `--since` is
  still inclusive; `docker logs -t` still emits fixed-width
  RFC3339NanoFixed timestamps that round-trip as `--since` values. The
  only harness change the driver required was teaching the helper where
  the local driver keeps its files (`local-logs/container.log*` instead of
  `<id>-json.log*`).
- The retention caveat from json-file applies unchanged: rotation deletes
  the oldest archive silently, and no `docker logs` variant errors when
  lines are gone. With compression the same `max-size x max-file` budget
  retains far more history (here ~7x), which shrinks that risk for equal
  disk spend.

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
