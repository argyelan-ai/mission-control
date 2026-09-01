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
einer echten Box hardware, not estimated):

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

Steuerung freischalten — `--allow-control` (Rechte-Nachtrag 01.09.2026,
nach dem Live-Test auf einer echten Box: "RTNETLINK answers: Operation not permitted"):

Der Dienst läuft absichtlich als der aufrufende Benutzer, nicht als root
(Prinzip der geringsten Rechte, siehe UNIT_TEMPLATE weiter unten). Damit
kann der Agent den Gerätezustand MELDEN, aber nichts SETZEN — MTU, sysctl,
systemctl und der GPU-Modus brauchen alle root. Der Agent zu root zu machen
wäre die bequeme Antwort und die falsche: er spricht mit dem Netz.

Stattdessen ruft er die fünf Steuer-Aktionen über `sudo -n` auf, und
`--allow-control` schreibt die dazu passende Regel nach
/etc/sudoers.d/mc-node-agent:

    sudo python3 mc-node-agent.py --mc-url https://mc… --install --allow-control

Erlaubt sind GENAU diese fünf, jede mit absolutem Pfad und festen Argumenten:

  1. /usr/local/sbin/mc-gpu-mode.sh boost|normal|eco|eco+   GPU-Takt-Deckel
  2. /usr/local/sbin/mc-set-min-free.sh <zahl>              Speicher-Reserve
  3. systemctl enable|disable|start|stop earlyoom            OOM-Wächter
  4. /usr/local/sbin/latency-tune.sh                         Latenz-Abstimmung
  5. /usr/local/sbin/mc-set-mtu.sh <schnittstelle> <zahl>   MTU

Warum genau diese: sie sind eins zu eins die fünf Felder des
`desired_state` aus dem Geräte-Vertrag (docs/plans/2026-09-01-geraete-
steuerung-vertrag.md) — mehr kann MC gar nicht anfordern, also darf auch
nichts mehr erlaubt sein. Der systemctl-Pfad stammt aus derselben
control_binary()-Auflösung wie der echte Aufruf, damit Regel und Aufruf
nicht auseinanderlaufen können.

Warum Wrapper-Skripte statt `sysctl -w vm.min_free_kbytes=*` und
`ip link set * mtu *` (Review-Befund H1, 02.09.2026): ein `*` in sudoers
passt laut `man sudoers` auch ÜBER Wortgrenzen. Die alte sysctl-Regel liess
also `sysctl -w vm.min_free_kbytes=65536 kernel.core_pattern=|/tmp/x`
durch, die alte ip-Regel `ip link set eth0 down mtu 1500` — beides root.
sudo kann das seit 1.9.10 mit regulären Ausdrücken dicht machen, aber
nicht jede Box hat so ein neues sudo. Die beiden Wrapper
(scripts/device/mc-set-min-free.sh, mc-set-mtu.sh) prüfen deshalb SELBST:
genau N Argumente, nur Ziffern bzw. nur ein gültiger Schnittstellenname,
nur im erlaubten Bereich — und lehnen alles andere mit Exit 2 ab, BEVOR sie
irgendetwas ausführen. Die sudoers-Regel für die Wrapper enthält zwar
weiterhin ein `*` (die Zahl lässt sich nicht aufzählen), aber alles, was
über das Wortmuster hinaus mitgegeben wird, prallt am Wrapper ab.
`sysctl` und `ip` selbst tauchen in der Regel gar nicht mehr auf.

Bewusst NICHT in der Regel: `tee`, `sh`, `sed`, `bash`, `sysctl`, `ip` oder
irgendein Befehl mit freiem Pfad — jedes davon wäre über Umwege volles
root. Alle Skripte werden DIREKT aufgerufen (Shebang), nie über `bash
<skript>` — sonst wäre der Regeltext an einen Interpreter gebunden.

Die Steuer-Skripte liegen im Repo unter scripts/device/ und sind hier
zusätzlich als Text eingebettet (CONTROL_FILES, Drift-Test in
backend/tests/test_node_agent_parsers.py hält beide Fassungen byteidentisch).
Warum eingebettet und nicht per zweitem Endpunkt geladen: dieser Agent ist
EINE Datei, die per `curl` von GET /api/v1/nodes/agent-script oder über die
SSH-Einrichtung auf die Box kommt — ein zweiter Download hiesse ein zweiter
Endpunkt, ein zweiter Docker-Mount, ein zweiter Fehlerpfad, und die Skripte
könnten vom Agenten abweichen, der sie aufruft. So bringt `--install
--allow-control` alles selbst mit und kopiert es root-eigen (0755, nicht
für den Dienst-Benutzer schreibbar — sonst wäre ein Eintrag in sudoers
gleichbedeutend mit root) nach /usr/local/sbin/. Vor jedem Setzen prüft der
Agent, dass die Skripte da sind und root gehören; sonst steht in
`last_error` klar, was zu tun ist.

`--allow-control` lockert ausserdem die systemd-Zwangsjacke an genau zwei
Stellen, weil Steuern sonst unmöglich ist — nicht aus Nachlässigkeit:
`NoNewPrivileges` muss auf `no` (das Flag verbietet jedes setuid-Programm und
damit sudo — live belegt auf einer echten Box, 01.09.2026), und die Speichergrenze steigt
von 64 MB auf 96 MB, weil sudo und sein Kindprozess im selben cgroup zählen
und der erste `systemctl`-Aufruf sonst den OOM-Killer auslöst statt zu
wirken. Alles andere (Nice, IOSchedulingClass, CPUQuota) bleibt unverändert;
welche weiteren Härtungs-Flags den Steuer-Weg brechen würden, steht bei
render_unit().

OHNE `--allow-control` ändert sich nichts: der Agent meldet weiter, setzt
nichts, und die Zwangsjacke bleibt so streng wie bisher. Steuerung ist eine bewusste Entscheidung des Betreibers, kein
Nebeneffekt einer Installation. Fehlt die Regel, scheitert jede Setz-Aktion
mit einer verständlichen Meldung in `device_state.last_error` statt mit
einem rohen Exit-Code — der Agent stürzt nie ab.
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

# ── Geräte-Steuerung (docs/plans/2026-09-01-geraete-steuerung-vertrag.md) ────
#
# Warum eine feste Liste statt "was der Server schickt": ein Gerät fernsteuern
# heisst root-nahe Aktionen auslösen. Der Agent kennt deshalb NUR diese
# Aktionen, baut jeden Aufruf selbst als Argumentliste (nie shell=True, nie
# Serverdaten in eine Befehlszeile interpoliert) und lehnt alles ab, was nicht
# exakt in die erlaubten Werte passt (fail-closed).
GPU_MODES = ("boost", "normal", "eco", "eco+")
GPU_MODE_FILE = Path("/etc/mc-gpu-mode")
CONTROL_SCRIPT_DIR = "/usr/local/sbin"
GPU_MODE_SCRIPT = f"{CONTROL_SCRIPT_DIR}/mc-gpu-mode.sh"
LATENCY_TUNE_SCRIPT = f"{CONTROL_SCRIPT_DIR}/latency-tune.sh"
# Die beiden Wrapper mit strikter Argument-Prüfung (Review-Befund H1, siehe
# Moduldoc): sie ersetzen `sysctl -w …=*` und `ip link set * mtu *` in sudoers.
MIN_FREE_SCRIPT = f"{CONTROL_SCRIPT_DIR}/mc-set-min-free.sh"
MTU_SCRIPT = f"{CONTROL_SCRIPT_DIR}/mc-set-mtu.sh"
CLOCK_CAP_UNIT_PATH = Path("/etc/systemd/system/gb10-clock-cap.service")
CONTROL_SCRIPTS_MISSING_HINT = (
    "Steuer-Skripte fehlen — `sudo python3 mc-node-agent.py --mc-url … "
    "--install --allow-control` wiederholen"
)
# Der Halte-Prozess, den latency-tune.sh startet: er hält /dev/cpu_dma_latency
# offen. Stirbt er, fällt die Einstellung still zurück — genau deshalb prüfen
# wir ihn und nicht nur die ASPM-Datei.
LATENCY_TUNE_HOLDER = "cpu_dma_holder"
ASPM_POLICY_FILE = Path("/sys/module/pcie_aspm/parameters/policy")
MIN_FREE_KBYTES_FILE = Path("/proc/sys/vm/min_free_kbytes")
OOM_GUARD_UNIT = "earlyoom"
NET_ROUTE_FILE = Path("/proc/net/route")
SYS_CLASS_NET = Path("/sys/class/net")
# Plausibilitätsgrenzen. Ein Wert ausserhalb ist keine Konfiguration, sondern
# ein Fehler — ablehnen, statt das Gerät unbrauchbar zu machen (min_free von
# 100 GB macht die Box unbenutzbar, eine MTU von 70 killt das Netz).
# Absichtlich IDENTISCH zu backend/app/services/device_state.py
# (MIN_FREE_KBYTES_RANGE / MTU_RANGE): der Agent ist die letzte
# Verteidigungslinie und darf nie lockerer sein als das Backend — sonst
# entsteht ein Wertebereich, den nur eine der beiden Seiten kennt.
MIN_FREE_KBYTES_MIN = 65_536        # 64 MB
# Review M5 (02.09.2026): 64 GB war zu weit — auf einer 128-GB-Box wäre die
# Hälfte des Speichers Reserve. 16 GB reicht für jede sinnvolle Einstellung
# (im Feld: 5 GB) und deckelt den Schaden einer Fehleingabe.
MIN_FREE_KBYTES_MAX = 16_777_216    # 16 GB
# Review M5: 1280 (IPv6-Minimum) war zu tief — unter 1500 gibt es keinen
# sinnvollen Betrieb, aber einen leisen Leistungseinbruch im Verbund.
MTU_MIN = 1_500                     # Ethernet-Standard
MTU_MAX = 9_000                     # Jumbo-Frames
# Setz-Befehle dürfen hängen (systemctl wartet auf den Dienst) — hart deckeln,
# damit die Heartbeat-Schleife nie stehen bleibt.
SET_CMD_TIMEOUT_S = 20
# Review M1 (02.09.2026): "gesetzt" (Exit 0) heisst nicht "wirkt". Folgt der
# Ist-Zustand nach so vielen Setz-Versuchen mit Exit 0 immer noch nicht dem
# Soll, wird das Feld für PAUSE-Runden ausgesetzt und als last_error
# gemeldet. Sonst liefe alle 15 s derselbe root-Befehl (latency-tune.sh
# würde jedes Mal einen neuen Halte-Prozess starten, nvidia-smi jede Runde
# statt jede vierte laufen).
SET_ATTEMPTS_BEFORE_PAUSE = 3
SET_PAUSE_ROUNDS = 20               # 20 x 15 s = 5 Minuten
# Review N3: Deckel für die Grösse einer HTTP-Antwort. Eine Heartbeat-Antwort
# ist ein paar hundert Byte gross; alles jenseits von 1 MB ist kein MC,
# sondern ein Fehler oder ein Angriff — und würde die MemoryMax-Grenze der
# systemd-Unit (32/96 MB) sprengen, bevor json.loads überhaupt anfängt.
HTTP_MAX_BODY_BYTES = 1_048_576

