"""Harness catalog — the adapter-contract generalization pattern for
"what models/levels does this CLI actually support", replacing hardcoded
alias/window maps with dynamic, observed, cached discovery.

Two independent Redis-backed layers:

1. MODEL CATALOG (``discover_model_catalog``): the ``/model`` picker's own
   rows (command token + label), discovered from a THROWAWAY tmux window —
   NEVER the agent's own live session (see ``agent_chat_input``'s and
   ``pane_state``'s module docstrings for why a working session must never
   be disturbed; the same rule applies here even though this is a read-only
   discovery, not a switch — opening `/model` in a REAL session would leave
   a picker open in front of the operator/agent). Cached in Redis keyed by
   ``(harness, cli_version)`` so a CLI upgrade invalidates the old catalog
   automatically instead of silently serving stale rows forever.
   ``app.config.settings.model_aliases`` is the FALLBACK ONLY — served when
   the catalog is empty (cold cache, discovery not finished yet, or
   discovery genuinely failed) — never the primary source once a catalog
   exists.
2. OBSERVED WINDOW MAP (``observe_model_window`` /
   ``get_observed_model_windows``): every FRESH statusline-state read
   already tells us ``(model.id, context_window_size)`` for whatever model
   actually served that turn — persisted to one shared Redis hash, newest
   write wins (plain HSET, no versioning). This becomes the MIDDLE tier of
   ``transcript_chat.resolve_context_window``'s precedence chain:
   current-session statusline (stamped separately, per-event, by
   ``transcript_chat._stamp_usage_source``) > this observed map > the
   static config seed (``settings.context_windows``) > ``None``.
   ``transcript_chat.py`` does NOT import this module for the read side —
   callers (the router, the tailer) fetch the observed map themselves and
   pass it into ``resolve_context_window``/``read_history`` as a plain
   dict, keeping the parser's pure-function chain free of a Redis
   dependency and avoiding a circular import (the tailer, which writes
   observations, lives inside ``transcript_chat.py``).

Docker/cli-bridge only (v1) — ``harness_for`` returns ``None`` for every
other runtime, the same boundary ``agent_chat_input``'s capability
functions already enforce (no pane/tmux to discover from).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from typing import Any

from app.redis_client import RedisKeys, get_redis

logger = logging.getLogger("mc.harness_catalog")

_CATALOG_TTL_SECONDS = 24 * 3600  # 24h — see module docstring
_DISCOVERY_LOCK_TTL_SECONDS = 60  # generous vs. the few seconds discovery takes
_DISCOVERY_WINDOW_NAME = "mc-catalog-discovery"
_DISCOVERY_READY_TIMEOUT_SECONDS = 8
_DISCOVERY_POLL_INTERVAL_SECONDS = 0.3
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")

# A /model picker row: an optional "❯ " pointer, a number + period, then the
# label (Claude Code marks the CURRENTLY active one with a trailing "✔"),
# then Claude Code's own column separator (2+ spaces, mirrored from
# pane_state._LABEL_SPLIT_RE) and a description we don't need here.
_MODEL_ROW_RE = re.compile(r"^\s*(?:❯\s*)?\d+\.\s+(?P<label>\S.*?)(?:\s{2,}\S.*)?$")

# Known alias labels (lowercased) -> their /model command token. A row whose
# label does NOT match one of these (a local/custom model, e.g.
# "Qwen/Qwen3.6-35B-A3B-FP8") uses its own raw label as the command token
# verbatim — that IS the valid --model argument for those, live-verified
# together with the alias tokens below (Phase-0 discovery, 2026-08-18:
# `/model opus` as a direct argument persisted `"model":"opus"` into
# settings.json — the short alias token, not a full model id).
_KNOWN_ALIAS_COMMANDS = {
    "default": "default",
    "default (recommended)": "default",
    "sonnet": "sonnet",
    "opus": "opus",
    "haiku": "haiku",
}


def harness_for(agent) -> str | None:
    """Which harness catalog applies to this agent, or ``None`` if none
    does. v1: docker/cli-bridge only — same boundary
    ``agent_chat_input.effort_capabilities``/``slash_command_capabilities``
    already enforce (no pane/tmux to discover from for any other runtime).

    Kritischer Zusatz (18.08.2026, kritischer Test-Durchgang): NUR fuer
    harness=claude. Vorher galt JEDER cli-bridge-Agent als Claude — Kimi
    (kimi-CLI) und der omp-Agent haetten damit einen /model-Picker-Probe in
    eine fremde TUI bekommen und canSwitchEffort=true gemeldet, worauf ein
    Klick /effort-Kommandos in eine CLI getippt haette, die sie nicht kennt.
    Eine fremde CLI ist kein defekter Claude, sondern ein anderes Gerät."""
    runtime = getattr(agent, "agent_runtime", None)
    slug = getattr(agent, "slug", None)
    if runtime == "cli-bridge" and slug and getattr(agent, "harness", None) == "claude":
        return "claude"
    return None


def parse_model_picker(pane_text: str) -> list[dict[str, str]]:
    """Pure parser: a captured ``/model`` picker pane -> ``[{"command":str,
    "label":str}, ...]``, one entry per numbered row, skipping the header/
    footer/effort-row lines that don't match the row shape at all. See
    ``_KNOWN_ALIAS_COMMANDS`` for the label->command derivation; the ``✔``
    "currently active" marker and any trailing ``(recommended)`` suffix are
    stripped from the label used for BOTH the command lookup and the
    display label itself."""
    options: list[dict[str, str]] = []
    for line in pane_text.splitlines():
        m = _MODEL_ROW_RE.match(line)
        if m is None:
            continue
        raw_label = m.group("label").strip()
        label = raw_label.replace("✔", "").strip()
        label = re.sub(r"\s*\(recommended\)\s*$", "", label, flags=re.IGNORECASE).strip()
        command = _KNOWN_ALIAS_COMMANDS.get(label.lower())
        if command is None:
            # Not a known alias — a local/custom model row. Its own label
            # (before the ✔/"(recommended)" strip — those never apply to a
            # raw model id) IS the command token.
            command = raw_label.replace("✔", "").strip()
        options.append({"command": command, "label": label})
    return options


async def resolve_cli_version(agent) -> str | None:
    """CLI version for cache-keying — ``docker exec -u agent
    mc-agent-{slug} claude --version``, parsed for a ``N.N.N`` pattern
    (real output: ``"2.1.234 (Claude Code)"``). Docker/cli-bridge only;
    ``None`` on any failure (container gone, unexpected output) — the
    caller treats a missing version the same as a cache miss it can't key,
    forcing fresh discovery rather than risking a wrong cache hit."""
    slug = getattr(agent, "slug", None)
    runtime = getattr(agent, "agent_runtime", None)
    if runtime != "cli-bridge" or not slug:
        return None

    argv = ["docker", "exec", "-u", "agent", f"mc-agent-{slug}", "claude", "--version"]
    try:
        result = await asyncio.to_thread(
            subprocess.run, argv, capture_output=True, text=True, timeout=5
        )
    except Exception:
        logger.warning("harness_catalog: version check failed for slug=%s", slug, exc_info=True)
        return None

    if result.returncode != 0:
        return None
    m = _VERSION_RE.search(result.stdout or "")
    return m.group(0) if m else None


async def discover_model_catalog(agent) -> list[dict[str, str]]:
    """Returns this agent's harness's ``/model`` catalog — Redis-cached by
    ``(harness, cli_version)``, discovered fresh (throwaway tmux window,
    never the agent's own session) on a cache miss. Returns ``[]`` (NOT the
    static fallback — that's ``agent_chat_input.model_options_capabilities``'s
    job) when: the runtime has no harness (Boss/host), the CLI version
    can't be determined, or discovery itself fails for any reason. Never
    raises."""
    harness = harness_for(agent)
    if harness is None:
        return []
    slug = agent.slug

    cli_version = await resolve_cli_version(agent)
    if cli_version is None:
        return []

    redis = await get_redis()
    cache_key = RedisKeys.model_catalog(harness, cli_version, slug)
    try:
        cached = await redis.get(cache_key)
    except Exception:
        cached = None
    if cached:
        try:
            return json.loads(cached)
        except (json.JSONDecodeError, ValueError):
            pass  # fall through to a fresh discovery — corrupt cache entry

    lock_key = RedisKeys.model_catalog_discovery_lock(harness, cli_version, slug)
    try:
        acquired = await redis.set(lock_key, "1", nx=True, ex=_DISCOVERY_LOCK_TTL_SECONDS)
    except Exception:
        acquired = False
    if not acquired:
        # Another request is already discovering this exact (harness,
        # version) pair — don't pile on a second throwaway window; the
        # caller falls back to the static alias list for this one request.
        return []

    try:
        options = await _discover_via_throwaway_window(agent)
    except Exception:
        logger.warning(
            "harness_catalog: discovery failed for slug=%s version=%s",
            getattr(agent, "slug", None), cli_version, exc_info=True,
        )
        return []

    if options:
        try:
            await redis.set(cache_key, json.dumps(options), ex=_CATALOG_TTL_SECONDS)
        except Exception:
            logger.warning("harness_catalog: cache write failed for %s", cache_key, exc_info=True)
    return options


async def _discover_via_throwaway_window(agent) -> list[dict[str, str]]:
    """Opens a throwaway tmux window running a fresh ``claude`` session,
    drives it through ``/model`` to capture the picker, then tears the
    window down — regardless of success or failure (``finally``). NEVER
    touches the agent's own window 0 or any other real session."""
    slug = agent.slug
    window = _DISCOVERY_WINDOW_NAME

    await _tmux(slug, ["new-window", "-t", slug, "-n", window,
                        "claude --dangerously-skip-permissions"])
    try:
        if not await _wait_for_ready(slug, window):
            return []

        await _send_literal(slug, window, "/model")
        await _send_enter(slug, window)
        # The first Enter accepts the "/model" autocomplete suggestion (a
        # no-op if it was already the sole exact match); the picker itself
        # only opens on a SECOND Enter — mirrored from the live Phase-0
        # discovery transcript for this exact sequence.
        await asyncio.sleep(_DISCOVERY_POLL_INTERVAL_SECONDS)
        await _send_enter(slug, window)

        pane_text = await _poll_for_picker(slug, window)
        await _send_key(slug, window, "Escape")  # cancel — no selection change
        if pane_text is None:
            return []
        return parse_model_picker(pane_text)
    finally:
        await _tmux(slug, ["kill-window", "-t", f"{slug}:{window}"])


