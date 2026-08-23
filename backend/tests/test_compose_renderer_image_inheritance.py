"""Ein neuer Agent darf kein Image erben, das es nicht gibt.

`_build_new_agent_block` setzte `image` und `anchor_default_image` BEIDE aus
derselben Modulkonstante — sie waren also immer gleich, und die explizite
``image:``-Zeile wurde fuer claude/openclaude **nie** geschrieben. Was der Anker
auf der Platte tatsaechlich deklariert, hat der Code nie angeschaut.

Auf einer Installation, deren `docker-compose.agents.yml` noch aus der Zeit vor
dem Registry-Prefix stammt, steht dort der nackte Name `mc-claude-agent:latest`,
waehrend der Renderer per Default `ghcr.io/argyelan-ai/` davorsetzt. Der neue
Agent erbt dann einen Tag, den es in keiner Registry gibt: `pull access denied`
— und das Gegenteil dessen, was die README verspricht.

Der richtige Weg stand direkt daneben in `_rewrite_compose`: den Anker aus der
echten Datei lesen und damit vergleichen.
"""
from __future__ import annotations

import re
import textwrap

from app.services.compose_renderer import (
    CLAUDE_IMAGE,
    KIMI_IMAGE,
    OMP_IMAGE,
    OPENCLAUDE_IMAGE,
    _insert_new_agent_blocks,
)


def _block(content: str, slug: str) -> str:
    m = re.search(
        rf"^  mc-agent-{slug}:$.*?(?=^  mc-agent-|^[a-zA-Z]|\Z)",
        content, re.MULTILINE | re.DOTALL,
    )
    assert m, f"Block mc-agent-{slug} nicht gefunden in:\n{content}"
    return m.group(0)


def _explicit_image(block: str) -> str | None:
    m = re.search(r"^\s+image:\s*(.+?)\s*$", block, re.MULTILINE)
    return m.group(1) if m else None


# ── Alte Datei: nackter Name im Anker ───────────────────────────────────────

_LEGACY_FILE = textwrap.dedent("""\
    x-claude-agent-base: &claude-agent-base
      image: mc-claude-agent:latest
      restart: unless-stopped

    x-openclaude-agent-base: &openclaude-agent-base
      image: mc-agent-base:latest
      restart: unless-stopped

    x-omp-agent-base: &omp-agent-base
      image: mc-omp-agent:latest
      restart: unless-stopped

    services: {}

    networks:
      mission-control_default:
        external: true
    """)


def test_legacy_bare_anchor_forces_an_explicit_image():
    """Der Anker sagt `mc-claude-agent:latest`, der Renderer loest
    `ghcr.io/...` auf — dann MUSS die Zeile geschrieben werden."""
    out = _insert_new_agent_blocks(
        _LEGACY_FILE, [("alpha", CLAUDE_IMAGE)], vault_writers=set()
    )
    assert _explicit_image(_block(out, "alpha")) == CLAUDE_IMAGE, (
        "der neue Agent erbt den nackten Namen aus dem alten Anker — "
        "docker sucht dann einen Tag, den es in keiner Registry gibt"
    )


def test_legacy_bare_anchor_forces_it_for_openclaude_too():
    out = _insert_new_agent_blocks(
        _LEGACY_FILE, [("beta", OPENCLAUDE_IMAGE)], vault_writers=set()
    )
    assert _explicit_image(_block(out, "beta")) == OPENCLAUDE_IMAGE


def test_local_only_images_stay_inherited():
    """omp/kimi sind arm64-gepinnt und tragen nie ein Prefix — Anker und
    aufgeloester Wert sind gleich, die Zeile waere nur Rauschen."""
    out = _insert_new_agent_blocks(
        _LEGACY_FILE, [("gamma", OMP_IMAGE)], vault_writers=set()
    )
    block = _block(out, "gamma")
    assert "<<: *omp-agent-base" in block
    assert _explicit_image(block) is None


def test_missing_anchor_is_spelled_out_rather_than_guessed():
    """Der kimi-Anker fehlt in dieser alten Datei ganz. Dann gibt es nichts zu
    erben — das Image gehoert hingeschrieben."""
    out = _insert_new_agent_blocks(
        _LEGACY_FILE, [("delta", KIMI_IMAGE)], vault_writers=set()
    )
    assert _explicit_image(_block(out, "delta")) == KIMI_IMAGE


# ── Neue Vorlage: der Anker rechnet selbst ──────────────────────────────────

_CURRENT_FILE = _LEGACY_FILE.replace(
    "  image: mc-claude-agent:latest",
    '  image: "${MC_AGENT_IMAGE_PREFIX-ghcr.io/argyelan-ai/}mc-claude-agent:${MC_AGENT_IMAGE_TAG:-latest}"',
).replace(
    "  image: mc-agent-base:latest",
    '  image: "${MC_AGENT_IMAGE_PREFIX-ghcr.io/argyelan-ai/}mc-agent-base:${MC_AGENT_IMAGE_TAG:-latest}"',
)


def test_variable_anchor_is_left_to_do_its_job():
    """Rechnet der Anker das Image selbst aus denselben zwei Variablen aus,
    waere eine feste Zeile ein Rueckschritt: sie friert den Wert des
    Render-Zeitpunkts ein und nimmt dem Betreiber den Entwicklermodus
    (`MC_AGENT_IMAGE_PREFIX=`)."""
    out = _insert_new_agent_blocks(
        _CURRENT_FILE, [("alpha", CLAUDE_IMAGE)], vault_writers=set()
    )
    block = _block(out, "alpha")
    assert "<<: *claude-agent-base" in block
    assert _explicit_image(block) is None, (
        "die feste Zeile ueberschreibt den variablen Anker und bricht "
        "MC_AGENT_IMAGE_PREFIX="
    )


def test_a_real_override_still_wins_over_a_variable_anchor():
    """Ein Agent, dessen Runtime ein ANDERES Image verlangt, bekommt es
    natuerlich trotzdem hingeschrieben."""
    out = _insert_new_agent_blocks(
        _CURRENT_FILE, [("epsilon", OMP_IMAGE)], vault_writers=set()
    )
    assert _explicit_image(_block(out, "epsilon")) == OMP_IMAGE or (
        "<<: *omp-agent-base" in _block(out, "epsilon")
    )
