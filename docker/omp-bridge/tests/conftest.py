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
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
