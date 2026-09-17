"""Every ``docker compose`` argv built in backend/app must carry the project flag.

The 2026-09-17 incident had a SECOND compose call site (routers/cli_terminal.py
force-recreate) that repeated the missing ``-p`` while the constant's comment in
docker_agent_sync.py claimed to be "the only place in backend code that builds a
compose command". A comment claiming "the only place" invites exactly that — the
next reader trusts the comment instead of grepping. This test replaces the claim
with a check: it parses every module under backend/app and fails when a list
literal that starts a compose argv (``["compose", ...]`` or
``["docker", "compose", ...]``) does not pass ``-p`` / ``COMPOSE_PROJECT_NAME``.

Honest limits: the scan sees compose argv built as list literals — the only
construction style in this codebase (both call sites feed
asyncio.create_subprocess_exec / subprocess.run, never a shell string). A future
site that shells out via a formatted ``"docker compose up ..."``
string would NOT be seen by this scan; that would be a new pattern to extend the
scan for, not a silent variant of an existing one.
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND_APP = Path(__file__).resolve().parent.parent / "app"

# Known call sites — pinned so the scan cannot pass vacuously (a scan that
# finds zero sites proves nothing).
KNOWN_SITES = (
    "app/services/docker_agent_sync.py",
    "app/routers/cli_terminal.py",
)


def _is_compose_argv(node: ast.expr) -> bool:
    """True when the list literal starts a ``docker compose`` argv."""
    if not isinstance(node, ast.List) or not node.elts:
        return False
    first = node.elts[0]
    if not (isinstance(first, ast.Constant) and first.value in ("compose", "docker")):
        return False
    if first.value == "docker":
        if len(node.elts) < 2:
            return False
        second = node.elts[1]
        return isinstance(second, ast.Constant) and second.value == "compose"
    return True


def _carries_project_flag(node: ast.List) -> bool:
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and elt.value == "-p":
            return True
        if isinstance(elt, ast.Name) and elt.id == "COMPOSE_PROJECT_NAME":
            return True
    return False


def _scan_sites() -> list[tuple[str, int, bool]]:
    """(relative file, line, carries_project_flag) for every compose argv site."""
    sites: list[tuple[str, int, bool]] = []
    for path in sorted(BACKEND_APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if _is_compose_argv(node):
                rel = path.relative_to(BACKEND_APP.parent).as_posix()
                sites.append((rel, node.lineno, _carries_project_flag(node)))
    return sites


def test_every_compose_argv_carries_project_flag():
    """No compose command may be built anywhere in backend/app without -p."""
    sites = _scan_sites()
    found_files = {rel for rel, _, _ in sites}
    for known in KNOWN_SITES:
        assert known in found_files, (
            f"scan no longer sees the known compose site {known!r} — the scan "
            f"pattern or the call site changed; update the scan, do not delete it"
        )
    offenders = [(rel, line) for rel, line, ok in sites if not ok]
    assert not offenders, (
        "compose argv without the project flag (silent wrong-image incident "
        f"2026-09-17): {offenders}"
    )
