"""backend and mc-worker must run under an init process (``init: true``).

Without it the entrypoint, then uvicorn / ``python -m app.worker``, is PID 1
of the container. The kernel drops every signal PID 1 has no handler for
(``man 7 pid_namespaces``), so a ``docker stop`` during migrations or the
import phase, before the Python handler exists, waited the whole
``stop_grace_period`` and ended in SIGKILL (exit 137). Measured live after
#509: stop 1 s after a restart took 30.2 s, exit 137.
"""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _services() -> dict:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    return compose["services"]


def test_backend_and_worker_run_under_init():
    services = _services()
    for name in ("backend", "mc-worker"):
        assert services[name].get("init") is True, (
            f"{name}: init: true fehlt -- ohne Init-Prozess ist die App PID 1 "
            "und ein SIGTERM vor dem Python-Handler wird verworfen (SIGKILL nach "
            "stop_grace_period)"
        )


def test_worker_keeps_entrypoint_so_migrations_still_run_first():
    """init: true wraps the entrypoint, it does not replace it."""
    worker = _services()["mc-worker"]
    assert worker["entrypoint"] == ["/docker-entrypoint.sh"]
    assert worker["command"] == ["python", "-m", "app.worker"]
