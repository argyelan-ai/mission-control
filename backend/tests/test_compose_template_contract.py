"""Was die ausgelieferte Vorlage einer frischen Installation verspricht.

Zwei Dinge, die auf der Maschine ihres Autors nie auffallen:

1. **Die Anker trugen nackte Image-Namen.** `compose_renderer` setzt
   `MC_AGENT_IMAGE_PREFIX` per Default auf die veroeffentlichte Registry, die
   Vorlage sagte aber `image: mc-claude-agent:latest`. Ein Agent ohne
   aufgeloestes Image erbt seinen Namen ueber `<<: *anchor` — und damit einen
   Tag, den es in keiner Registry gibt: `pull access denied`.
2. **Eine Variable ohne Default und ohne Eintrag in `.env.example`** laesst
   compose bei JEDEM Aufruf warnen ("variable is not set"). Auf einer frischen
   Installation ist das das Erste, was man zu sehen bekommt.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from app.services.compose_renderer import (
    CLAUDE_IMAGE,
    KIMI_IMAGE,
    OMP_IMAGE,
    OPENCLAUDE_IMAGE,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = _REPO_ROOT / "docker" / "docker-compose.agents.example.yml"
ENV_EXAMPLE = _REPO_ROOT / ".env.example"

# Anker → das Image, das der Renderer fuer denselben Harness aufloest.
ANCHORS = {
    "x-claude-agent-base": CLAUDE_IMAGE,
    "x-openclaude-agent-base": OPENCLAUDE_IMAGE,
    "x-omp-agent-base": OMP_IMAGE,
    "x-kimi-agent-base": KIMI_IMAGE,
}

_HAS_COMPOSE = (
    shutil.which("docker") is not None
    and subprocess.run(
        ["docker", "compose", "version"], capture_output=True
    ).returncode == 0
)
_needs_compose = pytest.mark.skipif(
    not _HAS_COMPOSE, reason="docker compose not available"
)


def _resolved_images(workdir: Path, env: dict[str, str]) -> dict[str, str]:
    """Die Vorlage von `docker compose` aufloesen lassen und je Anker das
    fertige Image herausziehen — ueber einen Dienst, der den Anker erbt, denn
    genau so erbt ein Agent ohne eigenes Image seinen Namen."""
    probe = TEMPLATE.read_text(encoding="utf-8")
    services = "\n".join(
        f"  probe-{i}:\n    <<: *{anchor.removeprefix('x-')}\n"
        for i, anchor in enumerate(ANCHORS)
    )
    probe = probe.replace("services: {}", "services:\n" + services, 1)

    # Die Anker laden `docker/.env.shared` relativ zum Projektverzeichnis —
    # auf einer laufenden Installation erzeugt start-all.sh sie.
    (workdir / "docker").mkdir(parents=True, exist_ok=True)
    (workdir / "docker" / ".env.shared").write_text("", encoding="utf-8")
    out = subprocess.run(
        ["docker", "compose", "-f", "-", "config", "--format", "json"],
        input=probe, capture_output=True, text=True, timeout=120,
        env={**os.environ, **env}, cwd=str(workdir),
    )
    assert out.returncode == 0, out.stderr
    import json

    parsed = json.loads(out.stdout)["services"]
    return {
        anchor: parsed[f"probe-{i}"]["image"]
        for i, anchor in enumerate(ANCHORS)
    }


# ── 1. Image-Prefix ──────────────────────────────────────────────────────────


def test_registry_anchors_are_not_hardcoded_bare_names():
    """Statischer Teil, laeuft immer: die beiden veroeffentlichten Images
    duerfen nicht als nackter Name in der Vorlage stehen."""
    data = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    for anchor in ("x-claude-agent-base", "x-openclaude-agent-base"):
        image = data[anchor]["image"]
        assert "MC_AGENT_IMAGE_PREFIX" in image, (
            f"{anchor} traegt ein festes Image ({image!r}) — ein Agent, der es "
            "ueber den Anker erbt, sucht dann einen Tag, den es nicht gibt"
        )


def test_local_only_anchors_stay_bare():
    """omp/kimi bleiben absichtlich lokal: ihre Binaries sind arm64-gepinnt,
    die Images werden nie veroeffentlicht. Ein Prefix schickte compose auf
    einen Pull, der nur scheitern kann."""
    data = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    assert data["x-omp-agent-base"]["image"] == OMP_IMAGE
    assert data["x-kimi-agent-base"]["image"] == KIMI_IMAGE


@_needs_compose
def test_default_resolves_to_the_same_image_the_renderer_picks(tmp_path):
    """Ohne gesetzte Variablen muss compose exakt das Image bauen, das
    `compose_renderer` fuer denselben Harness schreibt — sonst zeigen Vorlage
    und Renderer auf zwei verschiedene Images."""
    env = {k: v for k, v in os.environ.items()}
    env.pop("MC_AGENT_IMAGE_PREFIX", None)
    env.pop("MC_AGENT_IMAGE_TAG", None)
    resolved = _resolved_images(tmp_path, env)
    for anchor, expected in ANCHORS.items():
        assert resolved[anchor] == expected, anchor


@_needs_compose
def test_empty_prefix_gives_bare_local_names(tmp_path):
    """Entwicklermodus: `MC_AGENT_IMAGE_PREFIX=""` in der `.env` faellt auf
    nackte lokale Namen zurueck — genau wie im Renderer."""
    resolved = _resolved_images(
        tmp_path, {"MC_AGENT_IMAGE_PREFIX": "", "MC_AGENT_IMAGE_TAG": ""}
    )
    assert resolved["x-claude-agent-base"] == "mc-claude-agent:latest"
    assert resolved["x-openclaude-agent-base"] == "mc-agent-base:latest"


# ── 2. Keine Warnung auf einer frischen Installation ─────────────────────────


def test_every_variable_has_a_default_or_lives_in_env_example():
    """Sonst warnt compose bei jedem Aufruf — und die erste Erfahrung mit
    Mission Control ist eine Wand aus 'variable is not set'."""
    text = TEMPLATE.read_text(encoding="utf-8")
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")

    # ${NAME} ohne ':-'/'-' Default. ${HOME} kommt von der Shell.
    undefaulted = {
        m.group(1)
        for m in re.finditer(r"\$\{([A-Z_][A-Z0-9_]*)\}", text)
        if m.group(1) != "HOME"
    }
    missing = [
        name for name in sorted(undefaulted)
        if not re.search(rf"^#?\s*{re.escape(name)}=", env_example, re.M)
    ]
    assert not missing, (
        "Variablen ohne Default und ohne Eintrag in .env.example — compose "
        f"warnt darauf bei jedem Aufruf: {missing}"
    )


@_needs_compose
def test_fresh_install_sees_no_compose_warning():
    """Wirk-Beweis statt Papierform: die Vorlage einmal durch `docker compose
    config` schicken, mit leerer Umgebung, und auf Warnungen schauen."""
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("MC_", "OLLAMA_", "OPENAI_", "TAVILY_"))
    }
    out = subprocess.run(
        ["docker", "compose", "-f", "-", "config", "-q"],
        input=TEMPLATE.read_text(encoding="utf-8"),
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert out.returncode == 0, out.stderr
    warnings = [l for l in out.stderr.splitlines() if "variable is not set" in l]
    assert not warnings, "\n".join(warnings)
