"""Which model an ACP session is pinned to — read at USE time, never at boot.

Incident 19.10.2026 (an omp agent, runtime switch GLM -> DeepSeek): MC re-rendered
``models.yml`` + ``omp.env`` inside the running container (ADR-078 reload,
``runtime_propagation.reload_omp_config``), but the chat daemon
(``acp_chat.py``) and the task bridge (``bridge.py``) are long-lived processes
that had read ``OMP_MODEL_SELECTOR`` from their OWN environment once, six days
earlier. Every new chat session and every task session was then pinned to
``mc-openai/GLM-5.3-Flash-EXL3`` — a model ``models.yml`` no longer listed —
and omp answered ``Unknown ACP model``. The reload's premise ("the bridge
respawns window 0 per task, launch-omp.sh re-reads omp.env") only holds for
the native TUI path; under ``OMP_DRIVER=acp`` no window is respawned.

Two layers, both generic (no model names anywhere):

1. :func:`current_selector` reads the rendered ``omp.env`` every time, with
   the same precedence as ``launch-omp.sh`` (the file wins over the process
   env, an explicit ``OMP_ACP_MODEL`` override wins over both).
2. :func:`choose_model` checks the wanted selector against the models the
   session actually OFFERS (``configOptions`` of ``session/new`` /
   ``session/load``). A selector that is not offered is never sent blindly:
   the offered sibling of the same provider is used instead, and the caller
   shows the operator what happened.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Optional

#: Notice code of the chat line that reports a fallback (frontend:
#: ``chat.notice.model_fallback``).
MODEL_FALLBACK_CODE = "model_fallback"


#: Providers omp knows by these names. An ``OPENAI_MODEL`` that already starts
#: with one of them is a full selector; anything else is a bare model id that
#: MC serves under its own ``mc-openai`` provider (entrypoint.sh renders it).
QUALIFIED_PROVIDERS = ("mc-openai", "lm-studio", "openai")

_MISSING_MODEL = "OPENAI_MODEL not set — entrypoint must render models.yml first"


def qualify(model: Optional[str]) -> Optional[str]:
    """Bare model id -> ``mc-openai/<id>``; an already provider-qualified value
    passes through unchanged (never ``mc-openai/mc-openai/X``). Never a bare
    token: that mis-resolves against omp's built-in provider catalog."""
    m = (model or "").strip()
    if not m:
        return None
    if "/" in m and m.split("/", 1)[0] in QUALIFIED_PROVIDERS:
        return m
    return f"mc-openai/{m}"


def selector_from(env: Mapping[str, str]) -> Optional[str]:
    """THE selector rule — chat daemon, task bridge and ``launch-omp.sh`` all
    follow it (``tests/test_model_selector_single_source.py`` runs one table
    through every entry point):

    ``OMP_ACP_MODEL`` (explicit override) > ``OMP_MODEL_SELECTOR`` (rendered)
    > :func:`qualify` (``OPENAI_MODEL``). None when nothing is set."""
    for key in ("OMP_ACP_MODEL", "OMP_MODEL_SELECTOR"):
        value = (env.get(key) or "").strip()
        if value:
            return value
    return qualify(env.get("OPENAI_MODEL"))


def env_file_path(env: Mapping[str, str]) -> Optional[Path]:
    """The rendered omp.env: OMP_ENV_FILE (the entrypoint exports it), else
    $OMP_HOME/omp.env. None when neither is known."""
    explicit = (env.get("OMP_ENV_FILE") or "").strip()
    if explicit:
        return Path(explicit)
    home = (env.get("OMP_HOME") or "").strip()
    return Path(home) / "omp.env" if home else None


def read_env_file(path: Optional[Path]) -> dict[str, str]:
    """``KEY=VALUE`` lines of omp.env (the format render-omp-config.sh writes).
    Missing or unreadable file = empty dict, never an exception."""
    if path is None:
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def current_env(env: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """The process env overlaid with the CURRENT rendered omp.env — exactly
    what ``launch-omp.sh`` does with ``set -a; . omp.env``. ``OMP_ACP_MODEL``
    is never written to the file, so an explicit override in the process env
    still wins."""
    base = dict(os.environ if env is None else env)
    merged = {**base, **read_env_file(env_file_path(base))}
    if (base.get("OMP_ACP_MODEL") or "").strip():
        merged["OMP_ACP_MODEL"] = base["OMP_ACP_MODEL"]
    return merged


def current_selector(
    env: Optional[Mapping[str, str]] = None, *, required: bool = False,
) -> Optional[str]:
    """The selector as of NOW (:func:`current_env` + :func:`selector_from`).

    ``required=True``: no model configured is a boot error (``RuntimeError``),
    never a silent fallback to omp's built-in default model (ADR-054)."""
    selector = selector_from(current_env(env))
    if selector is None and required:
        raise RuntimeError(_MISSING_MODEL)
    return selector


def require_selector(env: Mapping[str, str]) -> str:
    """:func:`selector_from` for an env that is already merged; raises like
    ``current_selector(required=True)`` when nothing is set."""
    selector = selector_from(env)
    if selector is None:
        raise RuntimeError(_MISSING_MODEL)
    return selector


def offered_models(config_options: Any) -> list[str]:
    """Values of the ``model`` config option. Empty = the server did not say
    (then nothing can be checked and the wanted selector goes out as is)."""
    if not isinstance(config_options, list):
        return []
    for opt in config_options:
        if not isinstance(opt, dict):
            continue
        if opt.get("id") != "model" and opt.get("category") != "model":
            continue
        values: list[str] = []
        for choice in opt.get("options") or []:
            if isinstance(choice, dict) and isinstance(choice.get("value"), str):
                values.append(choice["value"])
            elif isinstance(choice, str):
                values.append(choice)
        return values
    return []


def _provider(selector: str) -> str:
    return selector.split("/", 1)[0] if "/" in selector else ""


def choose_model(wanted: Optional[str], config_options: Any) -> tuple[Optional[str], Optional[str]]:
    """``(value_to_pin, replaced)``.

    * wanted offered (or nothing to check against) -> ``(wanted, None)``
    * wanted NOT offered, but the session offers a model of the same provider
      -> ``(that model, wanted)`` — the runtime now serves a different model
      under the same MC provider; that one is what the agent should run.
    * no such sibling -> ``(wanted, None)``: there is no better choice, so the
      selector goes out unchanged and a refusal by omp lands in the chat as
      an error card. Refusing ourselves would only trade omp's precise
      message for a vaguer one — and an incomplete option list must never
      stop a pin that omp would have accepted.

    Generic on purpose: no model names, only "is it offered" and "same
    provider". With exactly one model per MC provider (what models.yml
    renders) the sibling is unambiguous; with several, the first offered one
    wins and the notice names it.
    """
    if not wanted:
        return None, None
    offered = offered_models(config_options)
    if not offered or wanted in offered:
        return wanted, None
    provider = _provider(wanted)
    if provider:
        siblings = [v for v in offered if _provider(v) == provider]
        if siblings:
            return siblings[0], wanted
    return wanted, None


def display_name(selector: Optional[str]) -> str:
    """``mc-openai/Model-X`` -> ``Model-X`` for operator-facing text."""
    if not selector:
        return ""
    return selector.split("/", 1)[1] if "/" in selector else selector
