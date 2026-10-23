"""The bound runtime's context window — ONE rule for every reader.

Why this module exists (Hermes ring bug, 22.10.2026): MC told omp the served
model's window (``OMP_CONTEXT_WINDOW`` from the runtime row) but told Hermes
nothing. Hermes then guessed the window from its own model-name table — with a
stale model name in ``~/.hermes/config.yaml`` it guessed GLM's 202,752 while the
runtime served DeepSeek with 1,048,576 — and the chat ring showed 9 % instead
of 2 %. omp on the same runtime was right, because only omp got the number.

Two readers need the SAME answer, so it lives here once:

* ``build_runtime_env`` (routers/internal.py) renders it into every
  OpenAI-protocol harness's env (``OMP_CONTEXT_WINDOW`` for omp,
  ``MC_CONTEXT_WINDOW`` for everyone) — the window the harness is TOLD.
* the chat-history readers (agent_chat router, transcript tailer) hand it to
  the omp-format transcript parser, which prefers it over a harness-reported
  window that disagrees — the window the operator is SHOWN.

If the two ever used different rules, the ring would disagree with the
harness' own compaction thresholds, which is exactly the drift this fixes.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# rule: R-runtime-window-one-source - told window and shown window must never diverge
def runtime_context_window(runtime: Any | None) -> int | None:
    """The window MC tells a harness for this runtime row, or ``None``.

    ``max_context_len`` (the engine's ceiling) wins; ``preferred_context_len``
    is the fallback — the same precedence ``build_runtime_env`` has always used
    for ``OMP_CONTEXT_WINDOW``. Anything that is not a positive integer counts
    as unknown: no invented number.
    """
    if runtime is None:
        return None
    raw = getattr(runtime, "max_context_len", None) or getattr(
        runtime, "preferred_context_len", None
    )
    try:
        value = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


async def agent_runtime_window(agent: Any | None, session: Any | None = None) -> int | None:
    """The context window of the runtime ``agent`` is bound to, or ``None``.

    ``None`` for an agent without a binding (cloud/Claude Code agents usually
    have one too, but they are read by a parser that never consults this) and
    on ANY lookup failure — the chat must render with the harness' own number
    rather than not at all. ``session`` is used when the caller has one; the
    tailer has none and gets a short-lived session of its own.
    """
    runtime_id = getattr(agent, "runtime_id", None)
    if not runtime_id:
        return None
    from app.models.runtime import Runtime

    try:
        if session is not None:
            runtime = await session.get(Runtime, runtime_id)
        else:
            from app.database import async_session_maker

            async with async_session_maker() as own:
                runtime = await own.get(Runtime, runtime_id)
    except Exception:  # noqa: BLE001 — a display figure never breaks the chat
        logger.warning(
            "runtime_window: runtime %s of agent %s not readable",
            runtime_id,
            getattr(agent, "id", None),
            exc_info=True,
        )
        return None
    return runtime_context_window(runtime)


async def current_agent_runtime_window(agent: Any | None) -> int | None:
    """Like ``agent_runtime_window``, but re-reads the agent's binding first.

    For the long-lived chat tailer: its ``agent`` row is a snapshot from the
    first SSE connect, and a runtime switch changes EITHER the agent's
    ``runtime_id`` (agent_runtime_switch) OR the runtime row's
    ``max_context_len`` (recipe switch on the same slot). Hermes under ACP
    keeps its chat session across that restart, so the tailer is not
    restarted either — without this re-read the live ring kept the old
    runtime's window until the last viewer disconnected (review #939).
    ``None`` on any failure, like ``agent_runtime_window``.
    """
    agent_id = getattr(agent, "id", None)
    if agent_id is None:
        return await agent_runtime_window(agent)
    from app.database import async_session_maker
    from app.models.agent import Agent

    try:
        async with async_session_maker() as own:
            fresh = await own.get(Agent, agent_id)
            if fresh is None:
                return None
            return await agent_runtime_window(fresh, own)
    except Exception:  # noqa: BLE001 — a display figure never breaks the chat
        logger.warning(
            "runtime_window: agent %s not readable", agent_id, exc_info=True
        )
        return None


#: Protocols whose harness learns its window from its vendor, never from MC —
#: the same set ``internal._context_window_env`` skips (Claude Code, Grok,
#: Kimi, the voice sockets). For those the runtime row is not the truth.
_VENDOR_WINDOW_PROTOCOLS: frozenset[str] = frozenset({"anthropic", "grok", "kimi", "voice"})


def agent_shown_window(runtime: Any | None) -> int | None:
    """The window an agent's context figures are measured against, or ``None``.

    ``None`` means "keep the agent's own ``context_max``": no runtime, a
    runtime without a known window, or a vendor-window harness. Otherwise the
    runtime's window — the one MC TELLS the harness (``MC_CONTEXT_WINDOW`` /
    ``OMP_CONTEXT_WINDOW``) and the one the chat ring divides by. Round-2
    user test (23.10.2026): the agent row kept its creation default (200k)
    on a 1M runtime, so the agent page and the chat disagreed.
    """
    if runtime is None:
        return None
    from app.services.harness_compat import runtime_protocol

    if runtime_protocol(runtime) in _VENDOR_WINDOW_PROTOCOLS:
        return None
    return runtime_context_window(runtime)
