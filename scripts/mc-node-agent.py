#!/usr/bin/env python3
"""mc-node-agent — self-registering push-telemetry agent for Mission Control
(Fleet & Rezepte v2, Phase 1 — docs/plans/2026-08-30-node-agent-telemetry-phase1.md).

ONE FILE, STANDARD LIBRARY ONLY, Python >= 3.9. Design constraints (Mark,
30.08.2026 — "der Agent darf das servierende Modell NIE crashen"):

- No third-party dependencies — nothing to `pip install` on a box that is
  busy serving an LLM. Only stdlib (urllib, json, subprocess, shutil,
  argparse, ...).
- Outbound-only HTTPS poll, no listening socket — this agent is never
  something to attack from the network side.
- `--install` wraps the process in a systemd straightjacket (MemoryMax=128M,
  CPUQuota=10%, Nice=10, IOSchedulingClass=idle) so a runaway heartbeat loop
  can only ever starve itself, never the GPU workload sharing the box.
- Every collection step and every network call is wrapped in try/except — a
  stack trace killing the background process is worse than one missed
  heartbeat. Failures go to stderr (the systemd journal), backoff
  exponentially (15s -> 120s cap), and the loop NEVER exits on its own once
  it has started.

Usage — first run, mint a pairing code via
POST /api/v1/nodes/pairing-codes (admin, from the MC UI/API), then on the
target box:

    sudo python3 mc-node-agent.py --mc-url https://mc.example.ts.net \\
        --pair ABCD1234 --install

This trades the code for a token (saved to /etc/mc-node-agent/token,
chmod 600, chowned to the invoking user), writes + enables the systemd
service, and from then on the service runs the heartbeat loop unattended.

Manual run (token already paired, e.g. for testing without installing a
service):

    MC_NODE_TOKEN=... python3 mc-node-agent.py --mc-url https://mc.example.ts.net

`--install` needs sudo/root (it writes a unit under /etc/systemd/system and
calls systemctl) — running it as a normal user prints an error and exits
instead of silently doing nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import platform
import pwd
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
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

SYSTEM_TOKEN_PATH = Path("/etc/mc-node-agent/token")
USER_TOKEN_PATH = Path.home() / ".config" / "mc-node-agent" / "token"
SYSTEMD_UNIT_PATH = Path("/etc/systemd/system/mc-node-agent.service")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s mc-node-agent %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mc-node-agent")


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


def _walk_dir_stats(root: Path, follow_symlinks: bool) -> tuple[int, int, float | None]:
    """Sums file sizes + finds the newest mtime under `root`.

    Dedupes by real path when following symlinks — the HF cache's snapshots/
    directories are almost entirely symlinks into a shared blobs/ store, and
    two revisions pointing at the same blob must only count its bytes once.
    Any unreadable entry (permission error, dangling symlink) is skipped,
    not fatal.
    """
    seen: set[str] = set()
    total_bytes = 0
    file_count = 0
    mtime_max: float | None = None

    def _walk(path: str) -> None:
        nonlocal total_bytes, file_count, mtime_max
        try:
            entries = list(os.scandir(path))
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=follow_symlinks):
                    _walk(entry.path)
                elif entry.is_file(follow_symlinks=follow_symlinks):
                    key = os.path.realpath(entry.path) if follow_symlinks else entry.path
                    if key in seen:
                        continue
                    seen.add(key)
                    st = entry.stat(follow_symlinks=follow_symlinks)
                    total_bytes += st.st_size
                    file_count += 1
                    if mtime_max is None or st.st_mtime > mtime_max:
                        mtime_max = st.st_mtime
            except OSError:
                continue
        return

    _walk(str(root))
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
    total_bytes, file_count, mtime_max = _walk_dir_stats(path, follow_symlinks=True)
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
    total_bytes, file_count, mtime_max = _walk_dir_stats(snapshots_dir, follow_symlinks=True)
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
    """Deterministic hash of a scan result — the agent compares this against
    the hash it last SENT (kept in memory, see run_loop) to decide whether
    this scan cycle's inventory is worth attaching to the heartbeat."""
    canonical = json.dumps(entries, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def collect_telemetry() -> dict:
    """One telemetry snapshot — every source is independently guarded, so a
    single failing collector (e.g. no /proc on a non-Linux test box) still
    lets the rest of the heartbeat go out."""
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

    telemetry.update(parse_nvidia_smi(_run_nvidia_smi()))
    return telemetry


# ── HTTP (urllib only — no requests/httpx dependency) ────────────────────────


def _http_post_json(url: str, payload: dict, token: str | None, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}


def pair(mc_url: str, code: str) -> str:
    """Trades a pairing code for a node_token via POST /api/v1/nodes/pair
    (unauthenticated — the code itself is the credential)."""
    payload = {
        "code": code,
        "hostname": socket.gethostname(),
        "os": platform.system().lower(),
        "arch": platform.machine(),
        "agent_version": AGENT_VERSION,
    }
    try:
        result = _http_post_json(f"{mc_url}/api/v1/nodes/pair", payload, token=None, timeout=HTTP_TIMEOUT_S)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} von {mc_url}/api/v1/nodes/pair: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"{mc_url} nicht erreichbar: {e.reason}") from e
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
ExecStart={python} {script} --mc-url {mc_url}
Restart=always
RestartSec=30

