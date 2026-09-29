"""Guard: behaviour derives from HARNESS and runtime capability, never from
an agent's identity (ADR-084, replaces ADR-081).

Three layers:

1. **Fresh-agent proof** — a brand-new slug (never configured anywhere) with
   harness omp gets the exact ACP treatment the fleet veterans get: renderer
   injection, headless chat, tier-2 skip. This is the DoD acceptance: a
   five-minutes-old agent in a fresh install switches to omp like everyone.
2. **Name-list guard** — the tripwire that goes RED when someone reintroduces
   an agent-name list as a behaviour switch (ADR-081's
   ``OMP_ACP_AGENT_SLUGS`` pattern): neither the removed identifiers nor a
   new ``*_agent_slugs`` settings field may come back (the single whitelisted
   exception is the operator opt-out for host bridges the backend cannot
   observe).
3. **Counter-direction** — claude-harness agents keep their exact previous
   behaviour, and existing omp agents (the former list members) keep ACP.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from app.config import Settings, settings
from app.services.compose_renderer import (
    CLAUDE_IMAGE,
    OMP_IMAGE,
    _build_new_agent_block,
    _rewrite_compose,
)
from app.services.harness_compat import omp_driver_for, settings_extras_for

BACKEND_APP = Path(__file__).resolve().parent.parent / "app"
REPO_ROOT = BACKEND_APP.parent.parent

# The ONLY permitted slug-list field: an explicit operator opt-out for host
# agents whose bridge driver the backend cannot observe. Everything else —
# driver decisions, chat transport, image injection — must be harness-derived.
_ALLOWED_AGENT_SLUG_FIELDS = {"recovery_tier2_skip_agent_slugs"}

# Identifiers of the abolished ADR-081 mechanism. Their return = red test.
_FORBIDDEN_IDENTIFIERS = (
    "OMP_ACP_AGENT_SLUGS",
    "omp_acp_agent_slugs",
    "omp_acp_agents",
)

_FRESH_SLUG = f"fresh-{uuid.uuid4().hex[:8]}"


# ── 1. fresh-agent proof ─────────────────────────────────────────────────────


def test_fresh_agent_slug_gets_acp_from_harness_alone(monkeypatch):
    """A slug that exists in NO config gets OMP_DRIVER=acp — harness property."""
    monkeypatch.setattr(settings, "omp_driver_default", "acp", raising=False)
    assert omp_driver_for("omp") == "acp"
    block = _build_new_agent_block(_FRESH_SLUG, OMP_IMAGE, is_vault_writer=False)
    assert "- OMP_DRIVER=acp" in block
    assert "- OMP_ACP_PERMISSIONS=yolo" in block


def test_fresh_agent_chat_is_headless(monkeypatch):
    """The fresh agent's Sessions chat runs over ACP — like the veterans."""
    monkeypatch.setattr(settings, "omp_driver_default", "acp", raising=False)
    from app.services.acp_chat_transport import headless_chat_kind

    class _Agent:
        slug = _FRESH_SLUG
        agent_runtime = "cli-bridge"
        harness = "omp"

    assert headless_chat_kind(_Agent()) == "acp-docker"


def test_fresh_agent_renders_acp_in_full_compose_rewrite(monkeypatch):
    """End-to-end through _rewrite_compose: an omp service block for the fresh
    slug picks up the driver override from its image alone."""
    monkeypatch.setattr(settings, "omp_driver_default", "acp", raising=False)
    compose = (
        "x-omp-agent-base: &omp-agent-base\n"
        "  image: mc-omp-agent:latest\n"
        "services:\n"
        f"  mc-agent-{_FRESH_SLUG}:\n"
        "    <<: *omp-agent-base\n"
        f"    container_name: mc-agent-{_FRESH_SLUG}\n"
    )
    result = _rewrite_compose(compose, {})
    assert f"mc-agent-{_FRESH_SLUG}:" in result
    assert "OMP_DRIVER=acp" in result


# ── 2. name-list guard ───────────────────────────────────────────────────────