async def _wait_for_ready(slug: str, window: str) -> bool:
    """Polls capture-pane for the CLI's own ready-signal glyphs (mirrored
    from ``docker_agent_sync._wait_for_window_ready`` — the same vocabulary
    ``pane_state`` already builds on) up to
    ``_DISCOVERY_READY_TIMEOUT_SECONDS``."""
    deadline = time.time() + _DISCOVERY_READY_TIMEOUT_SECONDS
    while time.time() < deadline:
        pane = await _capture(slug, window)
        if pane and any(sig in pane for sig in ("╭─", "❯", "> ", "$ ")):
            return True
        await asyncio.sleep(_DISCOVERY_POLL_INTERVAL_SECONDS)
    return False


async def _poll_for_picker(slug: str, window: str) -> str | None:
    deadline = time.time() + _DISCOVERY_READY_TIMEOUT_SECONDS
    while time.time() < deadline:
        pane = await _capture(slug, window)
        if pane and "Select model" in pane:
            return pane
        await asyncio.sleep(_DISCOVERY_POLL_INTERVAL_SECONDS)
    return None


async def _tmux(slug: str, tmux_args: list[str]) -> None:
    argv = ["docker", "exec", "-u", "agent", f"mc-agent-{slug}", "tmux", *tmux_args]
    await asyncio.to_thread(subprocess.run, argv, capture_output=True, timeout=10)