# Rechte-Nachtrag 01.09.2026 (Live-Test auf einer echten Box: "RTNETLINK answers:
# Operation not permitted"). Der Dienst läuft als normaler Benutzer — Melden
# geht damit, Setzen nicht. Statt den Agenten zu root zu machen, ruft er die
# fünf Aktionen über `sudo -n` auf; erlaubt werden sie einzeln in
# /etc/sudoers.d/mc-node-agent (nur mit --allow-control, siehe Moduldoc).
SUDOERS_PATH = Path("/etc/sudoers.d/mc-node-agent")
# Absolute Pfade, deterministisch aus einer festen Kandidatenliste — bewusst
# OHNE $PATH: die sudoers-Regel gilt für den aufgelösten Pfad, und sudo löst
# über secure_path auf, nicht über den PATH des Dienstes. Beide Seiten müssen
# denselben Pfad meinen, sonst greift die Regel nicht und niemand sieht warum.
CONTROL_BINARY_CANDIDATES = {
    # Nur noch systemctl (Review H1, 02.09.2026): sysctl und ip laufen jetzt
    # ausschliesslich hinter den Wrapper-Skripten, bash gar nicht mehr —
    # Skripte werden direkt über ihren Shebang aufgerufen.
    "systemctl": ("/usr/bin/systemctl", "/bin/systemctl"),
}
# Diese Textbausteine schreibt sudo selbst, wenn es ablehnt. Sie sind der
# Unterschied zwischen "Regel fehlt" und "Befehl selbst ist gescheitert" —
# und damit zwischen einer verständlichen und einer nutzlosen Meldung.
_SUDO_REFUSAL_MARKERS = (
    "password is required",
    "a terminal is required",
    "not allowed to execute",
    "may not run",
    "no tty present",
    "is not in the sudoers file",
    "sudo: unable to",
)
SUDO_MISSING_RULE_HINT = (
    "keine Berechtigung: sudoers-Regel fehlt — Installation mit "
    "`sudo python3 mc-node-agent.py --mc-url … --install --allow-control` wiederholen"
)

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


def _float_or_none(raw: str) -> float | None:
    """Wie _int_or_none, aber für power.draw — Watt kommen mit Nachkomma."""
    s = raw.strip().strip("[]")
    if not s or s.upper() == "N/A":
        return None
    try:
        return round(float(s), 1)
    except ValueError:
        return None


def parse_nvidia_smi_device(stdout: str) -> dict:
    """Zieht Takt/Watt/Temperatur aus DERSELBEN nvidia-smi-Zeile, die
    parse_nvidia_smi liest (Felder 5 und 6 der erweiterten Abfrage in
    _run_nvidia_smi).

    Bewusst kein zweiter nvidia-smi-Aufruf: jeder Aufruf ist ein fork+exec mit
    ~21 MB Spitze auf GB10 (Speicher-Diät, siehe Moduldoc). Ein Aufruf, zwei
    Auswertungen. Eine alte/kurze Zeile (weniger Felder) ergibt None statt
    eines Fehlers.
    """
    result: dict[str, float | int | None] = {
        "gpu_clock_mhz": None,
        "gpu_power_w": None,
        "gpu_temp_c": None,
    }
    if not stdout or not stdout.strip():
        return result
    parts = [p.strip() for p in stdout.strip().splitlines()[0].split(",")]
    if len(parts) >= 4:
        result["gpu_temp_c"] = _int_or_none(parts[3])
    if len(parts) >= 5:
        result["gpu_clock_mhz"] = _int_or_none(parts[4])
    if len(parts) >= 6:
        result["gpu_power_w"] = _float_or_none(parts[5])
    return result


def parse_gpu_mode(text: str) -> str:
    """Inhalt von /etc/mc-gpu-mode -> einer der GPU_MODES, sonst 'unknown'.

    Fail-closed: was nicht exakt einer der vier bekannten Modi ist, wird nicht
    geraten. 'unknown' heisst für die Oberfläche schlicht "Soll ≠ Ist".
    """
    value = (text or "").strip().lower()
    return value if value in GPU_MODES else "unknown"


def parse_min_free_kbytes(text: str) -> int | None:
    try:
        return int((text or "").strip())
    except ValueError:
        return None


def parse_oom_guard(is_enabled_out: str, is_active_out: str) -> str:
    """Die zwei systemctl-Ausgaben -> 'active' | 'inactive' | 'missing'.

    'not-found' (Unit gar nicht installiert) ist der wichtigste Fall: auf
    Marks zweiter Box fehlte earlyoom komplett, was dort Einfrierer statt
    kontrollierter Abschüsse ergab — das muss sichtbar anders aussehen als
    "installiert, aber aus".
    """
    enabled = (is_enabled_out or "").strip().splitlines()
    enabled_state = enabled[0].strip() if enabled else ""
    if not enabled_state or enabled_state in ("not-found", "not-found."):
        return "missing"
    active = (is_active_out or "").strip().splitlines()
    return "active" if active and active[0].strip() == "active" else "inactive"


def parse_aspm_policy_performance(text: str) -> bool:
    """/sys/module/pcie_aspm/parameters/policy listet alle Werte, der aktive
    steht in eckigen Klammern: 'default performance [powersave] ...'."""
    return "[performance]" in (text or "")


def parse_default_iface(proc_net_route: str) -> str | None:
    """Schnittstelle der Standard-Route aus /proc/net/route.

    Die Schnittstelle heisst nicht überall gleich (je nach Board z. B. eth0 oder enp1s0)
    — deshalb nachschlagen statt festverdrahten. Ziel 00000000 = Default-Route;
    bei mehreren gewinnt die mit der kleinsten Metrik (Feld 7), wie im Kernel.
    """
    best: tuple[int, str] | None = None
    for line in (proc_net_route or "").splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[1] != "00000000":
            continue
        try:
            metric = int(fields[6])
        except ValueError:
            metric = 0
        if best is None or metric < best[0]:
            best = (metric, fields[0])
    return best[1] if best else None


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
    timeout, non-zero exit) — parse_nvidia_smi treats '' as "no GPU data".

    Geräte-Steuerung 01.09.2026: die Abfrage trägt zusätzlich clocks.gr und
    power.draw. Sie hängen HINTEN dran, damit parse_nvidia_smi (Felder 1-4)
    unverändert weiterliest — ein Aufruf bedient jetzt Telemetrie UND
    device_state, statt ein zweiter fork+exec (~21 MB Spitze) dazuzukommen.
    """
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total,"
                "temperature.gpu,clocks.gr,power.draw",
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


# ── Geräte-Zustand lesen (Ist) ───────────────────────────────────────────────


def _read_text(path: Path) -> str | None:
    """Kleine Systemdatei lesen; nicht vorhanden/nicht lesbar -> None. Jede
    einzelne Quelle darf fehlen, ohne den ganzen Zustand zu kippen."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def read_oom_guard() -> str:
    """'active' | 'inactive' | 'missing' — zwei systemctl-Aufrufe.

    Teuerster Teil des Ist-Zustands (zwei fork+exec), deshalb ruft run_loop
    das nur im selben Takt wie nvidia-smi auf und reicht das Ergebnis an
    collect_device_state weiter.
    """
    try:
        enabled = subprocess.run(
            ["systemctl", "is-enabled", OOM_GUARD_UNIT],
            capture_output=True, text=True, timeout=NVIDIA_SMI_TIMEOUT_S,
        )
        active = subprocess.run(
            ["systemctl", "is-active", OOM_GUARD_UNIT],
            capture_output=True, text=True, timeout=NVIDIA_SMI_TIMEOUT_S,
        )
    except FileNotFoundError:
        return "missing"  # kein systemd auf dieser Box — kein Fehler
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("systemctl-Abfrage für %s fehlgeschlagen: %s", OOM_GUARD_UNIT, e)
        return "missing"
    return parse_oom_guard(enabled.stdout or enabled.stderr, active.stdout)


def _holder_process_running(name: str) -> bool:
    """Läuft ein Prozess, dessen Kommandozeile `name` enthält?

    Bewusst über /proc statt `pgrep` — pgrep wäre ein fork+exec pro Heartbeat,
    /proc kostet nur ein paar sehr kleine Lesevorgänge. Der eigene Prozess
    wird übersprungen, damit ein Agent, der zufällig so heisst, sich nicht
    selbst findet.
    """
    self_pid = str(os.getpid())
    try:
        pids = os.listdir("/proc")
    except OSError:
        return False
    for pid in pids:
        if not pid.isdigit() or pid == self_pid:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read(4096)
        except OSError:
            continue  # Prozess ist inzwischen weg oder gehört jemand anderem
        if name.encode() in cmdline:
            return True
    return False


