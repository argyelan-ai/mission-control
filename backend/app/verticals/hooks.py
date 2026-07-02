"""Hook-Registry — die EINZIGE Kopplungsrichtung Vertical → Core.

Core-Code ruft die Hook-Listen auf, ohne zu wissen, ob ein Vertical sie
befüllt hat. Verticals registrieren ihre Callables in ``register(app)``.
Leere Listen = No-op (gestrippter Public-Release).

Dieses Modul lebt bewusst im Core (wird nie gestrippt) und importiert
selbst NICHTS aus Vertical-Paketen.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

# Async-Hooks, gerufen nachdem ein Task auf status=done gewechselt ist.
# Signatur: async (session, task) -> None. Fehler werden vom Aufrufer
# geloggt und geschluckt (Vertical-Sync darf den Task-Flow nie brechen).
task_done_hooks: list[Callable[..., Awaitable[None]]] = []

# TOOLS.md-Zusatzsektionen: (scope_string, builder) — builder(ctx) -> str.
# tools_md_builder rendert die Sektion nur, wenn der Agent den Scope hat.
tools_md_sections: list[tuple[str, Callable[[dict], str]]] = []


async def run_task_done_hooks(session: Any, task: Any) -> None:
    """Alle task_done_hooks ausführen; Fehler loggen, nie propagieren."""
    import logging

    logger = logging.getLogger("mc.verticals.hooks")
    for hook in task_done_hooks:
        try:
            await hook(session, task)
        except Exception:
            logger.exception("task_done hook %s failed", getattr(hook, "__name__", hook))
