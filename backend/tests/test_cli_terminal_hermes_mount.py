"""Tests fuer Hermes-Routing in cli_terminal.py.

Verifiziert:
  - Hermes-Slug routet zu hermes-worker tmux-Session via query-params
  - Boss-Slug bleibt unveraendert (kein query-string -> bridge nutzt Default)
  - Unbekannter host-runtime Slug -> 404 (kein Mount)
"""
import os

import pytest

from app.routers import cli_terminal as cli_mod


def test_hermes_slug_routes_to_hermes_worker():
    """slug='hermes' -> upstream URL enthaelt session=hermes-worker + Socket."""
    url = cli_mod._build_host_upstream_url("hermes")
    assert url is not None
    assert "session=hermes-worker" in url
    assert "socket=" in url
    # Socket muss user-default sein, nicht Boss-Custom
    assert "boss-host" not in url


def test_boss_slug_unchanged():
    """slug='boss' -> upstream URL ohne query-params (Bridge nutzt Default)."""
    url = cli_mod._build_host_upstream_url("boss")
    assert url is not None
    # Backward-compat: kein ?session=, damit Bridge auf boss-host:0 default-attached
    assert "?" not in url or "session=" not in url


def test_unknown_host_slug_returns_none():
    """Unbekannter host-runtime Slug -> None (Caller schliesst WS mit 4004)."""
    url = cli_mod._build_host_upstream_url("phantom")
    assert url is None


def test_hermes_plist_entry_present():
    """_HOST_AGENT_PLISTS hat hermes-Eintrag fuer launchctl-Lifecycle."""
    plists = cli_mod._HOST_AGENT_PLISTS
    assert "hermes" in plists
    hermes_plists = plists["hermes"]
    # Eintrag verweist auf com.mc.hermes-bridge.plist
    joined = " ".join(hermes_plists) if isinstance(hermes_plists, list) else str(hermes_plists)
    assert "com.mc.hermes-bridge" in joined


def test_hermes_target_uses_user_default_socket():
    """Hermes-Target verweist auf user-default tmux Socket (nicht Boss Custom)."""
    targets = cli_mod._HOST_AGENT_TMUX_TARGETS
    assert "hermes" in targets
    target = targets["hermes"]
    assert target["session"] == "hermes-worker"
    # Socket muss /tmp/tmux-* oder $TMPDIR/tmux-* sein
    socket = target["socket"]
    assert socket.startswith("/tmp/tmux-") or "/tmux-" in socket


def test_boss_target_preserves_legacy_default():
    """Boss-Eintrag (falls vorhanden) signalisiert: keine query-params noetig."""
    # Boss hat KEIN _HOST_AGENT_TMUX_TARGETS Override, weil Bridge-Default
    # bereits auf Boss zeigt. Test stellt sicher dass kein Override sneakt.
    targets = cli_mod._HOST_AGENT_TMUX_TARGETS
    # Entweder fehlt 'boss' ganz, oder es zeigt ausdruecklich auf Boss-Default
    if "boss" in targets:
        assert targets["boss"].get("session") in (None, "boss-host:0")