def read_latency_tune() -> bool:
    """latency-tune gilt nur als aktiv, wenn BEIDES stimmt: ASPM auf
    performance UND der Halte-Prozess lebt. Nach einem Neustart ist beides
    weg — genau der stille Rückfall, den der Vertrag sichtbar machen will."""
    policy = _read_text(ASPM_POLICY_FILE)
    if policy is None or not parse_aspm_policy_performance(policy):
        return False
    return _holder_process_running(LATENCY_TUNE_HOLDER)


def read_mtu() -> dict | None:
    """{'iface': ..., 'value': ...} der Standard-Route, oder None."""
    route = _read_text(NET_ROUTE_FILE)
    if route is None:
        return None
    iface = parse_default_iface(route)
    if not iface or not _iface_exists(iface):
        return None
    raw = _read_text(SYS_CLASS_NET / iface / "mtu")
    value = _int_or_none(raw) if raw is not None else None
    return {"iface": iface, "value": value}


def _iface_exists(iface: str) -> bool:
    """Schnittstellenname gegen /sys/class/net prüfen — verhindert, dass ein
    krummer Name (Pfadtrenner, '..') überhaupt in einen Befehl gelangt."""
    if not iface or "/" in iface or iface in (".", "..") or len(iface) > 32:
        return False
    return (SYS_CLASS_NET / iface).exists()


def collect_device_state(
    gpu_fields: dict | None = None,
    oom_guard: str | None = None,
    apply_status: dict | None = None,
) -> dict:
    """Ein Ist-Zustands-Schnappschuss laut Datenvertrag.

    `gpu_fields`/`oom_guard` erlauben es run_loop, die beiden teuren Quellen
    (nvidia-smi, systemctl) zwischenzuspeichern — gleiches Muster wie
    collect_telemetry(gpu_fields=...). `apply_status` trägt Zeitpunkt und
    Fehler der letzten Setz-Runde mit; ohne Setz-Versuch bleibt beides None.
    """
    gpu = gpu_fields if gpu_fields is not None else parse_nvidia_smi_device(_run_nvidia_smi())
    status = apply_status or {}
    state: dict = {
        "gpu_mode": parse_gpu_mode(_read_text(GPU_MODE_FILE) or ""),
        "gpu_clock_mhz": gpu.get("gpu_clock_mhz"),
        "gpu_power_w": gpu.get("gpu_power_w"),
        "gpu_temp_c": gpu.get("gpu_temp_c"),
        "min_free_kbytes": None,
        "oom_guard": oom_guard if oom_guard is not None else read_oom_guard(),
        "latency_tune": False,
        "mtu": None,
        "applied_at": status.get("applied_at"),
        "last_error": status.get("last_error"),
    }
    raw_min_free = _read_text(MIN_FREE_KBYTES_FILE)
    if raw_min_free is not None:
        state["min_free_kbytes"] = parse_min_free_kbytes(raw_min_free)
    try:
        state["latency_tune"] = read_latency_tune()
    except OSError:
        state["latency_tune"] = False
    try:
        state["mtu"] = read_mtu()
    except OSError:
        state["mtu"] = None
    return state


# ── Soll-Zustand anwenden ────────────────────────────────────────────────────
#
# Sicherheitsregeln dieses Abschnitts (Vertrag, harte Regel 8):
#  - NUR die fünf fest einprogrammierten Aktionen unten, nie ein Befehl aus
#    der Serverantwort.
#  - Jeder Aufruf als Argumentliste, NIEMALS shell=True — Serverdaten landen
#    dadurch nie in einer Kommandozeile, die eine Shell auseinandernimmt.
#  - Jeder Wert wird streng geprüft (Typ + erlaubte Menge/Grenzen). Was nicht
#    passt, wird ignoriert und als last_error gemeldet (fail-closed).
#  - Idempotent: was schon stimmt, wird nicht angefasst.


def control_binary(name: str, exists=None) -> str:
    """Absoluter Pfad zu einem Systembefehl aus CONTROL_BINARY_CANDIDATES.

    Erster existierender Kandidat gewinnt; existiert keiner, wird der erste
    zurückgegeben, damit die Fehlermeldung einen konkreten Pfad nennt statt
    eines nackten Namens. Dieselbe Funktion beliefert den Laufzeit-Aufruf UND
    die sudoers-Datei — sie können deshalb gar nicht auseinanderlaufen.
    """
    candidates = CONTROL_BINARY_CANDIDATES[name]
    check = exists if exists is not None else os.path.exists
    for path in candidates:
        if check(path):
            return path
    return candidates[0]


def _privileged(argv: list[str]) -> list[str]:
    """Setzt `sudo -n` davor, solange wir nicht schon root sind.

    `-n` heisst: niemals nach einem Passwort fragen. Ein Dienst hat kein
    Terminal — ohne `-n` würde er hängen statt zu scheitern.
    """
    if os.geteuid() == 0:
        return argv
    return ["sudo", "-n"] + argv


def _run_set_cmd(argv: list[str]) -> str | None:
    """Führt eine der fest verdrahteten Aktionen aus. Rückgabe: None bei
    Erfolg, sonst eine kurze Fehlerbeschreibung für last_error.

    Der wichtigste Fall ist die fehlende sudoers-Regel: `sudo -n` gibt dann
    Exit 1 mit "a password is required" zurück — für den Betreiber unbrauchbar.
    Daraus wird hier ein Satz, der sagt, was zu tun ist.
    """
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=SET_CMD_TIMEOUT_S
        )
    except FileNotFoundError:
        if argv[0] == "sudo":
            return "sudo nicht vorhanden — Steuerung braucht sudo oder einen Dienst als root"
        return f"{argv[0]} nicht vorhanden"
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"{argv[0]} fehlgeschlagen: {e}"
    if proc.returncode == 0:
        return None
    detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
    if argv[0] == "sudo" and any(m in detail.lower() for m in _SUDO_REFUSAL_MARKERS):
        return SUDO_MISSING_RULE_HINT
    # Auch ohne sudo-Schicht (Dienst läuft als root) bleibt "not permitted"
    # möglich — etwa wenn der Kernel die Änderung ablehnt. Roh durchreichen,
    # das ist dann die ehrlichste Information.
    return f"{argv[-1] if argv[0] == 'sudo' else argv[0]} -> Exit {proc.returncode}: {detail[:200]}"


