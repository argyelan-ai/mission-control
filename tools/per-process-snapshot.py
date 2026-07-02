#!/usr/bin/env python3
"""Per-Process Memory Snapshot for Mission Control (MEM-02).

Combines:
  1) live `docker exec ps` per running mc-agent-* container
  2) live host `ps` for Boss (host-runtime claude/openclaude/bun/node)
  3) historical 24h trend from ~/Library/Logs/openclaw-memlog/snapshots-*.csv
  4) historical 48h container-level trend from ~/.mc/memory-samples.csv
into a single Markdown report at .planning/notes/memory-baseline.md.

Naming the dominant per-process RAM growth contributor is the Phase 2
deliverable (Roadmap Success Criterion 1) and the gating diagnostic for
Phase 3 (memory leak fix).

Re-runnable — every invocation overwrites the report. History lives in CSVs.

Nur stdlib — kein pip install noetig.
Ausfuehren: python3 tools/per-process-snapshot.py
"""
from __future__ import annotations

import csv
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = PROJECT_ROOT / ".planning" / "notes" / "memory-baseline.md"

HOME = Path(os.environ.get("HOME", str(Path.home())))
MEMLOG_DIR = HOME / "Library" / "Logs" / "openclaw-memlog"
CONTAINER_CSV = HOME / ".mc" / "memory-samples.csv"

NOW = datetime.now(timezone.utc)
WINDOW_24H = timedelta(hours=24)
WINDOW_48H = timedelta(hours=48)
TOP_N = 10


def run(cmd: list[str], timeout: int = 10) -> str:
    """Run subprocess, return stdout text. Empty string on failure."""
    try:
        return subprocess.check_output(cmd, text=True, timeout=timeout, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def list_mc_agents() -> list[str]:
    out = run(["docker", "ps", "--filter", "name=mc-agent", "--format", "{{.Names}}"])
    return [n.strip() for n in out.splitlines() if n.strip()]


def snapshot_container(name: str) -> list[tuple[int, int, str]]:
    """Return list of (pid, rss_kb, cmd) for top processes in container."""
    raw = run(["docker", "exec", name, "ps", "-eo", "pid,rss,cmd", "--sort=-rss"])
    rows: list[tuple[int, int, str]] = []
    for line in raw.splitlines()[1:]:  # skip header
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            rss = int(parts[1])
        except ValueError:
            continue
        cmd = parts[2][:80]
        rows.append((pid, rss, cmd))
        if len(rows) >= TOP_N:
            break
    return rows


def snapshot_host_boss() -> list[tuple[int, int, str]]:
    """Capture Boss host-runtime processes (claude/openclaude/bun/node)."""
    raw = run(["ps", "-axo", "pid,rss,command"])
    rows: list[tuple[int, int, str]] = []
    keywords = ("claude", "openclaude", "bun", "node")
    for line in raw.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            rss = int(parts[1])
        except ValueError:
            continue
        cmd = parts[2]
        if not any(k in cmd for k in keywords):
            continue
        rows.append((pid, rss, cmd[:80]))
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:TOP_N]


def _restart_aware_growth(values: list[int]) -> int:
    """Sum of monotonic growth segments — each container restart (sample drops
    to a noticeably lower value) starts a new segment, and we add up the
    within-segment growth (last - first per segment).

    A leak that's masked by periodic restarts ("RAM climbs to 900 MB, container
    restarts, climbs again") shows up here as a large positive number even
    though the simple last-minus-first delta is near zero or negative.
    """
    if len(values) < 2:
        return 0
    total = 0
    seg_start = values[0]
    prev = values[0]
    for v in values[1:]:
        # Heuristic for restart: drop > 50 MB AND drop > 30% of prev
        if prev - v > 50 and prev > 0 and (prev - v) / prev > 0.3:
            total += prev - seg_start
            seg_start = v
        prev = v
    total += prev - seg_start
    return total