# Straightjacket (Mark, 30.08.2026): this agent must never be able to
# starve the model-serving process sharing this box. A runaway loop can
# only ever hurt itself.
Nice=10
IOSchedulingClass=idle
MemoryMax=128M
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


def install_systemd_unit(mc_url: str, run_as_user: str) -> None:
    if os.geteuid() != 0:
        log.error(
            "--install braucht root (schreibt %s + ruft systemctl auf). "
            "Bitte mit sudo erneut ausführen.",
            SYSTEMD_UNIT_PATH,
        )
        raise SystemExit(1)
    if not SYSTEM_TOKEN_PATH.exists():
        log.error("Kein Token unter %s — erst --pair CODE ausführen (oder zusammen mit --install).", SYSTEM_TOKEN_PATH)
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

    Model-inventory scan (Nachtrag 30.08.2026): runs at startup and then
    every INVENTORY_SCAN_EVERY_N_HEARTBEATS-th successful heartbeat (~10min
    at the 15s interval). Only attached to the heartbeat body when its hash
    changed since the last time it was actually SENT — a restart just
    re-sends once, which is harmless.
    """
    backoff = HEARTBEAT_INTERVAL_S
    inventory_paths = default_inventory_paths()
    last_sent_inventory_hash: str | None = None
    heartbeat_count = 0
    while True:
        try:
            telemetry = collect_telemetry()

            inventory_to_send: list[dict] | None = None
            if heartbeat_count % INVENTORY_SCAN_EVERY_N_HEARTBEATS == 0:
                entries = scan_model_inventory(inventory_paths)
                current_hash = inventory_hash(entries)
                if current_hash != last_sent_inventory_hash:
                    inventory_to_send = entries
                    last_sent_inventory_hash = current_hash

            send_heartbeat(mc_url, token, telemetry, inventory=inventory_to_send)
            heartbeat_count += 1
            backoff = HEARTBEAT_INTERVAL_S
            time.sleep(HEARTBEAT_INTERVAL_S)
        except KeyboardInterrupt:
            log.info("Beendet (Ctrl+C).")
            return
        except Exception as e:  # noqa: BLE001 — intentional: NIE crashen (siehe Moduldoc)
            log.warning("Heartbeat fehlgeschlagen (%s) — nächster Versuch in %ss", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)


# ── CLI ────────────────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mission Control Node Agent — push telemetry (Fleet & Rezepte v2, Phase 1).",
    )
    parser.add_argument(
        "--mc-url",
        default=os.environ.get("MC_URL"),
        help="Mission-Control-Basis-URL, z.B. https://mc.example.ts.net (oder Env MC_URL)",
    )
    parser.add_argument(
        "--pair",
        metavar="CODE",
        help="Pairing-Code einlösen (aus POST /api/v1/nodes/pairing-codes) und Token speichern",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Als systemd-Dienst installieren — braucht sudo/root",
    )
    parser.add_argument(
        "--user",
        help="systemd User= für --install (Default: $SUDO_USER, sonst der aktuelle User)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

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