def _valid_int(value: object, low: int, high: int) -> int | None:
    """int im Bereich [low, high] — bool wird abgelehnt (bool ist in Python
    ein int-Subtyp, `True` würde sonst als 1 durchrutschen)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def control_script_problem(path: str) -> str | None:
    """Darf dieses Steuer-Skript über sudo als root laufen? None = ja.

    Zwei Prüfungen, beide aus demselben Grund: ein Skript, das in sudoers
    steht, IST root. Fehlt es, kann nichts gesetzt werden (klarer Hinweis
    statt rohem Exit-Code). Gehört es nicht root oder dürfen Gruppe/Andere
    hineinschreiben, könnte der Dienst-Benutzer (oder irgendwer) seinen
    eigenen Inhalt als root ausführen — dann lieber gar nicht setzen.
    """
    try:
        st = os.stat(path)
    except OSError:
        return CONTROL_SCRIPTS_MISSING_HINT
    if st.st_uid != 0:
        return f"{path} gehört nicht root — aus Sicherheitsgründen nicht ausgeführt"
    if st.st_mode & 0o022:
        return f"{path} ist für Gruppe/Andere schreibbar — aus Sicherheitsgründen nicht ausgeführt"
    return None


class _ApplyTracker:
    """Merkt sich pro Feld, wie oft ein Setz-Befehl mit Exit 0 durchlief,
    ohne dass der Ist-Zustand danach dem Soll folgte (Review M1).

    Warum: `changed` hängt am Exit-Code, nicht am Zustand. Ein Skript, das
    0 zurückgibt, aber nichts bewirkt (ASPM-Datei fehlt, nvidia-smi
    schweigt), würde sonst alle 15 s erneut laufen — bei latency-tune.sh mit
    jedem Mal einem neuen Halte-Prozess, und nvidia-smi jede Runde statt
    jede vierte. Nach SET_ATTEMPTS_BEFORE_PAUSE wirkungslosen Versuchen
    pausiert das Feld für SET_PAUSE_ROUNDS Runden und steht so lange mit
    "gesetzt, aber Ist folgt nicht" im last_error. Danach ein neuer Anlauf
    — vielleicht hat der Betreiber inzwischen etwas repariert.
    """

    __slots__ = ("round", "attempts", "paused_until", "targets")

    def __init__(self) -> None:
        self.round = 0
        self.attempts: dict[str, int] = {}
        self.paused_until: dict[str, int] = {}
        # Welcher Befehl (Feld -> Signatur) wurde zuletzt gezählt? Ändert MC
        # das Ziel (eco -> boost), beginnt eine neue Versuchsreihe — sonst
        # zählte ein schnelles Hin und Her als "wirkt nicht".
        self.targets: dict[str, str] = {}

    def begin_round(self, still_pending: dict[str, str]) -> None:
        """Am Rundenanfang mit dem FRISCHEN Ist-Zustand: welche Felder weichen
        noch ab (Feld -> Befehls-Signatur)? Alles andere ist erfüllt und wird
        vergessen; ein Feld mit NEUEM Ziel fängt bei null an."""
        self.round += 1
        for field in list(self.attempts):
            if still_pending.get(field) != self.targets.get(field):
                self.attempts.pop(field, None)
                self.paused_until.pop(field, None)
                self.targets.pop(field, None)
        for field, n in list(self.attempts.items()):
            if n >= SET_ATTEMPTS_BEFORE_PAUSE and field not in self.paused_until:
                self.paused_until[field] = self.round + SET_PAUSE_ROUNDS
        for field, until in list(self.paused_until.items()):
            if self.round >= until:
                # Pause vorbei: Zähler zurück, ein neuer Anlauf ist erlaubt.
                self.paused_until.pop(field, None)
                self.attempts[field] = 0

    def is_paused(self, field: str) -> bool:
        return self.round < self.paused_until.get(field, 0)

    def pause_message(self, field: str) -> str:
        n = self.attempts.get(field, 0)
        remaining_s = (self.paused_until.get(field, self.round) - self.round) * HEARTBEAT_INTERVAL_S
        return (
            f"{field}: gesetzt, aber Ist folgt nicht ({n} Versuche mit Exit 0) — "
            f"Pause, nächster Versuch in ~{remaining_s} s"
        )

    def record_success(self, field: str, signature: str) -> None:
        if self.targets.get(field) != signature:
            self.attempts[field] = 0
            self.targets[field] = signature
        self.attempts[field] = self.attempts.get(field, 0) + 1

    def reset(self) -> None:
        """MC hat den Soll gelöscht — nichts mehr zu verfolgen."""
        self.attempts.clear()
        self.paused_until.clear()
        self.targets.clear()


def plan_desired_state(desired: dict, current: dict) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """Welche Befehle bringen `current` auf `desired`? Reine Funktion, führt
    nichts aus.

    Rückgabe: (Liste von (Feldname, argv), Fehlerliste). Nur abweichende
    Felder ergeben einen Befehl; Felder, die in `desired` fehlen, bleiben
    unangetastet ("kein Feld = keine Meinung"). Jeder Wert wird streng
    geprüft — was nicht passt, wird zum Fehler, nie zum Befehl.
    """
    errors: list[str] = []
    plans: list[tuple[str, list[str]]] = []
    if not isinstance(desired, dict):
        return [], ["desired_state ist kein Objekt — ignoriert"]

    if "gpu_mode" in desired:
        mode = desired["gpu_mode"]
        if not isinstance(mode, str) or mode not in GPU_MODES:
            errors.append(f"gpu_mode {mode!r} abgelehnt (erlaubt: {', '.join(GPU_MODES)})")
        elif mode != current.get("gpu_mode"):
            plans.append(("gpu_mode", [GPU_MODE_SCRIPT, mode]))

    if "min_free_kbytes" in desired:
        value = _valid_int(desired["min_free_kbytes"], MIN_FREE_KBYTES_MIN, MIN_FREE_KBYTES_MAX)
        if value is None:
            errors.append(
                f"min_free_kbytes {desired['min_free_kbytes']!r} abgelehnt "
                f"(ganze Zahl {MIN_FREE_KBYTES_MIN}..{MIN_FREE_KBYTES_MAX} erwartet)"
            )
        elif value != current.get("min_free_kbytes"):
            plans.append(("min_free_kbytes", [MIN_FREE_SCRIPT, str(value)]))

    if "oom_guard" in desired:
        want = desired["oom_guard"]
        if not isinstance(want, bool):
            errors.append(f"oom_guard {want!r} abgelehnt (true/false erwartet)")
        else:
            is_active = current.get("oom_guard") == "active"
            if want != is_active:
                verb = "enable" if want else "disable"
                plans.append(("oom_guard", [control_binary("systemctl"), verb, "--now", OOM_GUARD_UNIT]))

    if "latency_tune" in desired:
        want = desired["latency_tune"]
        if not isinstance(want, bool):
            errors.append(f"latency_tune {want!r} abgelehnt (true/false erwartet)")
        elif want and not current.get("latency_tune"):
            plans.append(("latency_tune", [LATENCY_TUNE_SCRIPT]))
        elif not want and current.get("latency_tune"):
            # Es gibt (Stand 01.09.2026) kein Rückweg-Skript. Ehrlich melden
            # statt einen Ausschalt-Befehl zu erfinden — ein Neustart räumt es
            # ohnehin weg.
            errors.append("latency_tune=false: kein Ausschalt-Skript vorhanden — bitte neu starten")

    if "mtu" in desired:
        value = _valid_int(desired["mtu"], MTU_MIN, MTU_MAX)
        current_mtu = current.get("mtu") or {}
        iface = current_mtu.get("iface")
        if value is None:
            errors.append(
                f"mtu {desired['mtu']!r} abgelehnt (ganze Zahl {MTU_MIN}..{MTU_MAX} erwartet)"
            )
        elif not iface or not _iface_exists(iface):
            errors.append("mtu: keine Standard-Route-Schnittstelle gefunden")
        elif value != current_mtu.get("value"):
            plans.append(("mtu", [MTU_SCRIPT, iface, str(value)]))

    return plans, errors


def _plan_label(field: str, argv: list[str]) -> str:
    """Kurzer Präfix für Fehlermeldungen: 'gpu_mode=eco', 'mtu=9000 auf eth0'."""
    if field == "mtu":
        return f"mtu={argv[2]} auf {argv[1]}"
    if field == "oom_guard":
        return f"oom_guard={'True' if argv[1] == 'enable' else 'False'}"
    if field == "latency_tune":
        return "latency_tune"
    return f"{field}={argv[-1]}"


def apply_desired_state(
    desired: dict, current: dict, tracker: "_ApplyTracker | None" = None
) -> tuple[bool, list[str]]:
    """Gleicht `current` an `desired` an — nur die abweichenden Felder.

    Rückgabe: (etwas gesetzt?, Fehlerliste). Wirft nie: jeder Fehler wird
    eingesammelt und landet im nächsten Heartbeat als last_error.

    Vor jedem Skript-Aufruf wird geprüft, dass das Skript da ist und root
    gehört (control_script_problem) — sonst ein klarer Hinweis statt eines
    rohen sudo-Fehlers. `tracker` (optional, run_loop) setzt Felder aus, die
    trotz Exit 0 nicht wirken (Review M1).
    """
    plans, errors = plan_desired_state(desired, current)
    changed = False
    for field, argv in plans:
        label = _plan_label(field, argv)
        if tracker is not None and tracker.is_paused(field):
            errors.append(tracker.pause_message(field))
            continue
        if argv[0].startswith(CONTROL_SCRIPT_DIR + "/"):
            problem = control_script_problem(argv[0])
            if problem:
                errors.append(f"{label}: {problem}")
                continue
        err = _run_set_cmd(_privileged(argv))
        if err:
            errors.append(f"{label}: {err}")
        else:
            changed = True
            if tracker is not None:
                tracker.record_success(field, " ".join(argv))
    return changed, errors


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
    total = 0
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
        # Review N3: derselbe Deckel wie bei Content-Length, über alle Stücke
        # summiert — chunked darf nicht der Umweg um die Grenze sein.
        total += size
        if total > HTTP_MAX_BODY_BYTES:
            raise ValueError(f"HTTP-Antwort zu gross (chunked > {HTTP_MAX_BODY_BYTES} Byte)")
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
        # Review N3: Deckel VOR dem Lesen — sonst würde ein böswilliges oder
        # kaputtes Gegenüber mit "Content-Length: 4000000000" den Agenten
        # in die MemoryMax-Grenze der Unit laufen lassen (OOM-Kill statt
        # Fehlermeldung). Negative Längen sind ebenso Unsinn.
        if content_length < 0 or content_length > HTTP_MAX_BODY_BYTES:
            raise ValueError(
                f"HTTP-Antwort zu gross ({content_length} Byte, Deckel {HTTP_MAX_BODY_BYTES})"
            )
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


def send_heartbeat(
    mc_url: str,
    token: str,
    telemetry: dict,
    inventory: list[dict] | None = None,
    device_state: dict | None = None,
) -> dict:
    """Sendet den Heartbeat und gibt die Antwort zurück. `device_state` ist
    optional — ein Backend ohne Geräte-Steuerung ignoriert das Feld, und ein
    Agent auf einer Box ohne lesbaren Zustand lässt es einfach weg."""
    payload = {"telemetry": telemetry, "agent_version": AGENT_VERSION}
    if inventory is not None:
        payload["inventory"] = inventory
    if device_state is not None:
        payload["device_state"] = device_state
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
MemoryHigh={memory_high}
MemoryMax={memory_max}
TasksMax={tasks_max}
CPUQuota=10%
{no_new_privileges}
[Install]
WantedBy=multi-user.target
"""

# Genau die Zeilen, die sich zwischen Melde- und Steuer-Betrieb unterscheiden.
# Alles andere an der Zwangsjacke bleibt in BEIDEN Fällen gleich.
_UNIT_REPORT_ONLY = {
    # Review M6 (02.09.2026), LIVE GEMESSEN statt geschätzt: `systemctl show
    # mc-node-agent -p MemoryPeak` auf einer GB10-Box nach 24 h Betrieb =
    # 49,3 MB Spitze (30,7 MB laufend) — die Spitze kommt vom nvidia-smi-
    # Fork, der im selben cgroup zählt. Der alte harte Deckel von 32 MB lag
    # UNTER dieser Spitze: die Melde-Unit wäre vom cgroup-OOM-Killer
    # abgeschossen worden, ohne Meldung im last_error. 64 MB = Spitze plus
    # Reserve; MemoryHigh knapp darüber, damit der Kernel nicht bei jedem
    # nvidia-smi-Aufruf zu trimmen anfängt. Die Speicher-Diät im Moduldoc
    # bleibt richtig — nur die Zahl war zu knapp.
    "memory_high": "56M",
    "memory_max": "64M",
    "tasks_max": "8",
    "no_new_privileges": (
        "# Rechte-Sperre: der Prozess darf nie Rechte dazugewinnen. Gilt im\n"
        "# reinen Melde-Betrieb (Standard) — er braucht keine.\n"
        "NoNewPrivileges=true\n"
    ),
}
_UNIT_WITH_CONTROL = {
    # sudo + Kindprozess (systemctl/ip/bash) laufen IM SELBEN cgroup und
    # zählen auf dieselbe Speichergrenze. Der Agent liegt bei ~49 MB Spitze
    # (gemessen 02.09.2026, siehe _UNIT_REPORT_ONLY) — bei 64 MB hart würde
    # der erste `systemctl enable` den cgroup-OOM-Killer
    # auslösen, und zwar ohne Fehlermeldung im last_error. Das wäre genau der
    # stille Ausfall, den wir sonst überall jagen. Der Deckel steigt deshalb
    # nur im Steuer-Betrieb, und nur so weit, dass ein kurzlebiger Helfer
    # hineinpasst — die Zwangsjacke bleibt eine Zwangsjacke.
    "memory_high": "48M",
    "memory_max": "96M",
    # sudo + Kind = zwei zusätzliche Tasks, systemctl bringt eigene Threads mit.
    "tasks_max": "16",
    "no_new_privileges": (
        "# Rechte-Sperre AUS — bewusste Abwägung, nicht Nachlässigkeit:\n"
        "# NoNewPrivileges verbietet dem Prozess grundsätzlich, Rechte\n"
        "# dazuzugewinnen, und blockiert damit jedes setuid-Programm — also\n"
        "# auch sudo (live belegt auf einer echten Box, 01.09.2026: \'The \"no new\n"
        "# privileges\" flag is set, which prevents sudo from running as\n"
        "# root\'). Mit --allow-control SOLL der Agent genau fünf Aktionen\n"
        "# als root auslösen können; die Grenze zieht dann die sudoers-Regel\n"
        "# (/etc/sudoers.d/mc-node-agent), nicht mehr dieses Flag. Ohne\n"
        "# --allow-control bleibt es auf true.\n"
        "NoNewPrivileges=no\n"
    ),
}


