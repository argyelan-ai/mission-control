"""Shared test isolation for scripts/mc-cli/tests.

2026-09-14 incident: several tests hardcoded CTX_PATH = "/tmp/mc-context.env"
— the real, host-shared dispatch context file poll.sh writes on every
dispatch — and wrote or deleted it directly. A test run on the host stomped
every other agent's context: worst case a placeholder UUID silently survives
in the real file and `mc` sends it as a real header on the next call.

`mc_cli.config.context_env_path()` resolves the path fresh on every call via
MC_CONTEXT_ENV_PATH (default unchanged: /tmp/mc-context.env), so redirecting
that env var here reaches all production code paths (Config.from_env,
_cmd_ack, _cmd_recover) with no further wiring. Test modules that keep their
own CTX_PATH constant for read/write/cleanup helpers get it patched to the
same tmp path so both sides agree — real code and test scaffolding always
point at the same file.

(#579 originally wired this through a separate MC_CONTEXT_FILE var; #557
merged MC_CONTEXT_FILE and MC_CONTEXT_ENV_PATH into the one production
variable poll.sh and every bridge already use, so this fixture redirects
that one instead — same protection, no second variable to keep in sync.)
"""
import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_mc_context_file(tmp_path, request):
    ctx_path = str(tmp_path / "mc-context.env")
    prev_env = os.environ.get("MC_CONTEXT_ENV_PATH")
    os.environ["MC_CONTEXT_ENV_PATH"] = ctx_path

    had_attr = hasattr(request.module, "CTX_PATH")
    prev_attr = getattr(request.module, "CTX_PATH", None)
    if had_attr:
        request.module.CTX_PATH = ctx_path

    try:
        yield ctx_path
    finally:
        # Runs before this fixture's own env restore below (pytest tears
        # down in LIFO setup order, and this conftest fixture — being
        # requested by every test module — is set up ahead of any
        # module-local fixture), so module-local cleanup (e.g. a test
        # file's own `os.remove(CTX_PATH)` teardown) still sees the
        # redirected path, never the real one.
        if had_attr:
            request.module.CTX_PATH = prev_attr
        if prev_env is None:
            os.environ.pop("MC_CONTEXT_ENV_PATH", None)
        else:
            os.environ["MC_CONTEXT_ENV_PATH"] = prev_env