def read_per_process_trend() -> dict[str, dict[str, int]]:
    """Compute Δ MB over last 24h per container per process kind.

    For each (container, kind) we record:
      - delta:  last - first  (raw net change; can be negative if container
                restarted within window)
      - peak:   max RSS seen  (the leak signature — "claude climbs to 900 MB
                before forced restart")
      - growth: restart-aware sum of monotonic growth segments  (true total
                bytes leaked across the window, even with mid-window restarts)

    The Verdict line uses `growth` (not `delta`) — the project's documented
    leak signature is peak-before-restart, not net 24h drift. See
    Memory note `project_container_memory_leak.md` (2026-04-15: claude 300-900
    MB/Agent, bun 80 MB/Agent — leak is in claude, masked by restarts).

    Returns: {container: {claude/bun/node delta+peak+growth, samples: int}}
    Empty dict if no CSVs found.
    """
    by_container: dict[str, list[dict]] = defaultdict(list)
    cutoff = NOW - WINDOW_24H
    if not MEMLOG_DIR.exists():
        return {}
    for csv_file in sorted(MEMLOG_DIR.glob("snapshots-*.csv")):
        try:
            with csv_file.open() as f:
                for r in csv.DictReader(f):
                    try:
                        ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
                    except (KeyError, ValueError):
                        continue
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        continue
                    for k in ("total_mb", "claude_mb", "bun_mb", "node_mb"):
                        try:
                            r[k] = int(float(r.get(k) or 0))
                        except ValueError:
                            r[k] = 0
                    r["ts"] = ts
                    by_container[r["container"]].append(r)
        except OSError:
            continue
    out: dict[str, dict[str, int]] = {}
    for c, snaps in by_container.items():
        snaps.sort(key=lambda x: x["ts"])
        if len(snaps) < 2:
            continue
        first, last = snaps[0], snaps[-1]
        per_kind: dict[str, int] = {}
        for kind, csv_key in (("claude", "claude_mb"), ("bun", "bun_mb"), ("node", "node_mb")):
            series = [s[csv_key] for s in snaps]
            per_kind[f"{kind}_delta"]  = last[csv_key] - first[csv_key]
            per_kind[f"{kind}_peak"]   = max(series)
            per_kind[f"{kind}_growth"] = _restart_aware_growth(series)
        per_kind["samples"] = len(snaps)
        out[c] = per_kind
    return out


def read_container_trend() -> list[dict]:
    """48h container-level mem_pct delta from ~/.mc/memory-samples.csv."""
    if not CONTAINER_CSV.exists():
        return []
    cutoff = NOW - WINDOW_48H
    by_container: dict[str, list[dict]] = defaultdict(list)
    with CONTAINER_CSV.open() as f:
        for r in csv.DictReader(f):
            try:
                ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
            try:
                # mem_pct in CSV is e.g. "0.83%" — strip the %
                pct_raw = (r.get("mem_pct") or "0").rstrip("%")
                r["mem_pct"] = float(pct_raw)
            except ValueError:
                continue
            r["ts"] = ts
            by_container[r["container"]].append(r)
    rows = []
    for c, snaps in by_container.items():
        snaps.sort(key=lambda x: x["ts"])
        if len(snaps) < 2:
            continue
        rows.append({
            "container": c,
            "first": snaps[0]["mem_pct"],
            "last": snaps[-1]["mem_pct"],
            "delta": snaps[-1]["mem_pct"] - snaps[0]["mem_pct"],
        })
    rows.sort(key=lambda x: x["delta"], reverse=True)
    return rows


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_(no data)_\n"
    out = "| " + " | ".join(headers) + " |\n"
    out += "|" + "|".join(["---"] * len(headers)) + "|\n"
    for r in rows:
        out += "| " + " | ".join(str(x) for x in r) + " |\n"
    return out