def render_unit(mc_url: str, user: str, python: str, script: str, allow_control: bool = False) -> str:
    """Der Inhalt der systemd-Unit. Reine Funktion, damit der Unterschied
    zwischen Melde- und Steuer-Betrieb direkt prüfbar ist.

    Härtungs-Flags, die den Steuer-Weg brechen WÜRDEN, falls sie jemand
    später ergänzt — hier festgehalten, damit es niemand einzeln im Feld
    herausfinden muss (Stand 01.09.2026 ist keines davon in dieser Unit):

      NoNewPrivileges=true   blockiert sudo komplett  -> hier abhängig gemacht
      RestrictSUIDSGID=yes   blockiert sudo (setuid)  -> nicht setzen
      ProtectKernelTunables=yes  macht /proc/sys nur-lesbar -> sysctl -w scheitert
      ProtectSystem=strict   /etc + /usr nur-lesbar   -> sudoers/Skripte lesbar,
                             aber sysctl-Schreibwege dicht -> nicht setzen
      PrivateDevices=yes     versteckt /dev           -> latency-tune braucht
                             /dev/cpu_dma_latency
      CapabilityBoundingSet= leer würde CAP_NET_ADMIN entfernen -> ip link scheitert

    Nicht betroffen (bremsen nur, blockieren nicht): Nice, IOSchedulingClass,
    CPUQuota. Ein Setz-Befehl darf dadurch langsamer werden, SET_CMD_TIMEOUT_S
    (20 s) ist dafür reichlich bemessen.
    """
    values = _UNIT_WITH_CONTROL if allow_control else _UNIT_REPORT_ONLY
    return UNIT_TEMPLATE.format(user=user, python=python, script=script, mc_url=mc_url, **values)


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


SUDOERS_HEADER = """# Mission Control node-agent — Geräte-Steuerung (--allow-control)
#
# Erzeugt von mc-node-agent.py. NICHT von Hand bearbeiten: bei der nächsten
# Installation wird die Datei überschrieben.
#
# Warum es diese Datei gibt: der Dienst läuft als normaler Benutzer (Prinzip
# der geringsten Rechte). Melden geht damit, Setzen nicht — MTU, sysctl,
# systemctl und der GPU-Modus brauchen alle root. Statt den ganzen Agenten zu
# root zu machen, sind hier GENAU die fünf Aktionen erlaubt, die MC steuern
# kann, jede mit festem Pfad und festen Argumenten.
#
# Bewusst NICHT erlaubt: tee, sh, sed, bash, sysctl, ip oder irgendein Befehl
# mit freiem Pfad. Jedes davon wäre über Umwege volles root. Zahlen-Argumente
# (Speicher-Reserve, MTU) laufen über Wrapper-Skripte, die ihre Argumente
# SELBST streng prüfen — ein `*` in sudoers passt über Wortgrenzen hinweg
# und wäre allein keine Grenze.
"""


def render_sudoers(user: str, exists=None) -> str:
    """Der Inhalt von /etc/sudoers.d/mc-node-agent für `user`.

    Reine Funktion (kein Schreiben), damit sie direkt geprüft werden kann.
    Der systemctl-Pfad kommt aus derselben control_binary()-Auflösung wie
    der echte Aufruf — die Regel kann deshalb nicht am tatsächlich
    ausgeführten Pfad vorbeigehen. Die Skript-Pfade sind Konstanten, die
    Aufruf und Regel gemeinsam nutzen.
    """
    systemctl = control_binary("systemctl", exists=exists)

    cmds: list[str] = []
    # 1. GPU-Modus: die vier Modi einzeln, kein Platzhalter — ein `*` würde
    #    jedes Argument erlauben, das das Skript je bekommen könnte
    #    (auch `restore`/`status`, die nur systemd/root gehören).
    cmds += [f"{GPU_MODE_SCRIPT} {mode}" for mode in GPU_MODES]
    # 2. Speicher-Reserve: Wrapper mit genau EINEM Zahl-Argument. Der `*`
    #    ist nötig (die Zahl lässt sich nicht aufzählen), passt aber laut
    #    `man sudoers` über Wortgrenzen — die Grenze zieht der Wrapper:
    #    argc != 1 oder Nicht-Ziffern -> Exit 2, nichts ausgeführt.
    cmds.append(f"{MIN_FREE_SCRIPT} *")
    # 3. OOM-Wächter: nur die eine Unit. `--now` steht mit drin, weil der
    #    Agent genau so aufruft — eine Regel, die der echte Aufruf nicht
    #    trifft, wäre wertlos.
    for verb in ("enable", "disable", "start", "stop"):
        cmds.append(f"{systemctl} {verb} {OOM_GUARD_UNIT}")
    # `--now` nur bei enable/disable — nur die kennen den Schalter, und genau
    # so ruft der Agent auf (einschalten UND starten in einem Schritt).
    for verb in ("enable", "disable"):
        cmds.append(f"{systemctl} {verb} --now {OOM_GUARD_UNIT}")
    # 4. Latenz-Abstimmung: das Skript direkt, OHNE Argumente. Das leere
    #    Argumentmuster in sudoers heisst "genau so, nichts dahinter".
    cmds.append(f'{LATENCY_TUNE_SCRIPT} ""')
    # 5. MTU: Wrapper mit genau ZWEI Argumenten (Schnittstelle, Wert) — auch
    #    hier prüft der Wrapper argc, Zeichensatz und Bereich, nicht sudo.
    cmds.append(f"{MTU_SCRIPT} *")

    lines = [SUDOERS_HEADER]
    lines += [f"{user} ALL=(root) NOPASSWD: {cmd}" for cmd in cmds]
    return "\n".join(lines) + "\n"


def _visudo_check(path: Path) -> str | None:
    """Prüft eine sudoers-Datei mit `visudo -c -f`. None = in Ordnung."""
    try:
        proc = subprocess.run(
            ["visudo", "-c", "-f", str(path)],
            capture_output=True, text=True, timeout=SET_CMD_TIMEOUT_S,
        )
    except FileNotFoundError:
        return "visudo nicht gefunden — ohne Syntaxprüfung wird nichts installiert"
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"visudo-Prüfung fehlgeschlagen: {e}"
    if proc.returncode == 0:
        return None
    return (proc.stderr or proc.stdout or "").strip()[:500]


def _sudoers_staging_path() -> Path:
    """Die Prüf-Datei liegt INNERHALB von /etc/sudoers.d/ (Review H2).

    Warum dort und nicht unter /etc/mc-node-agent/: jenes Verzeichnis gehört
    dem Dienst-Benutzer. Er könnte zwischen visudo-Prüfung und Verschieben
    die Datei austauschen oder vorab einen Symlink hinlegen — und so eine
    eigene Regel als root installieren lassen. /etc/sudoers.d/ gehört root.
    Der Punkt im Namen ist Absicht: sudo ignoriert per Definition jede Datei
    in sudoers.d, deren Name einen `.` enthält oder auf `~` endet — die
    Prüf-Datei ist also nie eine gültige Regel, auch nicht kurz.
    """
    return SUDOERS_PATH.parent / (SUDOERS_PATH.name + ".tmp")