async def _send_literal(slug: str, window: str, text: str) -> None:
    await _tmux(slug, ["send-keys", "-t", f"{slug}:{window}", "-l", "--", text])


async def _send_key(slug: str, window: str, key: str) -> None:
    await _tmux(slug, ["send-keys", "-t", f"{slug}:{window}", key])


async def _send_enter(slug: str, window: str) -> None:
    await _send_key(slug, window, "Enter")


async def _capture(slug: str, window: str) -> str | None:
    argv = [
        "docker", "exec", "-e", "LANG=C.UTF-8", "-u", "agent", f"mc-agent-{slug}",
        "tmux", "capture-pane", "-p", "-t", f"{slug}:{window}",
    ]
    try:
        result = await asyncio.to_thread(
            subprocess.run, argv, capture_output=True, text=True, timeout=5
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


async def observe_model_window(model: str, window: int) -> None:
    """Persists one ``(model, context_window_size)`` observation from a
    FRESH statusline-state read to the shared Redis hash — fail-silent
    (a Redis hiccup must never break the usage-event stamping it rides
    along with). Newest write always wins (plain HSET)."""
    try:
        redis = await get_redis()
        await redis.hset(RedisKeys.model_window_observations(), model, window)
    except Exception:
        logger.warning("harness_catalog: observe_model_window failed for %s", model, exc_info=True)


async def get_observed_model_windows() -> dict[str, int]:
    """Reads the whole observed-window hash. Fail-silent -> ``{}`` (the
    caller's precedence chain falls through to the static config seed)."""
    try:
        redis = await get_redis()
        raw = await redis.hgetall(RedisKeys.model_window_observations())
    except Exception:
        logger.warning("harness_catalog: get_observed_model_windows failed", exc_info=True)
        return {}
    out: dict[str, int] = {}
    for model, value in (raw or {}).items():
        try:
            out[model] = int(value)
        except (TypeError, ValueError):
            continue
    return out
