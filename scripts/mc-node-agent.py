#!/usr/bin/env python3
"""mc-node-agent — self-registering push-telemetry agent for Mission Control
(Fleet & Rezepte v2, Phase 1 — docs/plans/2026-08-30-node-agent-telemetry-phase1.md).

ONE FILE, STANDARD LIBRARY ONLY, Python >= 3.9. Design constraints (Mark,
30.08.2026 — "der Agent darf das servierende Modell NIE crashen"):

- No third-party dependencies — nothing to `pip install` on a box that is
  busy serving an LLM. Only stdlib, and a deliberately narrow slice of it —
  see the "Speicher-Diät" note below.
- Outbound-only HTTP(S) poll, no listening socket — this agent is never
  something to attack from the network side.
- `--install` wraps the process in a systemd straightjacket (MemoryHigh/
  MemoryMax, CPUQuota=10%, Nice=10, IOSchedulingClass=idle) so a runaway
  heartbeat loop can only ever starve itself, never the GPU workload
  sharing the box.
- Every collection step and every network call is wrapped in try/except — a
  stack trace killing the background process is worse than one missed
  heartbeat. Failures go to stderr (the systemd journal), backoff
  exponentially (15s -> 120s cap), and the loop NEVER exits on its own once
  it has started.

Speicher-Diät (Mark, 30.08.2026 — GB10 has UNIFIED memory: every MB this
agent holds is a MB the LLM's context window doesn't get; measured live on
GX10 hardware, not estimated):

    Peak RSS (VmHWM) before: 26.7 MB  ->  after: see git log for the exact
    before/after ru_maxrss/VmHWM measurement this round's commits carry.

- No `urllib.request`/`http.client` (+9.1 MB) — see `_http_post_json` below,
  a raw-socket HTTP/1.1 client. `ssl` is imported lazily, only inside the
  https:// branch, so an http:// deployment never pays for it.
- No `hashlib` (+3.8 MB) — `inventory_hash()` compares canonical
  `json.dumps(..., sort_keys=True)` strings directly instead of hashing
  them; two of these are only ever compared for equality, never stored or
  transmitted, so string equality does the job with zero collision risk.
- No `logging` (+1.6 MB) — see the tiny `_Log`/`log` below; same call
  shape (`log.info(...)`, `log.warning(...)`, `log.error(...)`), writes to
  stderr, which systemd's journal captures exactly like it would logging's.
- No `argparse` (+1.1 MB) — see `parse_args()`, a ~40-line manual
  `sys.argv` parser for the 4 flags this agent actually has.
- `nvidia-smi` is polled only every GPU_POLL_EVERY_N_HEARTBEATS-th
  heartbeat (~1 min at the 15s interval), not every single one — see
  run_loop(). On GB10 (unified memory) nvidia-smi never reports
  memory.used/memory.total anyway (those come from /proc/meminfo above);
  it only supplies util/temp, which can update a little less often.

Usage — first run, mint a pairing code via
POST /api/v1/nodes/pairing-codes (admin, from the MC UI/API), then on the
target box:

    sudo python3 mc-node-agent.py --mc-url https://mc.tailnet-name.ts.net \\
        --pair ABCD1234 --install

This trades the code for a token (saved to /etc/mc-node-agent/token,
chmod 600, chowned to the invoking user), writes + enables the systemd
service, and from then on the service runs the heartbeat loop unattended.

Manual run (token already paired, e.g. for testing without installing a
service):

    MC_NODE_TOKEN=... python3 mc-node-agent.py --mc-url https://mc.tailnet-name.ts.net

`--install` needs sudo/root (it writes a unit under /etc/systemd/system and
calls systemctl) — running it as a normal user prints an error and exits
instead of silently doing nothing.
"""
from __future__ import annotations

import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AGENT_VERSION = "0.1.0"
HEARTBEAT_INTERVAL_S = 15
MAX_BACKOFF_S = 120
HTTP_TIMEOUT_S = 10
NVIDIA_SMI_TIMEOUT_S = 3
CPU_SAMPLE_GAP_S = 0.2
# Nachtrag 30.08.2026: model-inventory scan cadence — every 40th heartbeat
# at the 15s interval is ~10 minutes, plus once unconditionally at startup.
INVENTORY_SCAN_EVERY_N_HEARTBEATS = 40
# Speicher-Diät 30.08.2026, Punkt 5: nvidia-smi is a fork+exec on every call
# and (on GB10) never reports memory anyway — repoll only every 4th
# heartbeat (~1 min at the 15s interval), reuse the cached reading between.
GPU_POLL_EVERY_N_HEARTBEATS = 4

SYSTEM_TOKEN_PATH = Path("/etc/mc-node-agent/token")
USER_TOKEN_PATH = Path.home() / ".config" / "mc-node-agent" / "token"
SYSTEMD_UNIT_PATH = Path("/etc/systemd/system/mc-node-agent.service")