def _write_new_file_nofollow(path: Path, content: str, mode: int) -> None:
    """Datei NEU anlegen: O_EXCL (existiert sie schon, Fehler statt
    überschreiben) + O_NOFOLLOW (ein untergeschobener Symlink wird nicht
    verfolgt). Beides zusammen schliesst das Zeitfenster, in dem jemand
    anderes den Pfad besetzen könnte."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    os.chmod(path, mode)


def install_sudoers(run_as_user: str) -> None:
    """Schreibt /etc/sudoers.d/mc-node-agent — nur mit --allow-control.

    Reihenfolge mit Absicht: erst in die Prüf-Datei `mc-node-agent.tmp`
    IM root-eigenen sudoers.d schreiben (sudo ignoriert sie wegen des
    Punkts im Namen), dort mit `visudo -c` prüfen, und erst danach mit
    einem atomaren os.replace auf den echten Namen legen. Eine kaputte
    Datei in sudoers.d legt JEDES sudo auf der Box lahm — der Nutzer könnte
    sich selbst aussperren. So gibt es dieses Zeitfenster gar nicht erst.
    Nach dem Umbenennen wird noch einmal geprüft und im Zweifel sofort
    gelöscht.
    """
    if os.geteuid() != 0:
        log.error("--allow-control braucht root (schreibt %s). Bitte mit sudo ausführen.", SUDOERS_PATH)
        raise SystemExit(1)

    content = render_sudoers(run_as_user)
    SUDOERS_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    staging = _sudoers_staging_path()
    # Reste eines abgebrochenen Laufs (oder ein untergeschobener Symlink)
    # werden entfernt, nicht verfolgt: unlink löscht den Link selbst, nie
    # das Ziel. Danach legt O_EXCL die Datei garantiert frisch an.
    if staging.is_symlink() or staging.exists():
        staging.unlink()
    try:
        _write_new_file_nofollow(staging, content, 0o440)
    except OSError as e:
        log.error("Prüf-Datei %s konnte nicht angelegt werden: %s", staging, e)
        raise SystemExit(1)

    problem = _visudo_check(staging)
    if problem:
        staging.unlink(missing_ok=True)
        log.error("sudoers-Regel abgelehnt, nichts installiert: %s", problem)
        raise SystemExit(1)

    os.replace(staging, SUDOERS_PATH)
    os.chmod(SUDOERS_PATH, 0o440)
    try:
        os.chown(SUDOERS_PATH, 0, 0)
    except OSError as e:
        log.warning("chown root:root auf %s fehlgeschlagen: %s", SUDOERS_PATH, e)

    problem = _visudo_check(SUDOERS_PATH)
    if problem:
        # Sicherheitsnetz: falls die Datei am echten Platz doch beanstandet
        # wird (etwa wegen einer Kollision mit einer anderen Regel), sofort
        # weg damit — lieber keine Steuerung als ein kaputtes sudo.
        SUDOERS_PATH.unlink(missing_ok=True)
        log.error("sudoers-Regel nach dem Einbau beanstandet, wieder entfernt: %s", problem)
        raise SystemExit(1)

    log.info(
        "Steuerung freigeschaltet: %s erlaubt %s genau die fünf MC-Aktionen (sonst nichts).",
        SUDOERS_PATH, run_as_user,
    )


def remove_sudoers() -> bool:
    """Rückweg für --allow-control (Review M3): `--install` OHNE das Flag
    nimmt die Steuerung wieder weg. True, wenn eine Datei entfernt wurde.

    Auch eine liegengebliebene Prüf-Datei wird entsorgt. Die Steuer-Skripte
    in /usr/local/sbin bleiben absichtlich: root-eigen und ohne sudoers-Regel
    sind sie harmlos, und gb10-clock-cap.service braucht mc-gpu-mode.sh
    weiterhin beim Systemstart.
    """
    removed = False
    for path in (SUDOERS_PATH, _sudoers_staging_path()):
        try:
            if path.is_symlink() or path.exists():
                path.unlink()
                removed = removed or path == SUDOERS_PATH
        except OSError as e:
            log.warning("%s konnte nicht entfernt werden: %s", path, e)
    if removed:
        log.info("Steuerung zurückgenommen: %s entfernt — der Agent meldet nur noch.", SUDOERS_PATH)
    return removed


# ── Steuer-Skripte (eingebettet; Quelle und Drift-Test: scripts/device/) ────
#
# Warum als Text im Agenten: siehe Moduldoc ("Warum eingebettet und nicht per
# zweitem Endpunkt"). Die Dateien unter scripts/device/ sind die lesbare,
# prüfbare Fassung; backend/tests/test_node_agent_parsers.py stellt sicher,
# dass beide byteidentisch sind. Änderungen also IMMER dort machen und hier
# nachziehen (der Test wird sonst rot).

# >>> CONTROL_FILES (erzeugt von scripts/device/sync-into-agent.py — NICHT von Hand ändern)
CONTROL_FILES: dict[str, tuple[str, int, str]] = {
    # Zielpfad -> (Dateiname unter scripts/device/, Rechte, Inhalt)
    f"{CONTROL_SCRIPT_DIR}/mc-gpu-mode.sh": ("mc-gpu-mode.sh", 0o755, r"""#!/bin/bash
# mc-gpu-mode.sh {boost|normal|eco|eco+|restore|status} — GPU-Takt-Deckel (GB10).
#
# Warum es das gibt: GB10 schaltet unter Dauerlast HART ab — kein Kernel-
# Panic, kein Log. Der Embedded-Controller kappt den Strom, bevor das System
# etwas schreiben kann. Ein Takt-Deckel behebt das. Die Erzeugung hängt an
# der Speicherbandbreite, NICHT am Takt — Drosseln kostet deshalb fast
# nichts und spart sehr viel Strom (eigener Sweep 16.08.2026, Qwen38-27B,
# EINE Box):
#
#   Stufe    Takt     Erzeugung   Einlesen   Watt ⌀   °C max
#   boost    frei     20,3 tok/s   36,5 s     59,5      87    (Drosselung tritt auf)
#   normal   2200     19,6         37,8       39,9      81
#   eco      2000     20,4         39,2       32,5      74    <- bester Arbeitspunkt
#   eco+     1800     19,8         40,7       27,1      69
#
# Die Stufe steht in /etc/mc-gpu-mode; gb10-clock-cap.service ruft beim
# Systemstart `restore` auf, damit der Deckel einen Neustart überlebt.
#
# Wird von mc-node-agent.py installiert (root:root 0755 in /usr/local/sbin,
# nur mit --install --allow-control). Die sudoers-Regel erlaubt dem Agenten
# NUR die vier Stufen — `restore` und `status` ruft nur root/systemd.
set -u
STATE=/etc/mc-gpu-mode
MODE="${1:-status}"
NVSMI=/usr/bin/nvidia-smi

if [ "$#" -gt 1 ]; then
  echo "abgelehnt: höchstens ein Argument erwartet" >&2
  exit 2
fi
# Erst das Argument prüfen, dann die Umgebung: ein unbekannter Modus ist
# immer Exit 2, egal ob nvidia-smi da ist.
case "$MODE" in
  boost|normal|eco|eco+|restore|status) ;;
  *) echo "Aufruf: $0 {boost|normal|eco|eco+|restore|status}" >&2; exit 2 ;;
esac
[ -x "$NVSMI" ] || { echo "nvidia-smi fehlt unter $NVSMI" >&2; exit 1; }

# Scheitert nvidia-smi, wird die Stufe NICHT gespeichert und der Aufruf
# endet mit Exit 1 — der Agent meldet das dann als last_error, statt dass
# /etc/mc-gpu-mode etwas behauptet, was die GPU gar nicht fährt.
apply() {
  if ! "$NVSMI" -lgc "0,$1" >/dev/null 2>&1; then
    echo "nvidia-smi -lgc 0,$1 fehlgeschlagen" >&2
    return 1
  fi
}
release() {
  if ! "$NVSMI" -rgc >/dev/null 2>&1; then
    echo "nvidia-smi -rgc fehlgeschlagen" >&2
    return 1
  fi
}

case "$MODE" in
  boost)  release       && echo boost  > "$STATE" || exit 1 ;;
  normal) apply 2200    && echo normal > "$STATE" || exit 1 ;;
  eco)    apply 2000    && echo eco    > "$STATE" || exit 1 ;;
  eco+)   apply 1800    && echo "eco+" > "$STATE" || exit 1 ;;
  restore)                                     # beim Systemstart: gespeicherte Stufe setzen
          M=$(cat "$STATE" 2>/dev/null || echo eco)
          case "$M" in boost|normal|eco|eco+) ;; *) M=eco ;; esac
          exec "$0" "$M" ;;
  status) ;;
esac

GESPEICHERT=$(cat "$STATE" 2>/dev/null || echo "-")
IST=$("$NVSMI" --query-gpu=clocks.gr --format=csv,noheader 2>/dev/null)
WATT=$("$NVSMI" --query-gpu=power.draw --format=csv,noheader 2>/dev/null)
TEMP=$("$NVSMI" --query-gpu=temperature.gpu --format=csv,noheader 2>/dev/null)
echo "Modus: ${GESPEICHERT} | Takt: ${IST} | ${WATT} | ${TEMP} °C"
"""),
    f"{CONTROL_SCRIPT_DIR}/latency-tune.sh": ("latency-tune.sh", 0o755, r"""#!/bin/bash
# latency-tune.sh — Latenz-Abstimmung für den TP-Verbund über Netz:
# PCIe-ASPM auf "performance" und CPU-Tiefschlaf (tiefe C-States) sperren.
#
# Der zweite Teil braucht einen HALTE-PROZESS: solange jemand
# /dev/cpu_dma_latency mit dem Wert 0 offen hält, bleibt die CPU wach. Stirbt
# der Prozess (oder die Box startet neu), fällt die Einstellung STILL zurück —
# deshalb prüft der Agent den Prozess (Kennung "cpu_dma_holder" in der
# Kommandozeile) und nicht nur die ASPM-Datei.
#
# Idempotent: ist beides schon aktiv, wird NICHTS neu gestartet (Exit 0).
# Sonst würde jeder Aufruf einen weiteren Halter erzeugen. Scheitert einer
# der beiden Schritte, endet das Skript mit Exit 1 und einer Meldung — der
# Agent zeigt sie als last_error.
#
# Wird von mc-node-agent.py installiert (root:root 0755 in /usr/local/sbin,
# nur mit --install --allow-control). Kein Rückweg-Skript: ein Neustart
# räumt beides weg.
set -u
ASPM=/sys/module/pcie_aspm/parameters/policy
DMA=/dev/cpu_dma_latency
HOLDER_TAG=cpu_dma_holder

if [ "$#" -ne 0 ]; then
  echo "abgelehnt: keine Argumente erwartet" >&2
  exit 2
fi

holder_running() {
  # Muster mit Klammer, damit der pgrep-Aufruf selbst nie auf sich passt.
  pgrep -f "cpu_dma_holde[r]" >/dev/null 2>&1
}

aspm_ok=0
if [ -w "$ASPM" ]; then
  if grep -q '\[performance\]' "$ASPM" 2>/dev/null; then
    aspm_ok=1
  elif echo performance > "$ASPM" 2>/dev/null; then
    aspm_ok=1
  fi
fi
if [ "$aspm_ok" -ne 1 ]; then
  echo "ASPM-Richtlinie konnte nicht auf performance gesetzt werden ($ASPM)" >&2
  exit 1
fi

if ! holder_running; then
  if [ ! -w "$DMA" ]; then
    echo "$DMA nicht beschreibbar — C-State-Sperre unmöglich" >&2
    exit 1
  fi
  # Halter: eigene Sitzung (setsid), kein Terminal, überlebt das Ende dieses
  # Skripts. Die Zeile "# cpu_dma_holder" im Python-Code ist die Kennung,
  # nach der Agent und pgrep suchen.
  setsid nohup python3 -c "
