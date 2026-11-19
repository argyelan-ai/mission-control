"""Shared pytest fixtures for the omp-bridge suite.

Historical note (PR #555 follow-up B1): `tests/` only started running as a
single pytest process in CI with that PR. `bridge._ACP_CONTEXT_PCT` — then a
module global stamped from surviving acp-reader threads — was exactly the
leak the old save/restore fixture papered over: a stamp landing AFTER a
test's teardown persisted into every later test (the lane's intermittent
red). The global is gone (G5 cutover): the context% now lives in an
`ACPContextPct` holder owned by whoever owns its lifetime (serve_loop per
loop, tests per test), so nothing writes module state and no fixture is
needed.

The fs jail's container roots (test_fs_roots_hermetic.py): inside an agent
container the jail serves /deliverables and /tmp next to the session cwd.
A test that builds a real ACPClient (the replay tests) would write there —
the 18.10.1 fixture sends fs/write_text_file /tmp/ws/demo.py. Every test
starts with both switched off, as on the host; test_fs_jail.py turns them
on per test where it needs them.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _fs_jail_as_on_the_host(monkeypatch):
    """No deliverables root and no scratch root unless a test sets one."""
    monkeypatch.setenv("MC_DELIVERABLES_DIR", "")
    monkeypatch.setenv("MC_FS_SCRATCH_DIRS", "")