# ── Logging (stderr only — no `logging` module; systemd's journal captures
#    a service's stderr exactly the same way it would logging's output, see
#    Speicher-Diät note above) ────────────────────────────────────────────


class _Log:
    """Drop-in replacement for a `logging.Logger` restricted to the three
    levels this file actually uses — same call shape (`log.info("x=%s", x)`)
    so every existing call site needed zero changes."""

    @staticmethod
    def _emit(level: str, msg: str, *args: object) -> None:
        if args:
            msg = msg % args
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{ts} mc-node-agent {level} {msg}", file=sys.stderr, flush=True)

    def info(self, msg: str, *args: object) -> None:
        self._emit("INFO", msg, *args)

    def warning(self, msg: str, *args: object) -> None:
        self._emit("WARNING", msg, *args)

    def error(self, msg: str, *args: object) -> None:
        self._emit("ERROR", msg, *args)


log = _Log()


# ── Pure parsers — no network/subprocess side effects, unit-tested directly
#    (see backend/tests/test_node_agent_parsers.py) ──────────────────────────


def _parse_cpu_line(line: str) -> list[int] | None:
    """Parses one '/proc/stat' cpu-aggregate line into its jiffy counters."""
    parts = line.split()
    if len(parts) < 2 or parts[0] != "cpu":
        return None
    try:
        return [int(x) for x in parts[1:]]
    except ValueError:
        return None


def read_proc_stat_cpu_pct(sample_a: str, sample_b: str) -> float | None:
    """CPU% from two raw '/proc/stat' first-line samples taken apart in time.

    Standard jiffy-delta formula: busy% = 1 - idle_delta/total_delta, where
    idle = idle + iowait (iowait is "idle, waiting on disk" — still not CPU
    work). Returns None for any malformed/too-short sample rather than
    raising, so one glitchy read never kills the heartbeat.
    """
    a = _parse_cpu_line(sample_a.strip())
    b = _parse_cpu_line(sample_b.strip())
    if not a or not b or len(a) < 4 or len(b) < 4:
        return None
    idle_a = a[3] + (a[4] if len(a) > 4 else 0)
    idle_b = b[3] + (b[4] if len(b) > 4 else 0)
    total_delta = sum(b) - sum(a)
    idle_delta = idle_b - idle_a
    if total_delta <= 0:
        return None
    return round((1 - idle_delta / total_delta) * 100, 1)