import struct, time
f = open('$DMA', 'wb', buffering=0)
f.write(struct.pack('i', 0))
# $HOLDER_TAG
while True: time.sleep(3600)
" > /dev/null 2>&1 < /dev/null &
  sleep 1
  if ! holder_running; then
    echo "Halte-Prozess ($HOLDER_TAG) ist nicht gestartet" >&2
    exit 1
  fi
fi

echo "ASPM: $(cat "$ASPM") | Halter läuft"
"""),
    f"{CONTROL_SCRIPT_DIR}/mc-set-min-free.sh": ("mc-set-min-free.sh", 0o755, r"""#!/bin/bash
# mc-set-min-free.sh <kbytes> — Speicher-Reserve (vm.min_free_kbytes) setzen.
#
# Warum ein Wrapper statt `sysctl -w vm.min_free_kbytes=*` direkt in sudoers:
# ein `*` in einer sudoers-Regel passt laut `man sudoers` auch ÜBER
# Wortgrenzen. Die Regel liesse also auch
#     sysctl -w vm.min_free_kbytes=65536 kernel.core_pattern=|/tmp/x
# durch — und das wäre root. Dieser Wrapper ist deshalb die eigentliche
# Argument-Grenze: GENAU ein Argument, nur Ziffern, nur im erlaubten Bereich.
# Alles andere endet mit Exit 2, bevor irgendetwas ausgeführt wird.
#
# Wird von mc-node-agent.py installiert (root:root 0755 in /usr/local/sbin,
# nur mit --install --allow-control). Die Grenzen sind absichtlich IDENTISCH
# zu MIN_FREE_KBYTES_MIN/MAX im Agenten und MIN_FREE_KBYTES_RANGE im Backend.
set -u

MIN=65536       # 64 MB — darunter tut der Kernel nichts Sinnvolles mehr
MAX=16777216    # 16 GB — mehr Reserve macht die Box unbenutzbar

if [ "$#" -ne 1 ]; then
  echo "abgelehnt: genau ein Argument erwartet (kbytes), bekommen: $#" >&2
  exit 2
fi
VALUE="$1"
# Nur Ziffern, 5-8 Stellen (65536 hat 5, 16777216 hat 8). Kein Vorzeichen,
# kein Leerzeichen, kein Gleichheitszeichen, kein zweiter Schlüssel.
case "$VALUE" in
  ''|*[!0-9]*) echo "abgelehnt: '$VALUE' ist keine ganze Zahl" >&2; exit 2 ;;
esac
if [ "${#VALUE}" -gt 8 ] || [ "$VALUE" -lt "$MIN" ] || [ "$VALUE" -gt "$MAX" ]; then
  echo "abgelehnt: $VALUE ausserhalb von $MIN..$MAX" >&2
  exit 2
fi

SYSCTL=/usr/sbin/sysctl
[ -x "$SYSCTL" ] || SYSCTL=/sbin/sysctl

# Nur für Tests ohne root (sudo setzt die Umgebung zurück, env_reset — im
# echten Betrieb kommt diese Variable nie hier an).
if [ "${MC_DEVICE_DRY_RUN:-}" = "1" ]; then
  echo "DRY-RUN: $SYSCTL -w vm.min_free_kbytes=$VALUE"
  exit 0
fi
exec "$SYSCTL" -w "vm.min_free_kbytes=$VALUE"
"""),
    f"{CONTROL_SCRIPT_DIR}/mc-set-mtu.sh": ("mc-set-mtu.sh", 0o755, r"""#!/bin/bash
# mc-set-mtu.sh <schnittstelle> <mtu> — MTU einer Netz-Schnittstelle setzen.
#
# Warum ein Wrapper statt `ip link set * mtu *` direkt in sudoers: die zwei
# `*` passen laut `man sudoers` über Wortgrenzen hinweg, die Regel liesse also
#     ip link set eth0 down mtu 1500      (Schnittstelle aus)
#     ip link set eth0 netns 1 mtu 1500   (Schnittstelle in anderen Namensraum)
# durch. Dieser Wrapper ist deshalb die eigentliche Argument-Grenze: GENAU
# zwei Argumente, Schnittstelle nur aus dem erlaubten Zeichensatz UND in
# /sys/class/net vorhanden, MTU nur Ziffern im erlaubten Bereich. Alles
# andere endet mit Exit 2, bevor irgendetwas ausgeführt wird.
#
# Wird von mc-node-agent.py installiert (root:root 0755 in /usr/local/sbin,
# nur mit --install --allow-control). Die Grenzen sind absichtlich IDENTISCH
# zu MTU_MIN/MAX im Agenten und MTU_RANGE im Backend.
set -u

MIN=1500   # Ethernet-Standard — kleiner bremst nur und bricht Verbund-Traffic
MAX=9000   # Jumbo-Frames

if [ "$#" -ne 2 ]; then
  echo "abgelehnt: genau zwei Argumente erwartet (schnittstelle mtu), bekommen: $#" >&2
  exit 2
fi
IFACE="$1"
MTU="$2"

# Schnittstellenname: Linux erlaubt max. 15 Zeichen (IFNAMSIZ-1). Erlaubt
# sind nur Buchstaben, Ziffern, '-', '_' und '.' — kein '/', kein Leerzeichen,
# kein Optionsstrich am Anfang (sonst würde "-h" o.ä. als Option gelesen).
case "$IFACE" in
  ''|-*|*[!A-Za-z0-9._-]*) echo "abgelehnt: ungültiger Schnittstellenname '$IFACE'" >&2; exit 2 ;;
esac
if [ "${#IFACE}" -gt 15 ] || [ "$IFACE" = "." ] || [ "$IFACE" = ".." ]; then
  echo "abgelehnt: ungültiger Schnittstellenname '$IFACE'" >&2
  exit 2
fi
if [ ! -e "/sys/class/net/$IFACE" ] && [ "${MC_DEVICE_DRY_RUN:-}" != "1" ]; then
  echo "abgelehnt: Schnittstelle '$IFACE' gibt es nicht" >&2
  exit 2
fi

case "$MTU" in
  ''|*[!0-9]*) echo "abgelehnt: '$MTU' ist keine ganze Zahl" >&2; exit 2 ;;
esac
if [ "${#MTU}" -gt 4 ] || [ "$MTU" -lt "$MIN" ] || [ "$MTU" -gt "$MAX" ]; then
  echo "abgelehnt: MTU $MTU ausserhalb von $MIN..$MAX" >&2
  exit 2
fi

IP=/usr/sbin/ip
[ -x "$IP" ] || IP=/sbin/ip
[ -x "$IP" ] || IP=/usr/bin/ip

# Nur für Tests ohne root (sudo setzt die Umgebung zurück, env_reset — im
# echten Betrieb kommt diese Variable nie hier an).
if [ "${MC_DEVICE_DRY_RUN:-}" = "1" ]; then
  echo "DRY-RUN: $IP link set dev $IFACE mtu $MTU"
  exit 0
fi
# `dev` explizit: damit der Name nie als Schlüsselwort (up/down/netns…)
# gelesen werden kann, selbst wenn eine Schnittstelle so hiesse.
exec "$IP" link set dev "$IFACE" mtu "$MTU"
"""),
    str(CLOCK_CAP_UNIT_PATH): ("gb10-clock-cap.service", 0o644, r"""[Unit]
Description=GB10 GPU-Modus (gespeicherte Stufe setzen: boost/normal/eco/eco+)
After=nvidia-persistenced.service
Wants=nvidia-persistenced.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/mc-gpu-mode.sh restore
ExecStop=/usr/bin/nvidia-smi -rgc

