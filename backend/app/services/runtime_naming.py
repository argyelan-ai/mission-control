"""One rule for runtime display names and slugs — DERIVED, never hand-typed.

Why this module exists
----------------------
The runtime registry looked like it had duplicates on 2026-07-28::

    slug                     display_name                            model_identifier
    anthropic-claude-opus    "Claude Opus 4.7 (Anthropic Pro/Max)"   claude-opus-4-8
    anthropic-claude-sonnet  "Claude Sonnet 4.6 (Anthropic Pro/Max)" claude-sonnet-5
    anthropic-claude-opus-5  "claude-opus-5"                         claude-opus-5
    ollama-cloud             "Ollama Cloud (glm-5.1)"                glm-5.1
    ollama-cloud-glm-5-2     "glm-5.2"                               glm-5.2

They are not duplicates — they are LABEL DRIFT. Three names were typed by hand
and two were written raw by the catalog bind, so the same provider ends up with
two spellings and, worse, two of the names carry a version number that
contradicts the model the row actually drives. ``claude-sonnet-4-6`` even
exists at Anthropic, so "Claude Sonnet 4.6" reads as a real, different model
while the row runs ``claude-sonnet-5``.

The cure is the same one applied to the hard-coded model names one level below:
**stop typing the value, derive it.** Seed, catalog bind and the existing rows
all go through the functions here, so there is exactly one place where a
runtime's label can be decided.

THE HARD RULE
-------------
A derived display name may only contain version numbers that come out of
``model_identifier``. ``derive_display_name`` cannot violate this by
construction (it reads nothing else), and ``display_name_drift`` is the guard
that catches a human who types one anyway — see
``tests/test_runtime_naming.py``.

Three cases, on purpose
-----------------------
1. **Provider-backed cloud rows** (endpoint host is a known provider:
   api.anthropic.com, ollama.com, cli-chat-proxy.grok.com, api.kimi.com) →
   name and slug are DERIVED. This is where drift happens, because the catalog
   creates rows next to hand-written ones.
2. **Local / infrastructure runtimes** (vllm_docker, llamacpp_docker,
   lmstudio, unsloth, unsloth_porsche, omp, hermes) → the CURATED name is kept. A generic rule
   would make them worse, not better: "Spark vLLM (Laguna/Qwen — switchable)"
   says which host it runs on and that it is recipe-switchable — neither fact
   is in ``model_identifier`` — and "Hermes (Local Ollama → Cloud DeepSeek v4
   Pro)" describes a chain, not a single model.
3. **Rows without ``model_identifier``** (nemotron-super, qwen-coder-lms,
   unsloth-studio, gemma4-nvfp4 — the probe fills these in later) → nothing can
   be derived, so nothing is touched.

Rule 3 of the hard rule applies to ALL THREE cases: whatever a name was
produced by, it must not contradict ``model_identifier``.

``runtime.model_identifier`` stays the single source of truth about which model
a row drives. Nothing here writes it, and provider + model are never collapsed
into one row: one model = one runtime row remains the schema.

.. note::
   Alembic revision ``0167_runtime_display_names`` imports
   ``derive_runtime_display_name`` (same pattern as ``0152`` importing
   ``app.services.vault_key_migration``) so the historical data fix and the
   live code can never disagree. Keep that function importable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# ── Providers ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderNaming:
    """A provider MC can name models for.

    Recognition is by ENDPOINT HOST, not by runtime_type or slug: the host is
    what actually decides which upstream catalog a row belongs to, and it is
    the same key the bind dedupe guard uses. A local OpenAI-compatible shim can
    therefore never be mislabelled as its upstream vendor.
    """

    key: str
    label: str
    slug_prefix: str
    hosts: tuple[str, ...]
    # Wire protocol(s) (harness_compat.runtime_protocol) these rows must speak.
    # Empty = no constraint (the openai protocol covers several providers).
    protocols: tuple[str, ...] = ()


PROVIDERS: tuple[ProviderNaming, ...] = (
    ProviderNaming(
        key="anthropic",
        label="Anthropic Pro/Max",
        slug_prefix="anthropic",
        hosts=("api.anthropic.com",),
        protocols=("anthropic",),
    ),
    ProviderNaming(
        key="xai",
        label="xAI Cloud",
        slug_prefix="grok",
        # The Grok Build CLI proxy is the endpoint MC actually binds; api.x.ai
        # is listed so a row pointed there is still recognised.
        hosts=("cli-chat-proxy.grok.com", "api.x.ai"),
        protocols=("grok",),
    ),
    ProviderNaming(
        key="kimi",
        label="Moonshot Cloud",
        slug_prefix="kimi",
        hosts=("api.kimi.com",),
        protocols=("kimi",),
    ),
    ProviderNaming(
        key="ollama-cloud",
        label="Ollama Cloud",
        # Historic slug of the seeded row — kept so seed and bind agree.
        slug_prefix="ollama-cloud",
        hosts=("ollama.com", "api.ollama.com"),
    ),
)

#: Runtime types whose display name is curated by a human and must not be
#: overwritten by the rule (case 2 in the module docstring).
CURATED_RUNTIME_TYPES: frozenset[str] = frozenset(
    {"vllm_docker", "llamacpp_docker", "lmstudio", "unsloth", "unsloth_porsche", "omp", "hermes"}
)


def endpoint_host(endpoint: str | None) -> str | None:
    """Host part of an endpoint URL, lowercased and port-free."""
    if not endpoint:
        return None
    raw = endpoint.strip()
    if "//" not in raw:
        raw = "//" + raw
    host = (urlsplit(raw).hostname or "").lower()
    return host or None


def resolve_provider(
    endpoint: str | None, *, protocol: str | None = None
) -> ProviderNaming | None:
    """Which known provider does this endpoint belong to? None = unknown."""
    host = endpoint_host(endpoint)
    if not host:
        return None
    for provider in PROVIDERS:
        if host not in provider.hosts:
            continue
        # A row that speaks a different wire protocol than the provider's is a
        # misconfiguration, not a naming case — leave its name alone.
        if provider.protocols and protocol is not None and protocol not in provider.protocols:
            return None
        return provider
    return None


# ── Model id → human words ───────────────────────────────────────────────────

# Uppercased verbatim. Only genuine acronyms belong here — NEVER a version
# number, and never a per-model label (that would be hand-typed naming through
# the back door).
_ACRONYMS = frozenset(
    {
        "glm", "gpt", "ai", "vl", "llm", "moe", "kv",
        "fp4", "fp8", "fp16", "bf16", "int4", "int8", "nvfp4", "awq", "gptq", "gguf",
        "xai", "dgx", "api", "ocr", "tts", "vlm",
    }
)

# Joining words stay lowercase unless they open the name — "kimi-for-coding"
# reads as "Kimi for Coding", which is also how Moonshot spells it.
_LOWERCASE_WORDS = frozenset({"for", "of", "and", "with", "on", "in", "the"})

_SEGMENT_SPLIT = re.compile(r"[/:@]+")
_TOKEN_SPLIT = re.compile(r"[-_\s.]+")
_PURE_NUMBER = re.compile(r"\d+")
_VERSIONISH = re.compile(r"\d+(?:\.\d+)*")


def _case(token: str) -> str:
    low = token.lower()
    if low in _ACRONYMS:
        return token.upper()
    if _VERSIONISH.fullmatch(token):
        return token
    if any(c.isupper() for c in token[1:]):
        # Author cased it deliberately (A3B, NVFP4, Qwen3) — keep it.
        return token[0].upper() + token[1:]
    if any(c.isdigit() for c in token):
        # Short letter+digit blobs are size/precision tags: 35b, a3b, k3, 256k.
        if sum(c.isalpha() for c in token) <= 3:
            return token.upper()
        return token[0].upper() + token[1:]
    return token[0].upper() + token[1:].lower()


def humanize_model_id(model_id: str) -> str:
    """``claude-opus-4-8`` → ``Claude Opus 4.8``; ``glm-5.2`` → ``GLM 5.2``.

    Every character of the output comes from ``model_id`` — that is the whole
    point. Separator runs (``-`` ``_`` ``.`` whitespace) become word breaks,
    consecutive all-digit tokens re-join into a dotted version (so ``4-8``
    reads as ``4.8``, the spelling the provider itself uses), and a repeated
    vendor path segment is dropped (``Qwen/Qwen3.6-35B`` → ``Qwen3.6 35B``,
    not ``Qwen Qwen3.6 35B``).
    """
    segments = [s for s in _SEGMENT_SPLIT.split((model_id or "").strip()) if s]
    kept: list[str] = []
    for i, segment in enumerate(segments):
        nxt = segments[i + 1] if i + 1 < len(segments) else None
        if nxt and nxt.lower().startswith(segment.lower()):
            continue  # "Qwen/" in front of "Qwen3.6-..." adds nothing
        kept.append(segment)

    tokens: list[str] = []
    for segment in kept:
        # A dot BETWEEN digits is a version separator and must survive; a dot
        # anywhere else is a word break.
        segment = re.sub(r"(?<=\d)\.(?=\d)", "\x00", segment)
        for token in _TOKEN_SPLIT.split(segment):
            if token:
                tokens.append(token.replace("\x00", "."))

    merged: list[str] = []
    for token in tokens:
        if (
            merged
            and _PURE_NUMBER.fullmatch(token)
            and _VERSIONISH.fullmatch(merged[-1])
        ):
            merged[-1] = f"{merged[-1]}.{token}"
        else:
            merged.append(token)

    return " ".join(
        t.lower() if (i and t.lower() in _LOWERCASE_WORDS) else _case(t)
        for i, t in enumerate(merged)
    )


# ── The two rules ────────────────────────────────────────────────────────────


def derive_display_name(model_id: str, provider: ProviderNaming | None = None) -> str:
    """``claude-opus-5`` + Anthropic → ``Claude Opus 5 (Anthropic Pro/Max)``.

    Model first, provider in parentheses: the model is what the operator picks,
    the provider is the qualifier that keeps two vendors' ``glm-5.2`` apart.
    """
    name = humanize_model_id(model_id)
    if provider is None:
        return name
    if not name:
        # Nothing nameable in the id (empty / only separators) — the provider
        # label alone is still true, and an empty "(Anthropic…)" prefix is not.
        return provider.label
    return f"{name} ({provider.label})"


_SLUG_SANITIZE = re.compile(r"[^a-z0-9]+")
SLUG_MAX_LEN = 64


def derive_slug(prefix: str | None, model_id: str) -> str:
    """``claude-opus-5`` under the anthropic provider → ``anthropic-claude-opus-5``.

    The prefix is skipped when the model id already starts with it, so
    ``grok-4.5`` stays ``grok-4-5`` instead of becoming ``grok-grok-4-5``.
    Seed and bind call this with the SAME prefix (``ProviderNaming.slug_prefix``)
    so both produce the same slug for the same model.
    """
    base = _SLUG_SANITIZE.sub("-", (model_id or "").lower()).strip("-")
    clean_prefix = _SLUG_SANITIZE.sub("-", (prefix or "").lower()).strip("-")
    slug = base if (clean_prefix and base.startswith(clean_prefix)) else f"{clean_prefix}-{base}"
    return slug.strip("-")[:SLUG_MAX_LEN]


def derive_runtime_slug(endpoint: str | None, model_id: str, *, fallback_prefix: str | None = None) -> str:
    """Slug for a model on a given endpoint — the rule seed and bind share."""
    provider = resolve_provider(endpoint)
    prefix = provider.slug_prefix if provider else fallback_prefix
    return derive_slug(prefix, model_id)


def derive_runtime_display_name(
    endpoint: str | None,
    model_identifier: str | None,
    runtime_type: str | None = None,
    *,
    protocol: str | None = None,
) -> str | None:
    """Derived display name for a runtime row, or None when it must stay curated.

    None means "the rule has nothing better to offer" — for a curated local
    runtime (case 2) or a row without a ``model_identifier`` (case 3). Callers
    keep the existing name in that case; they never fall back to a guess.
    """
    if not model_identifier or not model_identifier.strip():
        return None
    if (runtime_type or "").strip() in CURATED_RUNTIME_TYPES:
        return None
    provider = resolve_provider(endpoint, protocol=protocol)
    if provider is None:
        return None
    return derive_display_name(model_identifier.strip(), provider)


# ── The guard ────────────────────────────────────────────────────────────────

def version_tokens(text: str | None) -> set[str]:
    """Version-ish numbers in a string, normalised to dot notation.

    ``claude-opus-4-8`` and ``Claude Opus 4.8`` both yield ``{"4.8"}`` — the
    provider writes the same version two ways and the guard must not care.

    Tokenised first, never matched across word boundaries: a plain
    ``\\d+([.\\-_]\\d+)*`` regex reads ``k3-256k`` as the single version
    ``3.256`` and would then reject the perfectly correct name ``K3 256K``.
    Only STANDALONE number tokens re-join (``4`` ``8`` → ``4.8``); numbers
    glued to letters (``k3``, ``256k``, ``35b``) stay separate values.
    """
    if not text:
        return set()
    # A dot between digits is part of the version; every other dot is a break.
    normalised = re.sub(r"(?<=\d)\.(?=\d)", "\x00", str(text).lower())
    tokens = [t.replace("\x00", ".") for t in re.split(r"[^0-9a-z\x00]+", normalised) if t]

    found: set[str] = set()
    run: list[str] = []

    def flush() -> None:
        if run:
            found.add(".".join(run))
            run.clear()

    for token in tokens:
        if _VERSIONISH.fullmatch(token):
            run.append(token)
            continue
        flush()
        found.update(m.group(0) for m in _VERSIONISH.finditer(token))
    flush()
    return found


def _covered_by(token: str, id_tokens: set[str]) -> bool:
    """Is ``token`` a version the model id actually carries?

    A shorter prefix on a dot boundary counts ("Claude Opus 4" for
    ``claude-opus-4-8`` is imprecise, not a lie). ``4.7`` for ``4-8`` does not.
    """
    for candidate in id_tokens:
        if candidate == token or candidate.startswith(token + "."):
            return True
    return False


def display_name_drift(display_name: str | None, model_identifier: str | None) -> list[str]:
    """Version numbers in ``display_name`` that ``model_identifier`` does not back.

    Empty list = the name is honest (or unverifiable). This is the check that
    would have caught "Claude Opus 4.7" on a row running ``claude-opus-4-8``
    and "Claude Sonnet 4.6" on a row running ``claude-sonnet-5``.

    Rows without a ``model_identifier`` return ``[]``: there is nothing to
    check against, and flagging them would only punish rows whose model the
    probe has not filled in yet.
    """
    if not model_identifier or not model_identifier.strip():
        return []
    id_tokens = version_tokens(model_identifier)
    if not id_tokens:
        # The model id carries no version at all (e.g. "kimi-for-coding") — any
        # number in the name is then unbacked.
        return sorted(version_tokens(display_name))
    return sorted(
        token
        for token in version_tokens(display_name)
        if not _covered_by(token, id_tokens)
    )
