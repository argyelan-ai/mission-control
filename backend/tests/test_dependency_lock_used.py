"""Image and CI install the backend from requirements.lock — the same pins.

CI used to run `pip install -e ".[test]"`, which re-resolves pyproject's
open ranges on every run: CI tested FastAPI 0.142 while the image (built from
the lock) ran 0.136. A route-table guard then passed on an empty table in CI
only. These checks keep the two install paths on the lock.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _run_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if "pip install" in line]


def test_ci_installs_backend_from_the_lock():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    installs = _run_lines(ci)
    backend = [line for line in installs if "-e ." in line or "-e \".[" in line]
    assert backend, "no backend install found in ci.yml"
    for line in backend:
        assert "--no-deps" in line, f"backend install re-resolves dependencies: {line}"
    assert ci.count("pip install -r requirements.lock") >= len(backend), (
        "every backend install in ci.yml must install requirements.lock first"
    )


def test_image_installs_from_the_lock():
    dockerfile = (REPO / "backend" / "Dockerfile").read_text()
    assert re.search(r"pip install .*-r requirements\.lock", dockerfile)


def test_lock_pins_the_web_stack():
    lock = (REPO / "backend" / "requirements.lock").read_text()
    for pkg in ("fastapi", "starlette", "sqlmodel", "pydantic"):
        assert re.search(rf"^{pkg}==\S+$", lock, re.M), f"{pkg} is not pinned in the lock"
