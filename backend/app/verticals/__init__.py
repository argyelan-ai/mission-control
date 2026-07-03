"""Vertical modules — optional, strippable feature bundles (ADR-044).

A vertical is a subpackage of ``app.verticals`` with a
``register(app)`` entrypoint in its ``__init__.py``. The discovery here
loads every subpackage that exists; if a directory is missing (e.g. because
the public release stripped it), the app boots unchanged without that feature.

Contract per vertical (see news_studio as reference):
  - ``register(app: FastAPI) -> None`` — include_router / app.mount / hook
    registration. Called once during app bootstrap.
  - Coupling INTO the core exclusively via ``app.verticals.hooks`` —
    core code NEVER imports directly from a vertical package.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil

logger = logging.getLogger("mc.verticals")


def register_all(app) -> list[str]:
    """Discover + register every vertical subpackage. Returns loaded names."""
    loaded: list[str] = []
    for mod_info in pkgutil.iter_modules(__path__):
        if not mod_info.ispkg:
            continue
        name = mod_info.name
        try:
            module = importlib.import_module(f"{__name__}.{name}")
        except Exception:
            logger.exception("Vertical %s failed to import — skipped", name)
            continue
        register = getattr(module, "register", None)
        if register is None:
            logger.warning("Vertical %s has no register(app) — skipped", name)
            continue
        register(app)
        loaded.append(name)
        logger.info("Vertical loaded: %s", name)
    return loaded
