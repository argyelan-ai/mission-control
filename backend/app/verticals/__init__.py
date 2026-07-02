"""Vertical-Module — optional, strippbare Feature-Bundles (ADR-044).

Ein Vertical ist ein Unterpaket von ``app.verticals`` mit einem
``register(app)``-Entrypoint in seinem ``__init__.py``. Die Discovery hier
lädt jedes vorhandene Unterpaket; fehlt ein Verzeichnis (z.B. weil der
Public-Release es strippt), bootet die App unverändert ohne das Feature.

Vertrag pro Vertical (siehe news_studio als Referenz):
  - ``register(app: FastAPI) -> None`` — include_router / app.mount / Hook-
    Registrierung. Wird einmalig beim App-Aufbau gerufen.
  - Kopplung IN den Core ausschliesslich über ``app.verticals.hooks`` —
    Core-Code importiert NIE direkt aus einem Vertical-Paket.
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