def render_report(
    container_snapshots: dict[str, list[tuple[int, int, str]]],
    host_snapshot: list[tuple[int, int, str]],
    per_process_trend: dict[str, dict[str, int]],
    container_trend: list[dict],
) -> str:
    out = [f"# Memory Baseline — {NOW.strftime('%Y-%m-%d %H:%M')} UTC\n"]
    out.append("Generated by `tools/per-process-snapshot.py` (MEM-02).\n")

    out.append("## Per-Container Process Top-10 (live snapshot)\n")
    if not container_snapshots:
        out.append("_(no running mc-agent-* containers found)_\n\n")
    for name, rows in sorted(container_snapshots.items()):
        out.append(f"### {name}\n")
        table_rows = [[pid, f"{rss/1024:.1f}", cmd] for pid, rss, cmd in rows]
        out.append(md_table(["PID", "RSS (MB)", "Command"], table_rows))
        out.append("")

    out.append("## Boss (host-runtime) processes\n")
    table_rows = [[pid, f"{rss/1024:.1f}", cmd] for pid, rss, cmd in host_snapshot]
    out.append(md_table(["PID", "RSS (MB)", "Command"], table_rows))
    out.append("")

    out.append("## 24h trend per process — net delta (Δ MB)\n")
    out.append("Raw `last − first` per kind. Negative values usually mean a "
               "container restart trimmed RAM mid-window — see the Peak and "
               "Restart-aware Growth tables below for the leak signature.\n")
    if not per_process_trend:
        out.append(f"_(no CSV data at {MEMLOG_DIR} — sampler may not be running)_\n\n")
    else:
        rows = []
        for c, d in sorted(per_process_trend.items()):
            rows.append([c,
                         f"{d['claude_delta']:+d}",
                         f"{d['bun_delta']:+d}",
                         f"{d['node_delta']:+d}",
                         d["samples"]])
        out.append(md_table(["container", "claude Δ", "bun Δ", "node Δ", "samples"], rows))
        out.append("")

    out.append("## 24h peak RSS per process (MB)\n")
    out.append("Highest RSS observed in the window. The leak signature is "
               "`peak ≫ live` — RAM grew toward the cgroup limit, then "
               "container restart reset it.\n")
    if per_process_trend:
        rows = []
        for c, d in sorted(per_process_trend.items()):
            rows.append([c,
                         d['claude_peak'],
                         d['bun_peak'],
                         d['node_peak']])
        out.append(md_table(["container", "claude peak", "bun peak", "node peak"], rows))
        out.append("")

    out.append("## 24h restart-aware growth per process (Σ MB)\n")
    out.append("Sum of within-segment growth between detected container "
               "restarts (RSS drop > 50 MB AND > 30 % of prev). Captures "
               "leakage that net-delta hides.\n")
    if per_process_trend:
        rows = []
        for c, d in sorted(per_process_trend.items()):
            rows.append([c,
                         f"{d['claude_growth']:+d}",
                         f"{d['bun_growth']:+d}",
                         f"{d['node_growth']:+d}"])
        out.append(md_table(["container", "claude growth", "bun growth", "node growth"], rows))
        out.append("")

    out.append("## 48h container-level trend (mem %)\n")
    if not container_trend:
        out.append(f"_(no CSV data at {CONTAINER_CSV})_\n\n")
    else:
        rows = [[d["container"], f"{d['first']:.1f}", f"{d['last']:.1f}", f"{d['delta']:+.1f}"]
                for d in container_trend]
        out.append(md_table(["container", "first %", "last %", "Δ %"], rows))
        out.append("")

    out.append("## Verdict\n")
    if per_process_trend:
        # Sum restart-aware GROWTH per kind across all containers.
        # This is the metric that survives mid-window container restarts.
        growth_totals = {"claude": 0, "bun": 0, "node": 0}
        peak_totals   = {"claude": 0, "bun": 0, "node": 0}
        for d in per_process_trend.values():
            for k in ("claude", "bun", "node"):
                growth_totals[k] += d[f"{k}_growth"]
                peak_totals[k]   += d[f"{k}_peak"]
        kind, total = max(growth_totals.items(), key=lambda x: x[1])
        n = len(per_process_trend)
        out.append(
            f"**Dominant 24h growth contributor across {n} containers: "
            f"{kind} (Σ {total:+d} MB restart-aware growth, "
            f"Σ {peak_totals[kind]} MB summed peaks)**\n"
        )
        out.append("")
        out.append("Backing data:\n")
        for k in ("claude", "bun", "node"):
            out.append(f"- {k}: Σ growth = {growth_totals[k]:+d} MB, "
                       f"Σ peak = {peak_totals[k]} MB")
    else:
        out.append("**Dominant: insufficient CSV data — re-run after sampler accumulates ≥2 snapshots.**\n")
    return "\n".join(out)


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    agents = list_mc_agents()
    container_snapshots = {a: snapshot_container(a) for a in agents}
    host_snapshot = snapshot_host_boss()
    per_process_trend = read_per_process_trend()
    container_trend = read_container_trend()
    report = render_report(container_snapshots, host_snapshot, per_process_trend, container_trend)
    OUTPUT.write_text(report, encoding="utf-8")
    print(f"Wrote {OUTPUT} ({len(report)} chars)", flush=True)
    print(f"  Containers seen: {len(container_snapshots)}", flush=True)
    print(f"  Host-Boss procs: {len(host_snapshot)}", flush=True)
    print(f"  Per-process trend rows: {len(per_process_trend)}", flush=True)
    print(f"  Container-level trend rows: {len(container_trend)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