def read_meminfo(text: str) -> dict:
    """Parses '/proc/meminfo' text into the memory subset of the telemetry
    schema. Missing/malformed lines simply leave the field None — a box
    without swap, for instance, has no SwapTotal/SwapFree lines at all."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        rest = rest.strip()
        if rest.endswith("kB"):
            try:
                values[key.strip()] = int(rest[:-2].strip())
            except ValueError:
                continue

    result: dict[str, int | None] = {
        "mem_total_mb": None,
        "mem_used_mb": None,
        "mem_available_mb": None,
        "swap_used_mb": None,
    }
    mem_total_kb = values.get("MemTotal")
    mem_available_kb = values.get("MemAvailable")
    if mem_total_kb is not None:
        result["mem_total_mb"] = round(mem_total_kb / 1024)
        if mem_available_kb is not None:
            result["mem_used_mb"] = round((mem_total_kb - mem_available_kb) / 1024)
            result["mem_available_mb"] = round(mem_available_kb / 1024)

    swap_total_kb = values.get("SwapTotal")
    swap_free_kb = values.get("SwapFree")
    if swap_total_kb is not None and swap_free_kb is not None:
        result["swap_used_mb"] = round((swap_total_kb - swap_free_kb) / 1024)
    return result


def read_loadavg(text: str) -> float | None:
    """Parses '/proc/loadavg' text -> load1 (the first of the three fields)."""
    try:
        return float(text.strip().split()[0])
    except (ValueError, IndexError):
        return None


def _int_or_none(raw: str) -> int | None:
    """nvidia-smi prints '[N/A]' for fields a unified-memory device (GB10)
    doesn't have — same tolerance as the backend's _parse_spark_metrics."""
    s = raw.strip().strip("[]")
    if not s or s.upper() == "N/A":
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def parse_nvidia_smi(stdout: str) -> dict:
    """Parses `nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,
    temperature.gpu --format=csv,noheader,nounits` output.

    Multi-GPU boxes print one line per GPU — Phase 1 only reports the first
    (single-accelerator scope; a later phase can sum/list them). Empty input
    (binary missing, or the caller couldn't run it) -> all fields None.
    """
    result: dict[str, int | None] = {
        "gpu_util_pct": None,
        "vram_used_mb": None,
        "vram_total_mb": None,
        "gpu_temp_c": None,
    }
    if not stdout or not stdout.strip():
        return result
    first_line = stdout.strip().splitlines()[0]
    parts = [p.strip() for p in first_line.split(",")]
    if len(parts) < 4:
        return result
    result["gpu_util_pct"] = _int_or_none(parts[0])
    result["vram_used_mb"] = _int_or_none(parts[1])
    result["vram_total_mb"] = _int_or_none(parts[2])
    result["gpu_temp_c"] = _int_or_none(parts[3])
    return result


# ── Model-weights inventory (Nachtrag 30.08.2026 — Phase 2's "already on the
#    box, skip the download" check) ──────────────────────────────────────────
#
# scan_model_inventory() and everything it calls only ever reads directory
# metadata (os.scandir/stat) and small config.json files — never a model's
# weight bytes. Every per-directory step tolerates its own failure (missing
# path, permission denied, broken symlink) so one bad mount can't take the
# whole scan down.


def _decode_hf_repo_id(dirname: str) -> str | None:
    """'models--Org--Name' -> 'Org/Name' (huggingface_hub's cache-dir naming:
    the repo id's single '/' becomes '--'). None if it doesn't match."""
    prefix = "models--"
    if not dirname.startswith(prefix):
        return None
    rest = dirname[len(prefix):]
    org, sep, name = rest.partition("--")
    if not sep or not org or not name:
        return None
    return f"{org}/{name}"


def _walk_dir_stats(root: Path, follow_file_symlinks: bool) -> tuple[int, int, float | None]:
    """Sums file sizes + finds the newest mtime under `root`.

    Dedupes by real path when following file symlinks — the HF cache's
    snapshots/ directories are almost entirely symlinks into a shared
    blobs/ store, and two revisions pointing at the same blob must only
    count its bytes once. Any unreadable entry (permission error, dangling
    symlink) is skipped, not fatal.

    Iterative (explicit stack), not recursive — Speicher-Diät round, review
    finding #7 (30.08.2026): this eliminates the whole RecursionError
    failure class outright, rather than just guarding against it. Directory
    symlinks are still never followed (`is_dir(follow_symlinks=False)`,
    unconditionally) — that alone already prevented a symlink CYCLE from
    ever recursing (RecursionError isn't an OSError, so the try/except
    below couldn't have caught it anyway), but an explicit stack has no
    depth limit at all, so even a pathologically deep real tree can't
    exhaust it either. Only file symlinks are followed (needed for the HF
    cache's snapshots/ -> blobs/ layout); a symlinked directory is simply
    skipped rather than descended into.
    """
    seen: set[str] = set()
    total_bytes = 0
    file_count = 0
    mtime_max: float | None = None

    stack: list[str] = [str(root)]
    while stack:
        path = stack.pop()
        try:
            entries = list(os.scandir(path))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=follow_file_symlinks):
                    key = os.path.realpath(entry.path) if follow_file_symlinks else entry.path
                    if key in seen:
                        continue
                    seen.add(key)
                    st = entry.stat(follow_symlinks=follow_file_symlinks)
                    total_bytes += st.st_size
                    file_count += 1
                    if mtime_max is None or st.st_mtime > mtime_max:
                        mtime_max = st.st_mtime
            except OSError:
                continue

    return total_bytes, file_count, mtime_max


def _read_model_type(config_path: Path) -> str | None:
    try:
        with open(config_path, encoding="utf-8") as f:
            return json.load(f).get("model_type")
    except (OSError, ValueError):
        return None


def _scan_local_model_dir(path: Path) -> dict:
    """models-local style: the whole subtree is one model; config.json (if
    present) directly under it names the model_type."""
    total_bytes, file_count, mtime_max = _walk_dir_stats(path, follow_file_symlinks=True)
    config_path = path / "config.json"
    model_type = _read_model_type(config_path) if config_path.is_file() else None
    return {
        "name": path.name,
        "total_bytes": total_bytes,
        "file_count": file_count,
        "mtime_max": mtime_max,
        "hf_repo_id": None,
        "model_type": model_type,
    }


def _scan_hf_cache_dir(path: Path) -> dict:
    """HF-cache style ('models--Org--Name'): size is computed over snapshots/
    (the blobs/ store is shared + deduped there via _walk_dir_stats), repo id
    decoded from the directory name, model_type from whichever snapshot
    revision has a config.json (usually just one — 'main')."""
    snapshots_dir = path / "snapshots"
    total_bytes, file_count, mtime_max = _walk_dir_stats(snapshots_dir, follow_file_symlinks=True)
    model_type = None
    if snapshots_dir.is_dir():
        try:
            for rev_dir in snapshots_dir.iterdir():
                config_path = rev_dir / "config.json"
                if config_path.is_file():
                    model_type = _read_model_type(config_path)
                    if model_type is not None:
                        break
        except OSError:
            pass
    return {
        "name": path.name,
        "total_bytes": total_bytes,
        "file_count": file_count,
        "mtime_max": mtime_max,
        "hf_repo_id": _decode_hf_repo_id(path.name),
        "model_type": model_type,
    }


def scan_model_inventory(paths: list) -> list[dict]:
    """Inventories model-weight directories under each root in `paths`
    (production: ~/models-local and ~/.cache/huggingface/hub). Each
    immediate subdirectory is classified by name: 'models--…' is HF-cache
    style, everything else is models-local style. A root that doesn't exist
    or isn't readable is skipped rather than failing the whole scan — most
    boxes will only ever have one of the two.

    Returns entries sorted by name (deterministic — inventory_hash() below
    depends on it).
    """
    entries: list[dict] = []
    for root in paths:
        root = Path(root)
        try:
            children = list(root.iterdir())
        except OSError as e:
            log.info("Inventar-Scan: %s nicht lesbar (%s) — übersprungen", root, e)
            continue
        for child in children:
            try:
                if not child.is_dir():
                    continue
                if child.name.startswith("models--"):
                    entries.append(_scan_hf_cache_dir(child))
                else:
                    entries.append(_scan_local_model_dir(child))
            except OSError as e:
                log.warning("Inventar-Scan: %s übersprungen (%s)", child, e)
                continue
    entries.sort(key=lambda e: e["name"])
    return entries


def inventory_hash(entries: list[dict]) -> str:
    """Canonical fingerprint of a scan result — the agent compares this
    against the fingerprint it last SENT (kept in memory, see run_loop) to
    decide whether this scan cycle's inventory is worth attaching to the
    heartbeat.

    Despite the name (kept for compatibility with existing call sites/
    tests), this is the canonical JSON string itself, not a hash —
    Speicher-Diät round, review point #2 (30.08.2026): hashlib costs
    +3.8 MB RSS on GB10 for zero benefit here, since two of these are only
    ever compared for equality, never stored, transmitted or indexed.
    String equality does that exactly as well as hash equality, with no
    collision risk at all.
    """
    return json.dumps(entries, sort_keys=True)


# ── Collection (subprocess/filesystem side effects — kept thin around the
#    pure parsers above) ─────────────────────────────────────────────────────


def _run_nvidia_smi() -> str:
    """Returns nvidia-smi's stdout, or '' on any failure (missing binary,
    timeout, non-zero exit) — parse_nvidia_smi treats '' as "no GPU data"."""
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=NVIDIA_SMI_TIMEOUT_S,
        )
        return proc.stdout if proc.returncode == 0 else ""
    except FileNotFoundError:
        return ""  # no NVIDIA GPU on this box — not an error
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("nvidia-smi fehlgeschlagen: %s", e)
        return ""


def collect_telemetry(gpu_fields: dict | None = None) -> dict:
    """One telemetry snapshot — every source is independently guarded, so a
    single failing collector (e.g. no /proc on a non-Linux test box) still
    lets the rest of the heartbeat go out.

    `gpu_fields`, if given, is used verbatim instead of polling nvidia-smi
    fresh — see run_loop's GPU_POLL_EVERY_N_HEARTBEATS gating (Speicher-Diät
    round, point #5, 30.08.2026): nvidia-smi is a fork+exec on every call,
    and on GB10 (unified memory) it never reports memory.used/total anyway
    (those come from /proc/meminfo above) — polling it every 15s bought
    nothing but four forks a minute. Passing None (the default) polls fresh,
    which keeps this function's own behaviour/tests unchanged.
    """
    telemetry: dict = {"ts": datetime.now(timezone.utc).isoformat()}

    try:
        with open("/proc/stat", encoding="utf-8") as f:
            sample_a = f.readline()
        time.sleep(CPU_SAMPLE_GAP_S)
        with open("/proc/stat", encoding="utf-8") as f:
            sample_b = f.readline()
        telemetry["cpu_pct"] = read_proc_stat_cpu_pct(sample_a, sample_b)
    except OSError as e:
        log.warning("CPU-Messung fehlgeschlagen: %s", e)
        telemetry["cpu_pct"] = None

    try:
        with open("/proc/loadavg", encoding="utf-8") as f:
            telemetry["load1"] = read_loadavg(f.read())
    except OSError:
        telemetry["load1"] = None

    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            telemetry.update(read_meminfo(f.read()))
    except OSError:
        telemetry.update(
            {"mem_total_mb": None, "mem_used_mb": None, "mem_available_mb": None, "swap_used_mb": None}
        )

    try:
        usage = shutil.disk_usage("/")
        telemetry["disk_used_gb"] = round((usage.total - usage.free) / 1024**3, 1)
        telemetry["disk_total_gb"] = round(usage.total / 1024**3, 1)
    except OSError:
        telemetry["disk_used_gb"] = None
        telemetry["disk_total_gb"] = None

    telemetry.update(gpu_fields if gpu_fields is not None else parse_nvidia_smi(_run_nvidia_smi()))
    return telemetry


# ── HTTP (raw socket — no urllib.request/http.client; see Speicher-Diät note
#    at the top of this file) ─────────────────────────────────────────────────


class _HTTPError(Exception):
    """A non-2xx HTTP response. `.status` + `.body` — deliberately not
    trying to mimic urllib.error.HTTPError's `.code`/`.read()` shape, since
    both the raise site and the only two catch sites live in this same file."""

    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}")


class _SocketReader:
    """Thin buffered reader over a raw socket — socket.makefile() would work
    too, but drags in io.BufferedReader/io.TextIOWrapper machinery this file
    doesn't otherwise need for parsing a status line, a few headers, and a
    JSON body."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = b""

    def read_until(self, delimiter: bytes, limit: int = 65536) -> bytes:
        while delimiter not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError(
                    "Verbindung geschlossen, bevor die Antwort vollständig war"
                )
            self._buf += chunk
            if len(self._buf) > limit:
                raise ValueError("HTTP-Antwort zu gross (Header/Zeilen-Limit überschritten)")
        before, _, after = self._buf.partition(delimiter)
        self._buf = after
        return before

    def read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise ConnectionError(
                    "Verbindung geschlossen, bevor der Antwort-Body vollständig war"
                )
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data


class _HTTPResponse:
    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int, headers: dict, body: bytes):
        self.status = status
        self.headers = headers
        self.body = body


def _split_url(url: str) -> tuple[str, str, int, str]:
    """Minimal http(s)://host[:port][/path] splitter — no urllib.parse (kept
    out of this file's own import list on purpose; see Speicher-Diät note)."""
    scheme, sep, rest = url.partition("://")
    if not sep or scheme not in ("http", "https"):
        raise ValueError(f"Nicht unterstützte oder fehlende URL-Schema (erwarte http(s)://...): {url!r}")
    host_port, _, path = rest.partition("/")
    path = "/" + path
    if not host_port:
        raise ValueError(f"URL ohne Host: {url!r}")
    if ":" in host_port:
        host, _, port_s = host_port.partition(":")
        try:
            port = int(port_s)
        except ValueError:
            raise ValueError(f"Ungültiger Port in URL: {url!r}") from None
    else:
        host = host_port
        port = 443 if scheme == "https" else 80
    return scheme, host, port, path


def _read_chunked_body(reader: _SocketReader) -> bytes:
    """Reads a `Transfer-Encoding: chunked` body per RFC 7230 §4.1 — each
    chunk is `<hex-size>\\r\\n<data>\\r\\n`, terminated by a zero-size chunk
    followed by zero or more trailer header lines and a final blank line."""
    parts: list[bytes] = []
    while True:
        size_line = reader.read_until(b"\r\n")
        size_str = size_line.split(b";", 1)[0].strip()
        try:
            size = int(size_str, 16)
        except ValueError:
            raise ValueError(f"Ungültige chunked-Grösse in Antwort: {size_line!r}") from None
        if size == 0:
            while reader.read_until(b"\r\n"):
                pass  # drain optional trailer headers up to the final blank line
            break
        parts.append(reader.read_exact(size))
        reader.read_until(b"\r\n")  # each chunk's data is followed by a bare CRLF
    return b"".join(parts)


def _read_http_response(sock: socket.socket, timeout: float) -> _HTTPResponse:
    sock.settimeout(timeout)
    reader = _SocketReader(sock)
    header_blob = reader.read_until(b"\r\n\r\n")
    lines = header_blob.split(b"\r\n")
    status_line = lines[0].decode("ascii", errors="replace")
    parts = status_line.split(" ", 2)
    if len(parts) < 2:
        raise ValueError(f"Ungültige HTTP-Statuszeile: {status_line!r}")
    try:
        status = int(parts[1])
    except ValueError:
        raise ValueError(f"Ungültiger Statuscode in Antwort: {status_line!r}") from None

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.decode("ascii", errors="replace").partition(":")
        if name:
            headers[name.strip().lower()] = value.strip()

    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = _read_chunked_body(reader)
    else:
        content_length = int(headers.get("content-length") or 0)
        body = reader.read_exact(content_length)

    return _HTTPResponse(status, headers, body)


def _http_post_json(url: str, payload: dict, token: str | None, timeout: float) -> dict:
    """HTTP/1.1 POST over a raw socket — see the Speicher-Diät note at the
    top of this file for why (`urllib.request` alone costs +9.1 MB RSS on
    GB10 via its http.client/email/ssl import chain).

    Deliberately NOT a general HTTP client: no redirects are followed — a
    3xx here means MC's own URL is misconfigured, so it is surfaced as an
    _HTTPError like any other non-2xx status, exactly like every 4xx/5xx.
    The request body is always sent in one shot with Content-Length (we
    build it ourselves, it's a small JSON object); the RESPONSE body may
    legitimately be chunked or Content-Length-delimited — that's the
    server's choice, both are handled in _read_http_response.
    """
    scheme, host, port, path = _split_url(url)
    body = json.dumps(payload).encode("utf-8")
    header_lines = [
        f"POST {path} HTTP/1.1",
        f"Host: {host}",
        "Content-Type: application/json",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    if token:
        header_lines.append(f"Authorization: Bearer {token}")
    request = ("\r\n".join(header_lines) + "\r\n\r\n").encode("ascii") + body

    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        if scheme == "https":
            import ssl  # lazy — only paid for by an https:// deployment (~4 MB)

            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.settimeout(timeout)
        sock.sendall(request)
        response = _read_http_response(sock, timeout)
    finally:
        sock.close()

    if response.status >= 300:
        raise _HTTPError(response.status, response.body)
    return json.loads(response.body.decode("utf-8")) if response.body else {}


def pair(mc_url: str, code: str) -> str:
    """Trades a pairing code for a node_token via POST /api/v1/nodes/pair
    (unauthenticated — the code itself is the credential)."""
    uname = os.uname()
    payload = {
        "code": code,
        "hostname": socket.gethostname(),
        "os": uname.sysname.lower(),
        "arch": uname.machine,
        "agent_version": AGENT_VERSION,
    }
    try:
        result = _http_post_json(f"{mc_url}/api/v1/nodes/pair", payload, token=None, timeout=HTTP_TIMEOUT_S)
    except _HTTPError as e:
        detail = e.body.decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.status} von {mc_url}/api/v1/nodes/pair: {detail}") from e
    except OSError as e:
        raise RuntimeError(f"{mc_url} nicht erreichbar: {e}") from e
    token = result.get("node_token")
    if not token:
        raise RuntimeError(f"Antwort ohne node_token: {result}")
    return token


def send_heartbeat(mc_url: str, token: str, telemetry: dict, inventory: list[dict] | None = None) -> dict:
    payload = {"telemetry": telemetry, "agent_version": AGENT_VERSION}
    if inventory is not None:
        payload["inventory"] = inventory
    return _http_post_json(f"{mc_url}/api/v1/nodes/heartbeat", payload, token=token, timeout=HTTP_TIMEOUT_S)


def default_inventory_paths() -> list[Path]:
    home = Path.home()
    return [home / "models-local", home / ".cache" / "huggingface" / "hub"]


# ── Token storage ─────────────────────────────────────────────────────────────


def _token_path() -> Path:
    return SYSTEM_TOKEN_PATH if os.geteuid() == 0 else USER_TOKEN_PATH


def save_token(token: str) -> Path:
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(token + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def load_token() -> str | None:
    env_token = os.environ.get("MC_NODE_TOKEN")
    if env_token:
        return env_token.strip()
    for path in (SYSTEM_TOKEN_PATH, USER_TOKEN_PATH):
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return None


def _chown_to_user(path: Path, user: str) -> None:
    """Best-effort chown so a non-root systemd User= can still read a token
    that --install (running as root via sudo) just wrote."""
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        log.warning("Unbekannter User '%s' — Token-Datei bleibt root-eigen (systemd-Dienst braucht ggf. User=root).", user)
        return
    try:
        os.chown(path, pw.pw_uid, pw.pw_gid)
    except OSError as e:
        log.warning("chown auf %s fehlgeschlagen: %s", path, e)


# ── systemd install ───────────────────────────────────────────────────────────

UNIT_TEMPLATE = """[Unit]
Description=Mission Control Node Agent (push telemetry, Fleet & Rezepte v2 Phase 1)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Environment=MALLOC_ARENA_MAX=1
ExecStart={python} -S -E -I {script} --mc-url {mc_url}
Restart=always
RestartSec=30

# Straightjacket (Mark, 30.08.2026, verschärft in der Speicher-Diät-Runde
# vom selben Tag): this agent must never be able to starve the model-serving
# process sharing this box. A runaway loop can only ever hurt itself.
# MemoryHigh is the SOFT limit — it makes the kernel actively reclaim/trim
# this process's memory under pressure, which is exactly what we want long
# before MemoryMax (the HARD limit) would ever have to OOM-kill it.
Nice=10
IOSchedulingClass=idle
MemoryHigh=16M
MemoryMax=32M
TasksMax=8
CPUQuota=10%
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
"""


def _default_install_user() -> str:
    """The account that ran `sudo ... --install` — SUDO_USER when invoked
    via sudo (the common case), the running uid's own name otherwise."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        return sudo_user
    return pwd.getpwuid(os.getuid()).pw_name


def _user_token_path_for(user: str) -> Path:
    """USER_TOKEN_PATH is computed from Path.home() at import time — under
    sudo that's root's home, not the invoking user's. This resolves the
    SAME ~/.config/mc-node-agent/token layout for an arbitrary user, so
    --install can find a token a plain (non-sudo) `--pair` run saved
    earlier under the operator's own home (review finding #9, 30.08.2026)."""
    try:
        home = Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        return USER_TOKEN_PATH
    return home / ".config" / "mc-node-agent" / "token"


def install_systemd_unit(mc_url: str, run_as_user: str) -> None:
    if os.geteuid() != 0:
        log.error(
            "--install braucht root (schreibt %s + ruft systemctl auf). "
            "Bitte mit sudo erneut ausführen.",
            SYSTEMD_UNIT_PATH,
        )
        raise SystemExit(1)

    if not SYSTEM_TOKEN_PATH.exists():
        # A common two-step flow: `python3 mc-node-agent.py --pair CODE`
        # WITHOUT sudo first (saves to the user's own home), THEN
        # `sudo python3 mc-node-agent.py --install` to set up the service.
        # Without this, --install would only ever look under /etc and fail
        # even though a perfectly good token already exists.
        legacy_path = _user_token_path_for(run_as_user)
        if legacy_path.exists():
            log.info("Übernehme Token aus %s nach %s.", legacy_path, SYSTEM_TOKEN_PATH)
            SYSTEM_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            SYSTEM_TOKEN_PATH.write_text(legacy_path.read_text(encoding="utf-8"), encoding="utf-8")
            os.chmod(SYSTEM_TOKEN_PATH, 0o600)
        else:
            log.error(
                "Kein Token unter %s oder %s — erst --pair CODE ausführen (oder zusammen mit --install).",
                SYSTEM_TOKEN_PATH, legacy_path,
            )
            raise SystemExit(1)

    _chown_to_user(SYSTEM_TOKEN_PATH, run_as_user)
    _chown_to_user(SYSTEM_TOKEN_PATH.parent, run_as_user)

    script_path = Path(__file__).resolve()
    unit = UNIT_TEMPLATE.format(
        user=run_as_user,
        python=sys.executable,
        script=script_path,
        mc_url=mc_url,
    )
    SYSTEMD_UNIT_PATH.write_text(unit, encoding="utf-8")
    os.chmod(SYSTEMD_UNIT_PATH, 0o644)
    log.info("systemd-Unit geschrieben: %s", SYSTEMD_UNIT_PATH)

    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "mc-node-agent.service"], check=True)
    log.info("mc-node-agent.service aktiviert + gestartet (User=%s).", run_as_user)


# ── Heartbeat loop ────────────────────────────────────────────────────────────


def run_loop(mc_url: str, token: str) -> None:
    """Runs forever. Every exception is caught here — a missed heartbeat is
    always preferable to a dead process (systemd's Restart=always is only a
    second line of defense, not the plan).

    Model-inventory scan (Nachtrag 30.08.2026, hardened per review finding
    #7): runs at startup and then every INVENTORY_SCAN_EVERY_N_HEARTBEATS-th
    successful heartbeat (~10min at the 15s interval). Only attached to the
    heartbeat body when its fingerprint changed since the last time it was
    actually SENT SUCCESSFULLY — `last_sent_fingerprint` is only updated
    after send_heartbeat returns without raising, never before. `cached_scan`
    holds the current scan window's result across retries: if a send fails
    (network hiccup), the next attempt reuses the same scan instead of
    re-walking the filesystem, and only clears once that window's heartbeat
    actually went through.

    GPU polling (Speicher-Diät round, point #5, 30.08.2026) follows the
    exact same "cache across retries, only advance on success" shape: a
    fresh nvidia-smi read is due at startup and then every
    GPU_POLL_EVERY_N_HEARTBEATS-th successful heartbeat; a failed send
    reuses the same cached GPU reading on retry rather than forking a new
    nvidia-smi process for nothing.
    """
    backoff = HEARTBEAT_INTERVAL_S
    inventory_paths = default_inventory_paths()
    heartbeat_count = 0
    last_sent_fingerprint: str | None = None
    cached_scan: tuple[str, list[dict]] | None = None
    scan_due = True  # scan once, unconditionally, at startup
    cached_gpu: dict | None = None
    gpu_poll_due = True  # poll once, unconditionally, at startup

    while True:
        try:
            if gpu_poll_due:
                cached_gpu = parse_nvidia_smi(_run_nvidia_smi())
                gpu_poll_due = False

            telemetry = collect_telemetry(gpu_fields=cached_gpu)

            if scan_due and cached_scan is None:
                try:
                    entries = scan_model_inventory(inventory_paths)
                    cached_scan = (inventory_hash(entries), entries)
                except Exception as e:  # noqa: BLE001 — a broken scan must
                    # never take the heartbeat with it.
                    log.warning("Inventar-Scan fehlgeschlagen (%s) — Heartbeat geht ohne Inventar raus", e)
                    cached_scan = (last_sent_fingerprint, [])  # treat as "unchanged", still resolves this window

            inventory_to_send: list[dict] | None = None
            if cached_scan is not None and cached_scan[0] != last_sent_fingerprint:
                inventory_to_send = cached_scan[1]

            send_heartbeat(mc_url, token, telemetry, inventory=inventory_to_send)

            if cached_scan is not None:
                last_sent_fingerprint = cached_scan[0]
                cached_scan = None
                scan_due = False

            heartbeat_count += 1
            if heartbeat_count % INVENTORY_SCAN_EVERY_N_HEARTBEATS == 0:
                scan_due = True
            if heartbeat_count % GPU_POLL_EVERY_N_HEARTBEATS == 0:
                gpu_poll_due = True

            backoff = HEARTBEAT_INTERVAL_S
            time.sleep(HEARTBEAT_INTERVAL_S)
        except KeyboardInterrupt:
            log.info("Beendet (Ctrl+C).")
            return
        except Exception as e:  # noqa: BLE001 — intentional: NIE crashen (siehe Moduldoc)
            log.warning("Heartbeat fehlgeschlagen (%s) — nächster Versuch in %ss", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)


# ── CLI (manual sys.argv parsing — no argparse, see Speicher-Diät note) ──────

_USAGE = """usage: mc-node-agent.py [-h] [--mc-url URL] [--pair CODE] [--install] [--user USER]

Mission Control Node Agent — push telemetry (Fleet & Rezepte v2, Phase 1).

  --mc-url URL   Mission-Control-Basis-URL, z.B. https://mc.tailnet-name.ts.net
                 (oder Env MC_URL)
  --pair CODE    Pairing-Code einlösen (aus POST /api/v1/nodes/pairing-codes)
                 und Token speichern
  --install      Als systemd-Dienst installieren — braucht sudo/root
  --user USER    systemd User= für --install (Default: $SUDO_USER, sonst der
                 aktuelle User)
  -h, --help     Diese Hilfe anzeigen
"""


class _Args:
    __slots__ = ("mc_url", "pair", "install", "user")

    def __init__(self) -> None:
        self.mc_url: str | None = os.environ.get("MC_URL")
        self.pair: str | None = None
        self.install: bool = False
        self.user: str | None = None


def _arg_error(msg: str) -> "None":
    print(f"mc-node-agent.py: error: {msg}", file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    raise SystemExit(2)


def parse_args(argv: list[str] | None = None) -> _Args:
    """Manual parser for this agent's 4 flags — only the space-separated
    `--flag value` form is supported (matches every call site in this repo;
    `--flag=value` is not implemented, kept out on purpose for simplicity)."""
    argv = sys.argv[1:] if argv is None else argv
    args = _Args()
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            print(_USAGE)
            raise SystemExit(0)
        elif arg == "--mc-url":
            i += 1
            if i >= len(argv):
                _arg_error("--mc-url braucht einen Wert")
            args.mc_url = argv[i]
        elif arg == "--pair":
            i += 1
            if i >= len(argv):
                _arg_error("--pair braucht einen Wert (den Code)")
            args.pair = argv[i]
        elif arg == "--install":
            args.install = True
        elif arg == "--user":
            i += 1
            if i >= len(argv):
                _arg_error("--user braucht einen Wert")
            args.user = argv[i]
        else:
            _arg_error(f"Unbekannte Option: {arg}")
        i += 1
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.mc_url:
        log.error("--mc-url fehlt (oder Env MC_URL setzen).")
        return 1
    mc_url = args.mc_url.rstrip("/")

    if args.pair:
        try:
            token = pair(mc_url, args.pair)
        except Exception as e:
            log.error("Pairing fehlgeschlagen: %s", e)
            return 1
        saved_path = save_token(token)
        log.info("Pairing erfolgreich — Token gespeichert unter %s", saved_path)
    else:
        token = load_token()

    if args.install:
        run_as_user = args.user or _default_install_user()
        try:
            install_systemd_unit(mc_url, run_as_user)
        except subprocess.CalledProcessError as e:
            log.error("systemctl-Aufruf fehlgeschlagen: %s", e)
            return 1
        return 0

    if not token:
        log.error("Kein Token — entweder --pair CODE angeben, MC_NODE_TOKEN setzen, oder vorher pairen.")
        return 1

    log.info("mc-node-agent v%s startet, Ziel=%s", AGENT_VERSION, mc_url)
    run_loop(mc_url, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
