"""Read a head run from its folder (host → MC contract, spec §6.1 / §6.5).

Trust rules:
- ``.wrapper/status.json`` + ``heartbeat`` are written only by the mc-head
  wrapper (the head cannot write ``.wrapper/`` inside the sandbox). The PR URL
  is taken from there and nowhere else.
- A run record counts only when its frontmatter ``head_run`` equals the run
  id AND its mtime lies inside the run window (vault ``jobs/`` is writable by
  other containers today).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter

from app.services.heads import paths

MAX_TEXT = 20_000


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_text(path: Path, limit: int = MAX_TEXT) -> str | None:
    try:
        return path.read_text(errors="replace")[:limit]
    except OSError:
        return None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def parse_ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


@dataclass
class HeadRun:
    run_id: str
    folder: Path
    spec: dict
    status: dict
    heartbeat_mtime: float | None
    log_mtime: float | None
    step: str | None
    question: str | None
    stop_requested: bool
    mirror: dict
    run_record_path: Path | None = None
    run_record_text: str | None = None
    run_record_passed: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def task_id(self) -> str | None:
        tid = self.spec.get("task_id")
        return tid if isinstance(tid, str) else None

    @property
    def created_ts(self) -> float:
        return parse_ts(self.spec.get("created_at")) or 0.0

    @property
    def pr_url(self) -> str | None:
        url = self.status.get("pr_url")
        return url if isinstance(url, str) and url.startswith("https://github.com/") else None


def _run_record(run_id: str, status: dict) -> tuple[Path | None, str | None, bool]:
    raw = status.get("run_record_path")
    if not isinstance(raw, str) or not raw:
        return None, None, False
    jobs = paths.vault_jobs_dir().resolve()
    try:
        path = Path(raw).resolve()
    except OSError:
        return None, None, False
    if jobs not in path.parents:
        return None, None, False
    text = _read_text(path, 200_000)
    mtime = _mtime(path)
    if text is None or mtime is None:
        return None, None, False
    try:
        post = frontmatter.loads(text)
    except Exception:  # noqa: BLE001 — broken YAML = not a valid record
        return path, text, False
    if str(post.metadata.get("head_run") or "") != run_id:
        return None, None, False
    started = parse_ts(status.get("started_at"))
    ended = parse_ts(status.get("exited_at")) or time.time()
    if started is None or not (started - 5 <= mtime <= ended + 60):
        return None, None, False
    passed = any(line.strip().lower() == "status: passed" for line in post.content.splitlines())
    return path, text, passed


def load_run(run_id: str) -> HeadRun | None:
    try:
        folder = paths.run_dir(run_id)
    except ValueError:
        return None
    spec = _read_json(folder / "spec.json")
    if not spec or spec.get("run_id") != run_id:
        return None
    wrapper = folder / ".wrapper"
    status = _read_json(wrapper / "status.json")
    rr_path, rr_text, rr_passed = _run_record(run_id, status)
    step = _read_text(folder / "step.txt", 500)
    return HeadRun(
        run_id=run_id,
        folder=folder,
        spec=spec,
        status=status,
        heartbeat_mtime=_mtime(wrapper / "heartbeat"),
        log_mtime=_mtime(folder / "head.log"),
        step=step.strip().splitlines()[0] if step and step.strip() else None,
        question=_read_text(folder / "question.md", 4000),
        stop_requested=(wrapper / "stop-requested").exists() or (folder / ".backend" / "stop-requested").exists(),
        mirror=_read_json(folder / ".backend" / "mirror.json"),
        run_record_path=rr_path,
        run_record_text=rr_text,
        run_record_passed=rr_passed,
    )


def list_runs() -> list[HeadRun]:
    root = paths.heads_root()
    if not root.is_dir():
        return []
    runs = []
    for spec_path in root.glob("*/spec.json"):
        run_id = spec_path.parent.name
        if not paths.RUN_ID_RE.match(run_id):
            continue
        run = load_run(run_id)
        if run is not None:
            runs.append(run)
    runs.sort(key=lambda r: r.created_ts)
    return runs


def write_backend_file(run_id: str, name: str, data: dict) -> None:
    folder = paths.run_dir(run_id) / ".backend"
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{name}.tmp"
    tmp.write_text(json.dumps(data))
    tmp.replace(folder / name)
