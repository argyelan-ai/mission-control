"""No changing ``docker`` call may bypass the gateway (ADR-094 decision 7).

Every backend call that creates, replaces, starts, restarts, stops or removes
a container has to run through ``app/services/docker_gateway.py`` — the one
place that reads the update drain and answers "deferred" while it is set. A
new direct ``subprocess.run(["docker", "restart", ...])`` somewhere else
would quietly punch a hole into that guard, so this test parses every module
under ``backend/app`` and fails when it finds one.

What counts as a docker call: a list/tuple literal whose first element is the
string ``"docker"`` (``subprocess.run(["docker", ...])``, ``cmd = ["docker",
...]``) and a call whose first positional argument is ``"docker"``
(``asyncio.create_subprocess_exec("docker", ...)``). Such a call is allowed
outside the gateway only when its subcommand is a literal from the read-only
list below — ``docker exec`` runs a command inside an existing container and
changes no container, so it stays free as well. A subcommand that is not a
literal (``"docker", action, ...`` or ``["docker", *args]``) is treated as
changing: the scan cannot prove otherwise, and that is exactly the shape the
old ``_docker_action`` helper had.

Honest limits: a docker command handed to a shell as one formatted string
(``f"docker restart {name}"``) is not seen. In ``backend/app`` those strings
only travel over SSH to model hosts (runtime_manager, host_memory_prep,
runtime_watcher) — remote daemons, not the stack the update replaces. A local
shell-string call would be a new pattern to extend this scan for.
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND_APP = Path(__file__).resolve().parent.parent / "app"
GATEWAY = "app/services/docker_gateway.py"

#: Subcommands that read or only run a command inside an existing container.
READ_ONLY_SUBCOMMANDS = frozenset({"exec", "inspect", "ps", "logs", "version", "info"})
#: Two-word read-only forms (``docker image inspect``).
READ_ONLY_PAIRS = frozenset({("image", "inspect"), ("container", "inspect")})

# Sites that must keep being seen — pinned so the scan cannot pass vacuously
# (a scan that finds zero docker calls proves nothing).
KNOWN_READ_ONLY_SITES = (
    "app/services/docker_agent_sync.py",  # docker inspect / docker exec
    "app/routers/cli_terminal.py",  # docker ps / docker exec
    "app/services/cli_versions.py",  # docker image inspect
)


def _const(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _classify(rest: list[ast.AST]) -> bool:
    """True when the argv after ``docker`` is provably read-only."""
    if not rest:
        return False
    first = _const(rest[0])
    if first is None:
        return False
    if first in READ_ONLY_SUBCOMMANDS:
        return True
    if len(rest) >= 2:
        second = _const(rest[1])
        if second is not None and (first, second) in READ_ONLY_PAIRS:
            return True
    return False


def scan(source: str, filename: str = "<scan>") -> list[tuple[int, bool]]:
    """(line, read_only) for every docker argv in ``source``."""
    tree = ast.parse(source, filename=filename)
    # ``runtime in ("docker", "host")`` is a membership test, not an argv.
    compared = {
        id(c) for node in ast.walk(tree) if isinstance(node, ast.Compare) for c in node.comparators
    }
    sites: list[tuple[int, bool]] = []
    for node in ast.walk(tree):
        if id(node) in compared:
            continue
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            if _const(node.elts[0]) == "docker":
                sites.append((node.lineno, _classify(list(node.elts[1:]))))
        elif isinstance(node, ast.Call) and node.args:
            if _const(node.args[0]) == "docker":
                sites.append((node.lineno, _classify(list(node.args[1:]))))
    return sites


def _scan_tree() -> dict[str, list[tuple[int, bool]]]:
    found: dict[str, list[tuple[int, bool]]] = {}
    for path in sorted(BACKEND_APP.rglob("*.py")):
        rel = path.relative_to(BACKEND_APP.parent).as_posix()
        sites = scan(path.read_text(encoding="utf-8"), rel)
        if sites:
            found[rel] = sites
    return found


def test_no_changing_docker_call_outside_the_gateway():
    found = _scan_tree()
    for known in KNOWN_READ_ONLY_SITES:
        assert known in found, (
            f"scan no longer sees the known docker site {known!r} — the scan "
            f"pattern or the call site changed; update the scan, do not delete it"
        )
    offenders = [
        f"{rel}:{line}"
        for rel, sites in found.items()
        if rel != GATEWAY
        for line, read_only in sites
        if not read_only
    ]
    assert not offenders, (
        "docker calls that change containers must go through "
        "app/services/docker_gateway.py (drain guard, ADR-094 decision 7): "
        f"{offenders}"
    )


def test_gateway_is_where_docker_runs():
    """The gateway itself must be found by the scan — otherwise it was renamed."""
    found = _scan_tree()
    assert GATEWAY in found, "docker_gateway.py builds no docker argv any more"


# ── the scan itself (so a weakened scan turns red, not silent) ─────────────


def test_scan_flags_direct_changing_calls():
    src = (
        "import subprocess, asyncio\n"
        "subprocess.run(['docker', 'restart', 'x'])\n"
        "cmd = ['docker', 'compose', '-p', 'p', 'up', '-d']\n"
        "asyncio.create_subprocess_exec('docker', action, name)\n"
        "subprocess.run(['docker', *args])\n"
        "subprocess.run(('docker', 'rm', '-f', 'x'))\n"
        "subprocess.run(['docker', 'image', 'rm', 'x'])\n"
    )
    assert [ro for _, ro in scan(src)] == [False] * 6


def test_scan_lets_read_only_calls_pass():
    src = (
        "import subprocess, asyncio\n"
        "subprocess.run(['docker', 'inspect', '-f', '{{.State.Running}}', 'x'])\n"
        "subprocess.run(['docker', 'exec', 'x', 'true'])\n"
        "asyncio.create_subprocess_exec('docker', 'ps', '-a')\n"
        "subprocess.run(['docker', 'image', 'inspect', 'x'])\n"
        "subprocess.run(['docker', 'logs', 'x'])\n"
    )
    assert [ro for _, ro in scan(src)] == [True] * 5


def test_scan_ignores_membership_tests():
    assert scan("ok = runtime in ('docker', 'host')\n") == []
