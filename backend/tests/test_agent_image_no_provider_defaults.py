"""The agent image must bake in NO provider defaults — endpoint OR model (PR9).

Sibling of tests/test_no_hardcoded_models.py, which guards the MODEL half of
the same rule. This file guards the ENDPOINT half, which the 2026-07-25 model
sanitation missed.

THE INCIDENT THIS REPLAYS (08.08.)
----------------------------------
After the Spark was switched to ``deepseek-v4-flash-0731-spark``, Shakespeare
looked misconfigured: ``docker exec mc-agent-shakespeare env`` reported
``OPENAI_BASE_URL=https://ollama.com/v1`` and no ``OPENAI_MODEL`` at all, and
a force-recreate with both env-files did not change it.

It was not misconfigured. That output was the IMAGE's own ENV, left over in
mc-agent-base/Dockerfile. The ``claude`` process actually serving the agent had
the correct values (verified live: ``OPENAI_BASE_URL=http://…:8000/v1``,
``OPENAI_MODEL=deepseek-v4-flash-0731-spark``), because the propagation path
had done its job: docker_agent_sync rendered
``~/.mc/agents/<slug>/claude-config/.env`` from the runtime row, the container
restarted, entrypoint.sh re-fetched /internal/bootstrap, and start-claude.sh
sourced the rendered .env over the image defaults.

So the baked-in ENV cost real debugging time by lying about the live state —
and worse, it was a SILENT fallback: with bootstrap down it would have pointed
an agent at a third-party provider instead of refusing to start. The model half
already had a fail-fast guard; the endpoint half never did.

The rule both halves share: model and endpoint come from the runtime row, via
bootstrap or the rendered .env, and from nowhere else. Missing means refuse to
boot, never "quietly use something else".
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_IMAGE_DIR = REPO_ROOT / "docker" / "mc-agent-base"
DOCKERFILE = AGENT_IMAGE_DIR / "Dockerfile"
ENTRYPOINT = AGENT_IMAGE_DIR / "entrypoint.sh"

# ENV lines only — a mention in a `#` comment cannot override anything at
# runtime, and the Dockerfile deliberately documents the removed default.
_ENV_LINE = re.compile(r"^\s*ENV\s+(?P<body>.+)$")

_FORBIDDEN_KEYS = ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY", "ANTHROPIC_MODEL")


def _env_assignments(dockerfile: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for raw in dockerfile.read_text(encoding="utf-8").splitlines():
        match = _ENV_LINE.match(raw)
        if not match:
            continue
        body = match.group("body").strip()
        if "=" not in body:
            continue
        key, _, value = body.partition("=")
        found[key.strip()] = value.strip()
    return found


@pytest.mark.parametrize("key", _FORBIDDEN_KEYS)
def test_agent_image_bakes_in_no_provider_default(key):
    assert DOCKERFILE.exists(), f"agent image Dockerfile missing at {DOCKERFILE}"
    baked = _env_assignments(DOCKERFILE)
    assert key not in baked, (
        f"{DOCKERFILE.relative_to(REPO_ROOT)} bakes {key}={baked.get(key)!r} into "
        f"the image. A provider default in the image drifts silently against "
        f"runtime.{'endpoint' if 'BASE_URL' in key else 'model_identifier'} and "
        f"makes `docker exec <agent> env` report something the running process "
        f"is not using (08.08. Shakespeare false alarm). It must come from "
        f"/api/v1/internal/bootstrap or the rendered claude-config/.env."
    )


@pytest.mark.parametrize("var", ["OPENAI_MODEL", "OPENAI_BASE_URL"])
def test_entrypoint_refuses_to_boot_without_model_or_endpoint(var):
    """Fail fast, symmetrically. Either variable missing means we do not know
    what we would be talking to — starting anyway is how an agent ends up on a
    provider nobody chose."""
    source = ENTRYPOINT.read_text(encoding="utf-8")
    guard = re.search(
        rf'if \[ -z "\$\{{{var}:-}}" \]; then(?P<body>.*?)\nfi',
        source,
        re.DOTALL,
    )
    assert guard, f"{ENTRYPOINT.name} has no empty-check guard for {var}"
    body = guard.group("body")
    assert "exit 1" in body, f"{var} guard in {ENTRYPOINT.name} does not exit non-zero"
    assert "FATAL" in body, f"{var} guard in {ENTRYPOINT.name} does not log loudly"
