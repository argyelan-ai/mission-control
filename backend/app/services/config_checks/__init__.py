"""Config checks — one registry for every "test this setting" (Welle 2, PR 2-1).

See ``registry.py`` for the entry shape, ``kinds.py`` for the entries and
``store.py`` for running them and keeping the latest result.
"""
from app.services.config_checks.registry import (
    COST_HINTS,
    REGISTRY,
    CheckKind,
    Outcome,
    Subject,
    register,
)
from app.services.config_checks.store import (
    CheckNotRunnable,
    CheckRun,
    UnknownCheck,
    kinds_catalog,
    run_check,
    status_item,
    status_items,
)
from app.services.config_checks import kinds  # noqa: E402,F401 — registers the entries

__all__ = [
    "COST_HINTS",
    "REGISTRY",
    "CheckKind",
    "CheckNotRunnable",
    "CheckRun",
    "Outcome",
    "Subject",
    "UnknownCheck",
    "kinds_catalog",
    "register",
    "run_check",
    "status_item",
    "status_items",
]
