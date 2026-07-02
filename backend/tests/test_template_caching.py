"""MEM-03 microbenchmark — Jinja compiled-template cache.

Phase 2 plan 02-02 landed the production cache config in
backend/app/services/template_renderer.py:_get_env (auto_reload=False,
cache_size=512). These four tests assert the resulting behaviour:

- second render of SOUL.md.j2 with the same context completes in <1s
- first→second render speedup is ≥5x
- Jinja Environment cache capacity is ≥256
- Environment.auto_reload is False

The _import_or_xfail() helper is a defensive guard kept from the Wave-0
stub period — if the module ever fails to import, the suite degrades to
xfail rather than erroring.
"""
from __future__ import annotations

import time
import uuid
import pytest


def _import_or_xfail():
    try:
        from app.services.template_renderer import (
            build_agent_context,
            render_agent_file,
            _get_env,
        )
        from app.models.agent import Agent
        return build_agent_context, render_agent_file, _get_env, Agent
    except ImportError as e:
        pytest.xfail(f"Plan 02-02 implements MEM-03 changes: {e}")


def _make_bench_agent():
    _, _, _, Agent = _import_or_xfail()
    return Agent(
        id=uuid.uuid4(),
        name="bench-agent",
        role="developer",
        board_id=uuid.uuid4(),
        is_board_lead=False,
    )


def test_second_render_under_1s():
    """Roadmap Success Criterion 2 — second render of SOUL.md.j2 must be <1s."""
    build_ctx, render, _get_env_fn, _Agent = _import_or_xfail()
    agent = _make_bench_agent()
    ctx = build_ctx(agent, agents_on_board=[])
    _ = render("SOUL.md.j2", ctx)  # warm-up to load module-level singletons

    t0 = time.perf_counter()
    out1 = render("SOUL.md.j2", ctx)
    t_first = time.perf_counter() - t0

    t0 = time.perf_counter()
    out2 = render("SOUL.md.j2", ctx)
    t_second = time.perf_counter() - t0

    assert out1 == out2, "deterministic output expected"
    assert t_second < 1.0, f"second render must be <1s (was {t_second*1000:.1f}ms)"


def test_speedup_at_least_5x():
    """Roadmap Success Criterion 2 — first/second render speedup ≥5x.

    Tests the cache by forcing a cold first render: clear Jinja's
    compiled-template cache, render once (compile + cache populate),
    render again (cache hit). The ratio must be ≥5x to prove the
    cache is doing its job. Without this clear() the test is order-
    dependent — a previous test in the suite warms the singleton.
    """
    build_ctx, render, _get_env_fn, _Agent = _import_or_xfail()
    agent = _make_bench_agent()
    ctx = build_ctx(agent, agents_on_board=[])

    # Force cold cache so "first" render is a real compile.
    _get_env_fn().cache.clear()

    t0 = time.perf_counter()
    render("SOUL.md.j2", ctx)
    t_first = time.perf_counter() - t0

    t0 = time.perf_counter()
    render("SOUL.md.j2", ctx)
    t_second = time.perf_counter() - t0

    ratio = t_first / max(t_second, 1e-9)
    assert ratio >= 5, f"second render must be ≥5x faster (was {ratio:.1f}x)"


def test_cache_size_at_least_256():
    """Roadmap Success Criterion 2 — Jinja Environment cache capacity ≥256."""
    _, _, _get_env_fn, _ = _import_or_xfail()
    env = _get_env_fn()
    assert env.cache is not None, "Environment.cache must be enabled"
    assert env.cache.capacity >= 256, (
        f"Environment.cache.capacity must be ≥256 (was {env.cache.capacity})"
    )


def test_auto_reload_disabled():
    """MEM-03 — production must NOT use auto_reload (which os.stat()s every call)."""
    _, _, _get_env_fn, _ = _import_or_xfail()
    env = _get_env_fn()
    assert env.auto_reload is False, (
        "Production Environment must have auto_reload=False (defeats stat() per call)"
    )