[Install]
WantedBy=multi-user.target
"""),
}
# <<< CONTROL_FILES


def install_control_scripts() -> None:
    """Kopiert die eingebetteten Steuer-Skripte root-eigen nach
    /usr/local/sbin (0755) und die Takt-Deckel-Unit nach /etc/systemd/system
    — nur mit --install --allow-control (Review H3).

    Warum root-eigen und nicht für den Dienst-Benutzer schreibbar: die
    Skripte stehen in sudoers. Wer sie ändern kann, ist root. Geschrieben
    wird über eine `.tmp`-Datei im selben (root-eigenen) Verzeichnis mit
    O_EXCL|O_NOFOLLOW und dann atomar umbenannt — ein halb geschriebenes
    Skript kann so nie kurz als root laufen.

    Die Unit wird nur aktiviert (`enable`), nicht gestartet: ein Takt-Wechsel
    ist eine Entscheidung des Betreibers über MC, kein Nebeneffekt einer
    Installation. Beim nächsten Systemstart stellt sie die gespeicherte Stufe
    wieder her.
    """
    if os.geteuid() != 0:
        log.error("Steuer-Skripte installieren braucht root.")
        raise SystemExit(1)
    unit_written = False
    for target, (name, mode, content) in CONTROL_FILES.items():
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        tmp = path.with_name(path.name + ".tmp")
        try:
            if tmp.is_symlink() or tmp.exists():
                tmp.unlink()
            _write_new_file_nofollow(tmp, content, mode)
            os.chown(tmp, 0, 0)
            os.replace(tmp, path)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            log.error("Steuer-Skript %s konnte nicht installiert werden: %s", path, e)
            raise SystemExit(1)
        if path == CLOCK_CAP_UNIT_PATH:
            unit_written = True
    log.info("Steuer-Skripte installiert (root:root): %s", ", ".join(CONTROL_FILES))
    if unit_written:
        try:
            subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=SET_CMD_TIMEOUT_S)
            subprocess.run(
                ["systemctl", "enable", CLOCK_CAP_UNIT_PATH.name],
                check=True, timeout=SET_CMD_TIMEOUT_S,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            # Kein Abbruch: die Skripte sind da, nur die Boot-Wiederherstellung
            # fehlt — das ist ein Hinweis, kein Grund, die Steuerung zu verweigern.
            log.warning("%s konnte nicht aktiviert werden: %s", CLOCK_CAP_UNIT_PATH.name, e)


def install_systemd_unit(mc_url: str, run_as_user: str, allow_control: bool = False) -> None:
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
    unit = render_unit(
        mc_url=mc_url,
        user=run_as_user,
        python=sys.executable,
        script=str(script_path),
        allow_control=allow_control,
    )
    SYSTEMD_UNIT_PATH.write_text(unit, encoding="utf-8")
    os.chmod(SYSTEMD_UNIT_PATH, 0o644)
    log.info("systemd-Unit geschrieben: %s", SYSTEMD_UNIT_PATH)

    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "mc-node-agent.service"], check=True)
    # `restart` statt `enable --now`: läuft der Dienst schon (erneute
    # Installation, etwa um --allow-control ein- oder auszuschalten), würde
    # `--now` ihn NICHT neu starten — die geänderte Unit (NoNewPrivileges,
    # Speichergrenze) bliebe bis zum nächsten Neustart wirkungslos. restart
    # startet einen gestoppten Dienst genauso wie `start`.
    subprocess.run(["systemctl", "restart", "mc-node-agent.service"], check=True)
    log.info("mc-node-agent.service aktiviert + (neu) gestartet (User=%s).", run_as_user)


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

    Geräte-Steuerung (01.09.2026, docs/plans/2026-09-01-geraete-steuerung-
    vertrag.md): jede Runde legt den Ist-Zustand in den Heartbeat und wendet
    danach den `desired_state` aus der Antwort an — nur, was abweicht. Der
    Abgleich läuft bei JEDER Runde, deshalb überlebt eine Einstellung auch
    einen Neustart der Box von selbst. Eine Antwort ohne `desired_state`
    (altes Backend) bedeutet schlicht: nichts tun. Nach einem erfolgreichen
    Setzen werden die zwischengespeicherten Messwerte sofort für ungültig
    erklärt, damit die nächste Runde den echten neuen Zustand meldet und
    nicht denselben Befehl noch einmal absetzt.
    """
    backoff = HEARTBEAT_INTERVAL_S
    inventory_paths = default_inventory_paths()
    heartbeat_count = 0
    last_sent_fingerprint: str | None = None
    cached_scan: tuple[str, list[dict]] | None = None
    scan_due = True  # scan once, unconditionally, at startup
    cached_gpu: dict | None = None
    cached_gpu_device: dict | None = None
    cached_oom_guard: str | None = None
    gpu_poll_due = True  # poll once, unconditionally, at startup
    apply_status: dict = {"applied_at": None, "last_error": None}
    tracker = _ApplyTracker()

    while True:
        try:
            if gpu_poll_due:
                smi_stdout = _run_nvidia_smi()
                cached_gpu = parse_nvidia_smi(smi_stdout)
                cached_gpu_device = parse_nvidia_smi_device(smi_stdout)
                cached_oom_guard = read_oom_guard()
                gpu_poll_due = False

            telemetry = collect_telemetry(gpu_fields=cached_gpu)

            try:
                device_state = collect_device_state(
                    gpu_fields=cached_gpu_device,
                    oom_guard=cached_oom_guard,
                    apply_status=apply_status,
                )
            except Exception as e:  # noqa: BLE001 — ein kaputter Ist-Zustand
                # darf den Heartbeat nie mitreissen (siehe Moduldoc).
                log.warning("Geräte-Zustand nicht lesbar (%s) — Heartbeat geht ohne raus", e)
                device_state = None

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

            response = send_heartbeat(
                mc_url, token, telemetry,
                inventory=inventory_to_send,
                device_state=device_state,
            )

            if device_state is not None:
                desired = (response or {}).get("desired_state")
                if isinstance(desired, dict) and desired:
                    try:
                        # Rundenanfang mit dem FRISCHEN Ist: welche Felder
                        # weichen noch ab? Daraus lernt der Tracker, ob ein
                        # Setz-Befehl der letzten Runde gewirkt hat (M1).
                        pending = {
                            field: " ".join(argv)
                            for field, argv in plan_desired_state(desired, device_state)[0]
                        }
                        tracker.begin_round(pending)
                        changed, errors = apply_desired_state(desired, device_state, tracker)
                    except Exception as e:  # noqa: BLE001 — NIE crashen
                        changed, errors = False, [f"Setzen abgebrochen: {e}"]
                    if errors:
                        for err in errors:
                            log.warning("Soll-Zustand: %s", err)
                        apply_status["last_error"] = "; ".join(errors)[:500]
                    else:
                        apply_status["last_error"] = None
                    if changed:
                        apply_status["applied_at"] = datetime.now(timezone.utc).isoformat()
                        # Zwischenspeicher verwerfen: sonst vergliche die
                        # nächste Runde gegen den alten Ist-Zustand und
                        # setzte denselben Wert nochmals.
                        gpu_poll_due = True
                else:
                    # Review M2: MC hat den Soll gelöscht (oder nie gesetzt)
                    # — ein alter Fehler darf dann nicht als rote Ampel
                    # kleben bleiben. Es gibt nichts mehr, das scheitern kann.
                    if apply_status["last_error"] is not None:
                        log.info("Kein Soll-Zustand mehr — letzter Fehler zurückgesetzt.")
                    apply_status["last_error"] = None
                    tracker.reset()

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

_USAGE = """usage: mc-node-agent.py [-h] [--mc-url URL] [--pair CODE] [--install]
                        [--allow-control] [--user USER]

Mission Control Node Agent — push telemetry (Fleet & Rezepte v2, Phase 1).

  --mc-url URL   Mission-Control-Basis-URL, z.B. https://mc.tailnet-name.ts.net
                 (oder Env MC_URL)
  --pair CODE    Pairing-Code einlösen (aus POST /api/v1/nodes/pairing-codes)
                 und Token speichern
  --install      Als systemd-Dienst installieren — braucht sudo/root.
                 Ohne --allow-control nimmt es eine frühere Freischaltung
                 zurück (löscht /etc/sudoers.d/mc-node-agent).
  --allow-control
                 Steuerung freischalten: kopiert die Steuer-Skripte root-eigen
                 nach /usr/local/sbin und schreibt /etc/sudoers.d/mc-node-agent
                 mit GENAU den fünf MC-Aktionen (GPU-Modus, min_free_kbytes,
                 earlyoom, latency-tune, MTU) und sonst nichts. Ohne diesen
                 Schalter meldet der Agent nur und setzt nie etwas.
                 Braucht sudo/root.
  --user USER    systemd User= für --install (Default: $SUDO_USER, sonst der
                 aktuelle User)
  -h, --help     Diese Hilfe anzeigen
"""


class _Args:
    __slots__ = ("mc_url", "pair", "install", "allow_control", "user")

    def __init__(self) -> None:
        self.mc_url: str | None = os.environ.get("MC_URL")
        self.pair: str | None = None
        self.install: bool = False
        # Standard ist AUS: Steuerung ist eine bewusste Entscheidung des
        # Betreibers, kein Nebeneffekt einer Installation.
        self.allow_control: bool = False
        self.user: str | None = None


def _arg_error(msg: str) -> "None":
    print(f"mc-node-agent.py: error: {msg}", file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    raise SystemExit(2)


def parse_args(argv: list[str] | None = None) -> _Args:
    """Manual parser for this agent's 5 flags — only the space-separated
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
        elif arg == "--allow-control":
            args.allow_control = True
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

    if args.install or args.allow_control:
        run_as_user = args.user or _default_install_user()
        if args.install:
            try:
                install_systemd_unit(mc_url, run_as_user, allow_control=args.allow_control)
            except subprocess.CalledProcessError as e:
                log.error("systemctl-Aufruf fehlgeschlagen: %s", e)
                return 1
        # Bewusst NACH dem Dienst: scheitert die sudoers-Regel, läuft der
        # Agent trotzdem und meldet weiter — nur setzen kann er dann nicht.
        # Umgekehrt wäre eine gescheiterte Regel ein Totalausfall.
        if args.allow_control:
            try:
                # Erst die Skripte (root-eigen), dann die Regel, die sie
                # erlaubt — eine Regel auf ein noch fehlendes Skript wäre
                # zwar harmlos, aber der Agent meldete bis dahin Fehler.
                install_control_scripts()
                install_sudoers(run_as_user)
            except SystemExit:
                # Review M3: Steuerung konnte nicht freigeschaltet werden.
                # Die Unit steht aber schon auf "Steuer-Betrieb"
                # (NoNewPrivileges=no, 96 MB). Diese Lockerung ohne die
                # sudoers-Regel wäre Lockerung ohne Nutzen — deshalb zurück
                # auf die strenge Melde-Unit, und ehrlich mit Exit 1 enden.
                remove_sudoers()
                if args.install:
                    log.error(
                        "Steuerung NICHT freigeschaltet — Dienst wird auf reinen "
                        "Melde-Betrieb zurückgesetzt."
                    )
                    try:
                        install_systemd_unit(mc_url, run_as_user, allow_control=False)
                    except subprocess.CalledProcessError as e:
                        log.error("systemctl-Aufruf beim Zurücksetzen fehlgeschlagen: %s", e)
                return 1
        else:
            # Review M3: der Rückweg. `--install` ohne das Flag nimmt eine
            # frühere Freischaltung wieder zurück — sonst gäbe es keinen
            # Weg zurück ausser Handarbeit in /etc/sudoers.d.
            if args.install:
                remove_sudoers()
            log.info(
                "Steuerung NICHT freigeschaltet (Standard). Der Agent meldet den "
                "Gerätezustand, setzt aber nichts. Zum Freischalten: erneut mit "
                "--install --allow-control ausführen."
            )
        return 0

    if not token:
        log.error("Kein Token — entweder --pair CODE angeben, MC_NODE_TOKEN setzen, oder vorher pairen.")
        return 1

    log.info("mc-node-agent v%s startet, Ziel=%s", AGENT_VERSION, mc_url)
    run_loop(mc_url, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
