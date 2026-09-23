"""Fixtures for the head launcher backend tests: fake run folders on disk."""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

BOX = str(uuid.UUID(int=0xB0C5))


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def heads_root(tmp_path, monkeypatch):
    from app.config import settings

    root = tmp_path / "heads"
    root.mkdir()
    vault = tmp_path / "vault"
    (vault / "jobs").mkdir(parents=True)
    monkeypatch.setattr(settings, "heads_root", root)
    monkeypatch.setattr(settings, "vault_path", vault)
    monkeypatch.setattr(settings, "heads_enabled", True)
    return root


def make_run(
    root: Path,
    *,
    run_id: str | None = None,
    task_id: str | None = None,
    status: dict | None = None,
    heartbeat_age: float | None = None,
    created_ago: float = 30,
    question: str | None = None,
    **spec_over,
) -> str:
    run_id = run_id or str(uuid.uuid4())
    folder = root / run_id
    (folder / ".wrapper").mkdir(parents=True)
    now = time.time()
    spec = {
        "run_id": run_id,
        "task_id": task_id,
        "title": "Fix the thing",
        "repo_full_name": "owner/demo",
        "base_branch": "main",
        "branch": f"head/2026-09-23-fix-{run_id[:4]}",
        "harness": "omp",
        "runtime_slug": "local-slot",
        "model": "glm",
        "base_url": "http://127.0.0.1:8000/v1",
        "box_keys": [BOX],
        "time_limit_s": 7200,
        "restarted_from": None,
        "mode": "fresh",
        "created_at": iso(now - created_ago),
    }
    spec.update(spec_over)
    (folder / "spec.json").write_text(json.dumps(spec))
    if status is not None:
        (folder / ".wrapper" / "status.json").write_text(json.dumps({"run_id": run_id, **status}))
    if heartbeat_age is not None:
        hb = folder / ".wrapper" / "heartbeat"
        hb.touch()
        os.utime(hb, (now - heartbeat_age, now - heartbeat_age))
    if question is not None:
        (folder / "question.md").write_text(question)
    return run_id


def write_run_record(root: Path, run_id: str, *, passed: bool = True, mtime: float | None = None) -> Path:
    from app.config import settings

    job = Path(settings.vault_path) / "jobs" / f"2026-09-23-fix-{run_id[:4]}"
    job.mkdir(parents=True, exist_ok=True)
    path = job / "run-record.md"
    path.write_text(
        "---\nid: job-x\ntype: run-record\nagent: head\ndate: 2026-09-23\n"
        f"head_run: {run_id}\n---\n\n# Run record\n\nStatus: {'passed' if passed else 'failed'}\n"
    )
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path
