"""Runtime naming rule + the drift guard (services/runtime_naming.py).

The bug this suite exists for (live registry, 2026-07-28):

    anthropic-claude-opus    "Claude Opus 4.7 (Anthropic Pro/Max)"   claude-opus-4-8
    anthropic-claude-sonnet  "Claude Sonnet 4.6 (Anthropic Pro/Max)" claude-sonnet-5

Hand-typed labels drifting away from the model the row actually drives. The
second one is the nasty variant: `claude-sonnet-4-6` EXISTS at Anthropic, so
the name reads as a different real model rather than as an obvious typo.

`test_no_seeded_runtime_name_contradicts_its_model` is the regression gate: it
runs over EVERY row of config/runtimes.json, not only the derivable ones, and
fails the moment somebody writes a version number into a name by hand again.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.services import runtime_naming as rn

RUNTIMES_JSON = pathlib.Path(__file__).parents[1] / "config" / "runtimes.json"


# ── Model id → words ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model_id,expected",
    [
        ("claude-opus-5", "Claude Opus 5"),
        # 4-8 is the provider's own spelling of 4.8 — the rule must not invent
        # a different number, only re-join the one that is there.
        ("claude-opus-4-8", "Claude Opus 4.8"),
        ("claude-sonnet-4-5", "Claude Sonnet 4.5"),
        ("glm-5.2", "GLM 5.2"),
        ("grok-4.5", "Grok 4.5"),
        ("kimi-code/k3", "Kimi Code K3"),
        ("kimi-for-coding", "Kimi for Coding"),
        # Repeated vendor path segment collapses: not "Qwen Qwen3.6 ...".
        ("Qwen/Qwen3.6-35B-A3B-FP8", "Qwen3.6 35B A3B FP8"),
        ("llama-3-1-8b-instruct", "Llama 3.1 8B Instruct"),
    ],
)
def test_humanize_model_id(model_id, expected):
    assert rn.humanize_model_id(model_id) == expected


def test_derived_name_only_contains_versions_from_the_model_id():
    """The hard rule, stated as a property over the whole known catalogue.

    Whatever the rule produces, every version number in it has to be traceable
    to `model_identifier` — that is the invariant "Claude Opus 4.7" broke.
    """
    catalogue = json.loads(
        (pathlib.Path(__file__).parents[1] / "config" / "model-catalog.json").read_text()
    )
    ids = [
        m["id"]
        for key, entry in catalogue.items()
        if not key.startswith("_")
        for m in entry["models"]
    ]
    assert ids, "manifest must not be empty, otherwise this test proves nothing"
    for provider in rn.PROVIDERS:
        for model_id in ids:
            name = rn.derive_display_name(model_id, provider)
            assert rn.display_name_drift(name, model_id) == [], (name, model_id)


# ── Provider resolution ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "endpoint,expected_key",
    [
        ("https://api.anthropic.com/v1/messages", "anthropic"),
        ("https://ollama.com/v1", "ollama-cloud"),
        ("https://cli-chat-proxy.grok.com", "xai"),
        ("https://api.kimi.com/coding/v1", "kimi"),
        # Local runtimes are NOT providers — a generic derived name would strip
        # information ("Spark vLLM (Laguna/Qwen — switchable)" says which host
        # and that it is recipe-switchable; no model id carries that).
        ("http://192.0.2.10:8000/v1", None),
        ("http://127.0.0.1:11434/v1", None),
        ("", None),
        (None, None),
    ],
)
def test_resolve_provider(endpoint, expected_key):
    provider = rn.resolve_provider(endpoint)
    assert (provider.key if provider else None) == expected_key


def test_provider_is_not_matched_on_a_foreign_wire_protocol():
    """A row on api.anthropic.com that does not speak the anthropic protocol is
    a misconfiguration — naming it "Anthropic Pro/Max" would paper over it."""
    assert rn.resolve_provider("https://api.anthropic.com/v1", protocol="openai") is None
    assert rn.resolve_provider("https://api.anthropic.com/v1", protocol="anthropic")


# ── The three cases ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "endpoint,model_id,runtime_type,expected",
    [
        # 1 — provider-backed cloud rows: derived.
        (
            "https://api.anthropic.com/v1/messages",
            "claude-opus-4-8",
            "cloud",
            "Claude Opus 4.8 (Anthropic Pro/Max)",
        ),
        ("https://ollama.com/v1", "glm-5.2", "cloud", "GLM 5.2 (Ollama Cloud)"),
        ("https://cli-chat-proxy.grok.com", "grok-4.5", "grok", "Grok 4.5 (xAI Cloud)"),
        (
            "https://api.kimi.com/coding/v1",
            "kimi-code/k3",
            "kimi",
            "Kimi Code K3 (Moonshot Cloud)",
        ),
        # 2 — curated local/infrastructure runtimes: untouched.
        ("http://192.0.2.10:8000/v1", "poolside/Laguna-S-2.1-NVFP4", "vllm_docker", None),
        ("http://127.0.0.1:11434/v1", "deepseek-v4-pro:cloud", "hermes", None),
        ("http://192.0.2.20:8000/v1", "gemma-4-26B-A4B-it-qat", "unsloth_porsche", None),
        ("http://192.0.2.10:8000/v1", "poolside/Laguna-S-2.1-NVFP4", "omp", None),
        # 3 — no model_identifier: nothing to derive from.
        ("https://api.anthropic.com/v1/messages", None, "cloud", None),
        ("https://ollama.com/v1", "   ", "cloud", None),
    ],
)
def test_derive_runtime_display_name_cases(endpoint, model_id, runtime_type, expected):
    assert rn.derive_runtime_display_name(endpoint, model_id, runtime_type) == expected


def test_curated_type_wins_even_on_a_provider_endpoint():
    """Belt and braces: a lifecycle-managed local runtime type is never
    renamed, whatever endpoint someone points it at."""
    assert (
        rn.derive_runtime_display_name("https://ollama.com/v1", "glm-5.2", "vllm_docker")
        is None
    )


# ── Slug rule ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "prefix,model_id,expected",
    [
        ("anthropic", "claude-opus-5", "anthropic-claude-opus-5"),
        # Prefix is skipped when the id already carries it — not "grok-grok-4-5".
        ("grok", "grok-4.5", "grok-4-5"),
        ("kimi", "k3-256k", "kimi-k3-256k"),
        ("ollama-cloud", "glm-5.1", "ollama-cloud-glm-5-1"),
    ],
)
def test_derive_slug(prefix, model_id, expected):
    assert rn.derive_slug(prefix, model_id) == expected


def test_seed_and_bind_derive_the_same_slug():
    """The inconsistency that started this: the seed row was called
    `anthropic-claude-opus` while a bind of the same model produced
    `anthropic-claude-opus-5`. One rule, one prefix source, one slug."""
    for endpoint, model_id in [
        ("https://api.anthropic.com/v1/messages", "claude-opus-5"),
        ("https://ollama.com/v1", "glm-5.2"),
        ("https://cli-chat-proxy.grok.com", "grok-4.5"),
        ("https://api.kimi.com/coding/v1", "k3"),
    ]:
        provider = rn.resolve_provider(endpoint)
        assert provider is not None
        assert rn.derive_runtime_slug(endpoint, model_id) == rn.derive_slug(
            provider.slug_prefix, model_id
        )


def test_slug_is_length_capped_and_url_safe():
    slug = rn.derive_runtime_slug(
        "https://ollama.com/v1", "some/very-long_model.name-" + "x" * 120
    )
    assert len(slug) <= rn.SLUG_MAX_LEN
    assert slug == slug.lower()
    assert all(c.isalnum() or c == "-" for c in slug)


# ── Drift guard ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "display_name,model_identifier,expected",
    [
        # The two live offenders.
        ("Claude Opus 4.7 (Anthropic Pro/Max)", "claude-opus-4-8", ["4.7"]),
        ("Claude Sonnet 4.6 (Anthropic Pro/Max)", "claude-sonnet-5", ["4.6"]),
        # Correct labels, in either spelling of the version.
        ("Claude Opus 4.8 (Anthropic Pro/Max)", "claude-opus-4-8", []),
        ("Claude Opus 4-8", "claude-opus-4-8", []),
        ("Claude Sonnet 5 (Anthropic Pro/Max)", "claude-sonnet-5", []),
        ("GLM 5.1 (Ollama Cloud)", "glm-5.1", []),
        # A shorter version is imprecise, not a lie — allowed on a dot boundary.
        ("Claude Opus 4", "claude-opus-4-8", []),
        # Curated names that mention numbers the model id does back.
        ("Hermes (vLLM Qwen3.6-35B)", "Qwen/Qwen3.6-35B-A3B-FP8", []),
        ("Hermes (Local Ollama, DeepSeek v4 Pro)", "deepseek-v4-pro:cloud", []),
        # Curated names without any number can never contradict anything.
        ("Spark vLLM (Laguna/Qwen — switchable)", "poolside/Laguna-S-2.1-NVFP4", []),
        # No model_identifier → unverifiable, must not be flagged (the probe
        # fills these in later; failing them would punish honest rows).
        ("Nemotron 3 Super", None, []),
        ("Gemma 4 31B NVFP4", "", []),
        # Model id without any version at all → every number in the name is
        # unbacked.
        ("Kimi for Coding 2", "kimi-for-coding", ["2"]),
    ],
)
def test_display_name_drift(display_name, model_identifier, expected):
    assert rn.display_name_drift(display_name, model_identifier) == expected


# ── Seed file — the gate that fails on a hand-typed version ──────────────────


def _seed_entries() -> list[dict]:
    return json.loads(RUNTIMES_JSON.read_text())


def test_no_seeded_runtime_name_contradicts_its_model():
    """Runs over ALL seed rows, derivable or curated.

    Rows without a `model_identifier` are skipped by `display_name_drift`
    itself (nothing to compare against) — that is deliberate and covered by
    `test_display_name_drift`.
    """
    offenders = []
    for entry in _seed_entries():
        model_id = entry.get("model_identifier") or entry.get("lms_identifier")
        drift = rn.display_name_drift(entry.get("display_name"), model_id)
        if drift:
            offenders.append((entry.get("id"), entry.get("display_name"), model_id, drift))
    assert offenders == [], (
        "display_name carries version numbers that model_identifier does not back: "
        f"{offenders}"
    )


def test_the_gate_actually_catches_the_original_bug():
    """A guard nobody has seen fail is not a guard.

    This is the exact pre-fix seed content; if this ever returns [] the test
    above has stopped protecting anything.
    """
    assert rn.display_name_drift(
        "Claude Opus 4.7 (Anthropic Pro/Max)", "claude-opus-4-8"
    ) == ["4.7"]
    assert rn.display_name_drift(
        "Claude Sonnet 4.6 (Anthropic Pro/Max)", "claude-sonnet-5"
    ) == ["4.6"]


def test_seed_names_of_provider_rows_equal_the_derived_name():
    """A fresh install must produce the same labels an existing fleet has after
    migration 0167 — otherwise the drift comes back on the next deploy."""
    mismatches = []
    for entry in _seed_entries():
        derived = rn.derive_runtime_display_name(
            entry.get("endpoint", ""),
            entry.get("model_identifier") or entry.get("lms_identifier"),
            entry.get("runtime_type"),
        )
        if derived and derived != entry.get("display_name"):
            mismatches.append((entry.get("id"), entry.get("display_name"), derived))
    assert mismatches == []