def _code_lines(text: str) -> list[str]:
    """Source lines only — prose in comments may mention history freely."""
    return [
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_no_slug_list_identifiers_in_backend_code():
    """The ADR-081 mechanism must stay dead: no source file under backend/app
    may reference the slug-list identifiers again."""
    offenders = []
    for path in BACKEND_APP.rglob("*.py"):
        text = "\n".join(_code_lines(path.read_text(encoding="utf-8")))
        for ident in _FORBIDDEN_IDENTIFIERS:
            if ident in text:
                offenders.append(f"{path}: {ident}")
    assert not offenders, (
        "agent-name list as behaviour switch reintroduced — ADR-084 forbids "
        "this; use the harness decision in harness_compat.omp_driver_for: "
        + ", ".join(offenders)
    )


def test_settings_has_no_new_agent_slug_fields():
    """Every ``*_agent_slugs`` settings field is a name list waiting to become
    a behaviour switch. New fields need an explicit whitelist entry here AND
    an ADR justifying why the harness cannot carry the decision."""
    field_names = set(Settings.model_fields.keys())
    slug_fields = {n for n in field_names if n.endswith("_agent_slugs")}
    unexpected = slug_fields - _ALLOWED_AGENT_SLUG_FIELDS
    assert not unexpected, (
        f"name-list config fields reintroduced: {sorted(unexpected)} — "
        "derive behaviour from the harness (ADR-084) instead"
    )


def test_deployment_config_carries_only_the_global_knob():
    """.env.example and docker-compose.yml expose OMP_DRIVER_DEFAULT (one knob,
    whole fleet) and no agent-name list."""
    for rel in (".env.example", "docker-compose.yml"):
        text = "\n".join(_code_lines((REPO_ROOT / rel).read_text(encoding="utf-8")))
        for ident in _FORBIDDEN_IDENTIFIERS:
            assert ident not in text, f"{rel} reintroduces {ident}"
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^OMP_DRIVER_DEFAULT=", env_example, re.MULTILINE)


def test_driver_decision_takes_a_harness_not_a_slug():
    """Structural guard on the decision point itself: ``omp_driver_for`` has
    exactly one parameter and it is the harness — a slug cannot even be
    passed."""
    import inspect

    params = list(inspect.signature(omp_driver_for).parameters)
    assert params == ["harness"]


# ── 3. counter-direction: veterans keep their behaviour ─────────────────────


def test_claude_harness_unchanged(monkeypatch):
    """A claude agent gets no OMP_DRIVER injection, no headless chat, keeps
    hooks/statusLine and the tier-2 restart — byte-identical to before."""
    monkeypatch.setattr(settings, "omp_driver_default", "acp", raising=False)
    block = _build_new_agent_block("some-claude-agent", CLAUDE_IMAGE, is_vault_writer=False)
    assert "OMP_DRIVER" not in block

    from app.services.acp_chat_transport import headless_chat_kind

    class _Agent:
        slug = "some-claude-agent"
        agent_runtime = "cli-bridge"
        harness = "claude"

    assert headless_chat_kind(_Agent()) is None
    assert settings_extras_for("claude", None) is True
    assert omp_driver_for("claude") == "native"


def test_former_list_members_keep_acp(monkeypatch):
    """The omp veterans (previously OMP_ACP_AGENT_SLUGS members) keep the ACP
    treatment — same driver, same permissions policy, now via the harness."""
    monkeypatch.setattr(settings, "omp_driver_default", "acp", raising=False)
    assert omp_driver_for("omp") == "acp"
    block = _build_new_agent_block("veteran-omp", OMP_IMAGE, is_vault_writer=False)
    assert "- OMP_DRIVER=acp" in block
    assert "- OMP_ACP_PERMISSIONS=yolo" in block


def test_openclaude_keeps_protocol_dependent_extras():
    """Matrix pin: openclaude's hooks/statusLine stay protocol-dependent
    (None in the matrix) — anthropic runtime yes, openai runtime no."""
    assert settings_extras_for("openclaude", None) is False  # no runtime → no protocol
    assert settings_extras_for("omp", None) is False
