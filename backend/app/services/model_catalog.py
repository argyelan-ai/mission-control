"""Provider model catalog — "which models does this provider OFFER?".

Mission Control probes local OpenAI-compatible runtimes for the model they are
actually serving (``probe_runtime_model`` + ``runtime_watcher``, ADR-054), but
cloud runtimes (anthropic-claude-*, grok-cloud, kimi-cloud, ollama-cloud) carry a
hand-typed ``model_identifier``. So when Anthropic starts shipping
``claude-opus-5`` while MC still points at ``claude-opus-4-8``, nobody finds out.

This module closes exactly that gap and nothing more:

    The catalog answers "these models EXIST".
    It NEVER answers "this model RUNS" — ``runtime.model_identifier`` stays the
    single source of truth for that. Nothing in here writes a runtime row; the
    only mutation path is the explicit operator action behind
    ``POST /api/v1/models/catalog/bind``, which goes through the regular runtime
    CRUD.

Shape mirrors ``services/cli_update_check.py`` (Redis cache + TTL + a "there is
something newer" badge) so the operator meets one pattern, not two.

Provider selection reuses ``harness_compat.runtime_protocol()`` — there is no
parallel "provider" concept. Protocols with exactly one endpoint (anthropic,
grok, kimi) collapse into one provider each; the ``openai`` protocol gets one
provider PER runtime row, because every openai runtime is a different endpoint
(local vLLM, LM Studio, Ollama Cloud) with its own key.


Three field-verified facts this module encodes (live-checked 2026-07-25)
-----------------------------------------------------------------------
1. **Anthropic wants ``Authorization: Bearer``, not ``x-api-key``.** The
   credential MC owns is the Claude Code OAuth token
   (vault key ``claude_code_oauth_token``). Sent as ``x-api-key`` it returns
   401 — that header is for real API keys, and MC has none (nor does it need
   one). ``anthropic-version: 2023-06-01`` is mandatory on top; without it the
   API rejects the request.
2. **Grok has two model worlds.** ``api.x.ai`` advertises ~10 models, while the
   Grok Build CLI proxy (``cli-chat-proxy.grok.com``) exposes exactly one. The
   grok harness can only ever drive what the CLI itself sees, so the proxy is
   the truth here — listing the x.ai catalog would offer the operator models
   that can never be bound.
3. **The Kimi access token lives ~900 s.** It is therefore read FRESH from disk
   on every probe and never cached. Only the resulting model list goes into
   Redis. Caching the token would guarantee 401s within the quarter hour.


⚠️ GROK IS DELIBERATELY MANIFEST-DRIVEN — do not "clean this up" (2026-07-28)
-----------------------------------------------------------------------------
``config/model-catalog.json`` looks like hand-maintained cruft for grok. It is
not, and deleting it would silently drop knowledge no API can give back:

* The Grok Build CLI ships model slugs that **no HTTP surface reports**. Checked
  on 2026-07-28: ``cli-chat-proxy.grok.com/v1/models`` (with every client-surface
  header the binary knows), ``grok models`` and ``~/.grok/models_cache.json`` ALL
  return exactly ``grok-4.5`` — while ``composer-2.5-fast`` is present inside the
  0.2.93 binary, next to the bundled "Cursor Composer toolset and prompt".
* Therefore the grok adapter **merges probe ∪ manifest** instead of using the
  manifest only as a fallback (see ``_MANIFEST_UNION_PROTOCOLS``). A working
  probe must not be able to *delete* knowledge — only add to it.
* Entries the CLI knows but the wire protocol refuses carry ``cli_only: true``
  in the manifest. They are shown (so the operator learns they exist) but are
  NOT bindable — ``POST /models/catalog/bind`` rejects them. Offering a model
  that 400s on first use is worse than not listing it.

Measured on 2026-07-28 against the live CLI proxy, with the CLI's own headers
(``x-grok-client-version`` / ``-identifier`` / ``-surface``; without them the
proxy answers 426 "CLI version (none) is outdated"):
``grok-4.5`` → HTTP 200 · ``composer-2.5-fast`` → HTTP 400
``{"code":"invalid-argument","error":"Model not found: composer-2.5-fast"}``.
All 50 recorded grok sessions on this host ran ``grok-4.5``; ``composer-2.5-fast``
appears in the binary only inside a vendored Cursor subagent prompt. Hence
``cli_only: true`` — documented, visible, unbindable.


⚠️ KIMI READS THE CLI'S OWN CONFIG — that is the primary source, not HTTP
-------------------------------------------------------------------------
The Kimi HTTP probe needs an access token that lives ~900 s, so the catalog was
only ever current while a Kimi agent had recently logged in — the rest of the
time it degraded to the manifest. But the Kimi Code CLI keeps its full model
table on disk, **token-independently**, in
``~/.mc/agents/<slug>/kimi-config/config.toml`` — the same file
``kimi provider list --json`` reads, complete with ``display_name`` and
``max_context_size``. That file is reachable through the existing ``${HOME}/.mc``
mount, so the kimi adapter reads it FIRST and only augments it over HTTP.
Consequence: a dead token no longer empties the Kimi catalog.


Credential reachability from inside the backend container (investigated 2026-07-25)
-----------------------------------------------------------------------------------
* **anthropic** — reachable. ``resolve_provider_credentials`` reads the vault
  (Postgres), which the backend owns anyway.
* **openai** — reachable. ``runtime.api_key_secret_id`` → vault.
* **kimi** — reachable. ``docker-compose.yml`` bind-mounts ``${HOME}/.mc`` into
  the backend at the identical path, and the Kimi Code CLI keeps its OAuth file
  at ``~/.mc/agents/<slug>/kimi-config/credentials/kimi-code.json``
  (KIMI_CODE_HOME of the host variant). Verified present on this host. Because
  several agents may hold their own credential file, all of them are globbed and
  the newest still-valid one wins.
* **grok** — reachable SINCE 2026-07-28. ``docker-compose.yml`` now bind-mounts
  ``~/.grok/auth.json`` read-only into the backend (next to the pre-existing
  ``~/.grok/logs`` and ``~/.grok/sessions`` harvester mounts), so
  ``read_grok_token()`` finds the file. Still no ``docker exec`` shell-out: the
  backend must never reach into an agent container for secrets. A missing mount
  degrades to the manifest with an explanatory ``credential_missing`` status,
  exactly as before.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.runtime import Runtime
from app.redis_client import RedisKeys, get_redis
from app.services.harness_compat import (
    resolve_provider_credentials,
    runtime_protocol,
)

logger = logging.getLogger(__name__)

# ── Tuning ───────────────────────────────────────────────────────────────────
# Same rhythm as the CLI update check: a warm cache for the cockpit, plus a
# SHORT negative TTL so a provider that was briefly down doesn't stay "broken"
# in the UI for a quarter of an hour.
CACHE_TTL = 15 * 60
NEGATIVE_CACHE_TTL = 60

_HTTP_TIMEOUT = 15.0
_RETRIES = 2  # total attempts on 5xx/timeout
_RETRY_BACKOFF = 0.5

ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
GROK_MODELS_URL = "https://cli-chat-proxy.grok.com/v1/models"
KIMI_MODELS_URL = "https://api.kimi.com/coding/v1/models"

_MANIFEST_PATH = Path(__file__).parent.parent.parent / "config" / "model-catalog.json"

# Status vocabulary the UI renders verbatim. "manifest_fallback" and
# "unreachable" are intentionally distinct: the first still shows models (stale
# ones), the second shows none — and an empty list must never be confused with
# "this provider has no models".
STATUS_OK = "ok"
STATUS_CREDENTIAL_MISSING = "credential_missing"
STATUS_UNREACHABLE = "unreachable"
STATUS_MANIFEST_FALLBACK = "manifest_fallback"
# A FOURTH, honest state — deliberately neither `ok` nor `manifest_fallback`.
# It means: the live provider was not reached, but the list did not come from
# our hand-typed manifest either — it was read out of the CLI's OWN config file
# on disk, the same file the CLI consults to decide which models it may drive.
# Calling that `ok` would claim a live confirmation we do not have; calling it
# `manifest_fallback` would slander a source that is more current than our
# manifest and is maintained by the vendor's own updater, not by us.
STATUS_CLI_CONFIG = "cli_config"

# Protocols whose MANIFEST outranks a successful probe and is therefore MERGED
# into it (union, deduplicated) instead of only standing in when the probe dies.
#
# grok and grok alone: its CLI ships model slugs that no HTTP surface reports
# (see the module docstring — measured, not assumed). For every other provider
# the live API is strictly better informed than a file in this repo, so letting
# a stale manifest add entries there would invent models. The rule is therefore
# opt-in per protocol and must stay that way: add a protocol here only with a
# measurement showing the manifest knows something the probe cannot.
_MANIFEST_UNION_PROTOCOLS = frozenset({"grok"})

PROTOCOL_LABELS = {
    "anthropic": "Anthropic",
    "grok": "Grok (CLI-Proxy)",
    "kimi": "Kimi Code",
    "openai": "OpenAI-kompatibel",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Data shapes ──────────────────────────────────────────────────────────────


@dataclass
class ProviderTarget:
    """One discovery target, derived from existing runtime rows."""

    key: str
    protocol: str
    label: str
    runtime_slugs: list[str] = field(default_factory=list)
    endpoint: str | None = None
    # Representative row — supplies credentials and (for openai) the endpoint.
    runtime: Runtime | None = None


@dataclass
class Discovery:
    status: str
    models: list[dict]
    error: str | None = None
    # Why the live probe failed, kept SEPARATE from `status`: once we serve
    # manifest data the status reads "manifest_fallback", which on its own would
    # hide whether the provider was down or the credential was gone — and those
    # need different operator actions.
    reason: str | None = None


class _CredentialUnavailable(Exception):
    """Raised by an adapter when the credential cannot be read at all.

    Distinct from "the provider said 401": both end up as credential_missing,
    but only this one skips the HTTP call entirely.
    """


# ── Manifest ─────────────────────────────────────────────────────────────────


def read_manifest() -> dict:
    """Fallback manifest, keyed by protocol. Never raises — a corrupt or missing
    file must degrade to "no fallback available", not blank the whole endpoint."""
    try:
        with open(_MANIFEST_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("model catalog: manifest unreadable (%s)", exc)
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _manifest_models(protocol: str) -> list[dict]:
    entry = read_manifest().get(protocol) or {}
    models = entry.get("models") or []
    return [
        {
            "id": m["id"],
            "display_name": m.get("display_name"),
            "created": None,
            "context_window": m.get("context_window"),
            "raw_provider": "manifest",
            # A model the CLI knows but the wire protocol refuses. Shown so the
            # operator learns it exists, never offered as bindable.
            "cli_only": bool(m.get("cli_only")),
            "note": m.get("note"),
        }
        for m in models
        if isinstance(m, dict) and isinstance(m.get("id"), str)
    ]


def manifest_cli_only_ids(protocol: str) -> set[str]:
    """Model ids flagged ``cli_only`` for this protocol.

    Used by the bind endpoint to refuse creating a runtime row for a model the
    provider's own API rejects — showing it is informative, binding it would
    hand the operator a runtime that 400s on first use.
    """
    return {m["id"] for m in _manifest_models(protocol) if m.get("cli_only")}


def _merge_models(primary: list[dict], extra: list[dict]) -> list[dict]:
    """Union of two model lists, deduplicated by id, ``primary`` winning.

    Order is stable: everything from ``primary`` first (live data, richer
    fields), then the ids only ``extra`` knows about. Never mutates its inputs.
    """
    merged = list(primary)
    seen = {m["id"] for m in merged}
    for model in extra:
        if model["id"] in seen:
            continue
        seen.add(model["id"])
        merged.append(model)
    return merged


def _with_manifest_union(protocol: str, discovery: Discovery) -> Discovery:
    """For opt-in protocols: fold the manifest INTO a successful probe.

    Guards the exact regression this feature exists to prevent — the grok probe
    starting to work and thereby *removing* ``composer-2.5-fast`` from the
    catalog, because the proxy has never heard of it.
    """
    if protocol not in _MANIFEST_UNION_PROTOCOLS:
        return discovery
    return Discovery(
        status=discovery.status,
        models=_merge_models(discovery.models, _manifest_models(protocol)),
        error=discovery.error,
        reason=discovery.reason,
    )


def _fallback(protocol: str, reason: str, error: str | None) -> Discovery:
    """Degrade a failed probe onto the manifest.

    Without manifest coverage the failure reason IS the status (no models to
    show). With coverage the status flips to manifest_fallback — stale data,
    honestly labelled — while ``reason``/``error`` keep the original cause.
    """
    models = _manifest_models(protocol)
    if not models:
        return Discovery(status=reason, models=[], error=error, reason=reason)
    return Discovery(
        status=STATUS_MANIFEST_FALLBACK, models=models, error=error, reason=reason
    )


# ── HTTP helper ──────────────────────────────────────────────────────────────


async def _get_json(url: str, headers: dict[str, str]) -> dict:
    """GET with retry on 5xx/timeout only.

    401/403 are NOT retried: a credential problem does not fix itself on the
    second attempt, and hammering an auth endpoint is how you get rate-limited.
    Raises httpx.HTTPStatusError / httpx.HTTPError for the caller to classify.
    """
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        for attempt in range(_RETRIES):
            try:
                resp = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt + 1 < _RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF * (attempt + 1))
                    continue
                raise
            if resp.status_code >= 500 and attempt + 1 < _RETRIES:
                await asyncio.sleep(_RETRY_BACKOFF * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
    # Unreachable in practice — the loop either returns or raises.
    raise last_exc or httpx.HTTPError("no attempt was made")


def _normalize_openai_list(data: dict, provider_tag: str) -> list[dict]:
    """OpenAI-style ``{"data": [{"id": ...}]}`` → normalized entries.

    Anthropic uses the same envelope but calls the label ``display_name`` and the
    timestamp ``created_at``; both spellings are accepted so one parser covers
    every provider we talk to.
    """
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    models = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        models.append(
            {
                "id": item["id"],
                "display_name": item.get("display_name") or item.get("name"),
                "created": item.get("created") or item.get("created_at"),
                # Grok's proxy reports this; most others don't. None means
                # "unknown", never "no context".
                "context_window": item.get("context_window"),
                "raw_provider": provider_tag,
                "cli_only": False,
                "note": None,
            }
        )
    return models


# ── Credential readers ───────────────────────────────────────────────────────


def _kimi_credentials_paths() -> list[Path]:
    """All per-agent Kimi OAuth files visible to the backend.

    ``~/.mc`` is bind-mounted into the container at the identical path, so
    ``settings.home_host`` resolves the same on host and in Docker.
    """
    root = Path(settings.home_host) / ".mc" / "agents"
    try:
        return sorted(root.glob("*/kimi-config/credentials/kimi-code.json"))
    except OSError:
        return []


def _kimi_config_paths() -> list[Path]:
    """All per-agent Kimi CLI config files visible to the backend.

    Same ``${HOME}/.mc`` bind-mount as the credentials — one directory up.
    Newest file first, so the freshest CLI version wins on conflicting entries.
    """
    root = Path(settings.home_host) / ".mc" / "agents"
    try:
        paths = list(root.glob("*/kimi-config/config.toml"))
    except OSError:
        return []

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(paths, key=lambda p: (-_mtime(p), str(p)))


def read_kimi_cli_models() -> list[dict]:
    """Kimi's model table straight out of the CLI's own config — NO token needed.

    This is the file behind ``kimi provider list --json``; the backend container
    cannot execute ``kimi``, so it reads the same source instead of shelling into
    an agent container. Layout (verified 2026-07-28, kimi-code 0.29.x)::

        [models."kimi-code/k3"]
        provider = "managed:kimi-code"
        model = "k3"
        max_context_size = 1048576
        display_name = "K3"

    The catalog id is the bare ``model`` value (``k3``), not the table key
    (``kimi-code/k3``): ``runtime.model_identifier`` carries the bare form, and
    the ``bound`` flag is a string comparison against exactly that.

    Never raises — an unparsable config yields an empty list so the HTTP path
    can still carry the provider.
    """
    models: list[dict] = []
    seen: set[str] = set()
    for path in _kimi_config_paths():
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            logger.warning("model catalog: kimi config %s unreadable (%s)", path, exc)
            continue
        table = data.get("models")
        if not isinstance(table, dict):
            continue
        for key, entry in table.items():
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("model")
            if not isinstance(model_id, str) or not model_id:
                # Fall back to the part after the provider alias in the key.
                model_id = str(key).split("/")[-1]
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            ctx = entry.get("max_context_size")
            models.append(
                {
                    "id": model_id,
                    "display_name": entry.get("display_name"),
                    "created": None,
                    "context_window": ctx if isinstance(ctx, int) else None,
                    "raw_provider": "kimi-cli-config",
                    "cli_only": False,
                    "note": None,
                }
            )
    return models


def read_kimi_token() -> str:
    """Read the Kimi access token from disk — FRESH, on every single probe.

    Never cached: the token's lifetime is ~900 s (verified), so any cached copy
    is a guaranteed 401 within minutes. Among several agent credential files the
    one with the latest ``expires_at`` wins.
    """
    best_token: str | None = None
    best_expiry: float = -1.0
    for path in _kimi_credentials_paths():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            continue
        try:
            expiry = float(data.get("expires_at") or 0)
        except (TypeError, ValueError):
            expiry = 0.0
        if expiry >= best_expiry:
            best_token, best_expiry = token, expiry
    if not best_token:
        raise _CredentialUnavailable(
            "Kimi-OAuth-Datei nicht lesbar "
            "(~/.mc/agents/<slug>/kimi-config/credentials/kimi-code.json). "
            "Ein Kimi-Agent muss mindestens einmal eingeloggt sein."
        )
    return best_token


def read_grok_token() -> str:
    """Read the Grok CLI OAuth token from ``~/.grok/auth.json``.

    Almost always raises inside Docker: compose mounts only ~/.grok/logs and
    ~/.grok/sessions, never the credential file (see module docstring). Kept as a
    real reader so that mounting the file is the only change ever needed.
    """
    path = Path(settings.home_host) / ".grok" / "auth.json"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise _CredentialUnavailable(
            f"Grok-OAuth ({path}) ist aus dem Backend-Container nicht lesbar — "
            f"docker-compose mountet nur ~/.grok/logs und ~/.grok/sessions. "
            f"Katalog kommt aus dem Manifest. ({exc.__class__.__name__})"
        ) from exc
    # Keyed by "<issuer>::<client_id>"; the bearer lives in the "key" field.
    for entry in data.values():
        if isinstance(entry, dict) and isinstance(entry.get("key"), str):
            return entry["key"]
    raise _CredentialUnavailable(f"Grok-OAuth ({path}) enthält keinen Token.")


# ── Adapters ─────────────────────────────────────────────────────────────────


async def _discover_anthropic(session: AsyncSession, target: ProviderTarget) -> Discovery:
    creds = await resolve_provider_credentials(session, None, target.runtime)
    token = creds.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        raise _CredentialUnavailable(
            "Vault-Key 'claude_code_oauth_token' fehlt oder ist nicht entschlüsselbar."
        )
    # Bearer, NOT x-api-key — the OAuth token is rejected as an api key (401).
    # anthropic-version is mandatory; the API 400s without it.
    data = await _get_json(
        ANTHROPIC_MODELS_URL,
        {
            "Authorization": f"Bearer {token}",
            "anthropic-version": ANTHROPIC_VERSION,
        },
    )
    return Discovery(STATUS_OK, _normalize_openai_list(data, "anthropic"))


async def _discover_grok(session: AsyncSession, target: ProviderTarget) -> Discovery:
    # CLI proxy, not api.x.ai: the grok harness can only drive what its own CLI
    # sees, and the two catalogs differ (10 vs 1 model).
    token = read_grok_token()
    data = await _get_json(GROK_MODELS_URL, {"Authorization": f"Bearer {token}"})
    # Union, not replacement — the proxy reports exactly one model while the CLI
    # binary ships more (see module docstring). Applied via the generic hook so
    # the opt-in list stays the single place that decides who gets this.
    return _with_manifest_union(
        "grok", Discovery(STATUS_OK, _normalize_openai_list(data, "grok"))
    )


async def _discover_kimi(session: AsyncSession, target: ProviderTarget) -> Discovery:
    """CLI config first, HTTP second.

    Inverted on purpose (2026-07-28): the config file is always readable, the
    access token dies after ~900 s. Reading HTTP first meant the Kimi catalog
    was empty-but-for-the-manifest whenever no Kimi agent had recently logged
    in. Now the token only ever ADDS models; it can no longer remove any.
    """
    config_models = read_kimi_cli_models()

    try:
        token = read_kimi_token()  # fresh read — ~900 s TTL, see read_kimi_token()
        data = await _get_json(KIMI_MODELS_URL, {"Authorization": f"Bearer {token}"})
    except (_CredentialUnavailable, httpx.HTTPError, ValueError) as exc:
        if not config_models:
            raise  # nothing on disk either → normal failure handling / manifest
        # Live list unavailable, but the CLI's own config is right here. That is
        # NOT a manifest fallback and NOT an "ok" — hence its own status.
        return Discovery(
            status=STATUS_CLI_CONFIG,
            models=config_models,
            error=f"{exc.__class__.__name__}: {exc}",
            reason=(
                STATUS_CREDENTIAL_MISSING
                if isinstance(exc, _CredentialUnavailable)
                else STATUS_UNREACHABLE
            ),
        )

    # Both sources alive: HTTP wins on shared ids (it is the live truth), the
    # config contributes anything the endpoint omits.
    live = _normalize_openai_list(data, "kimi")
    merged = _merge_models(live, config_models)
    # The endpoint returns bare ids without labels; the config has display_name
    # and max_context_size for the same models. Backfill instead of discarding.
    by_id = {m["id"]: m for m in config_models}
    enriched = []
    for model in merged:
        extra = by_id.get(model["id"])
        if extra is not None:
            model = {
                **model,
                "display_name": model.get("display_name") or extra.get("display_name"),
                "context_window": model.get("context_window") or extra.get("context_window"),
            }
        enriched.append(model)
    return Discovery(STATUS_OK, enriched)


async def _discover_openai(session: AsyncSession, target: ProviderTarget) -> Discovery:
    endpoint = (target.endpoint or "").rstrip("/")
    if not endpoint:
        raise _CredentialUnavailable("Runtime hat keinen Endpoint.")
    url = f"{endpoint}/models" if endpoint.endswith("/v1") else f"{endpoint}/v1/models"
    creds = await resolve_provider_credentials(session, None, target.runtime)
    headers = {}
    key = creds.get("OPENAI_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    # No key is NOT an error here: local vLLM/LM Studio serve /v1/models keyless.
    data = await _get_json(url, headers)
    return Discovery(STATUS_OK, _normalize_openai_list(data, "openai"))


ADAPTERS = {
    "anthropic": _discover_anthropic,
    "grok": _discover_grok,
    "kimi": _discover_kimi,
    "openai": _discover_openai,
}


# ── Provider targets ─────────────────────────────────────────────────────────


async def build_provider_targets(session: AsyncSession) -> list[ProviderTarget]:
    """Derive discovery targets from the runtime rows.

    Deliberately derived, never configured: a provider only exists in the
    catalog if MC already has a runtime pointing at it, so the catalog can never
    grow into an independent registry of its own.
    """
    runtimes = (
        await session.exec(select(Runtime).where(Runtime.enabled == True))  # noqa: E712
    ).all()

    targets: dict[str, ProviderTarget] = {}
    for rt in sorted(runtimes, key=lambda r: (r.ui_order, r.slug)):
        proto = runtime_protocol(rt)
        if proto not in ADAPTERS:
            continue
        if proto == "openai":
            # One provider per runtime — each has its own endpoint and key.
            key = f"openai:{rt.slug}"
            targets[key] = ProviderTarget(
                key=key,
                protocol=proto,
                label=rt.display_name,
                runtime_slugs=[rt.slug],
                endpoint=rt.endpoint,
                runtime=rt,
            )
            continue
        # Single-endpoint protocols collapse into one provider; several runtime
        # rows (anthropic-claude-opus / -sonnet) share one upstream catalog.
        existing = targets.get(proto)
        if existing is None:
            targets[proto] = ProviderTarget(
                key=proto,
                protocol=proto,
                label=PROTOCOL_LABELS.get(proto, proto),
                runtime_slugs=[rt.slug],
                endpoint=rt.endpoint,
                runtime=rt,
            )
        else:
            existing.runtime_slugs.append(rt.slug)
    return list(targets.values())


# ── Cache + orchestration ────────────────────────────────────────────────────


async def _cache_get(key: str) -> dict | None:
    try:
        redis = await get_redis()
        raw = await redis.get(RedisKeys.model_catalog_provider(key))
    except Exception as exc:  # noqa: BLE001 — Redis down must not break the page
        logger.warning("model catalog: cache read failed (%s)", exc)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def _cache_set(key: str, payload: dict, ttl: int) -> None:
    try:
        redis = await get_redis()
        await redis.set(RedisKeys.model_catalog_provider(key), json.dumps(payload), ex=ttl)
    except Exception as exc:  # noqa: BLE001
        logger.warning("model catalog: cache write failed (%s)", exc)


async def invalidate_cache(session: AsyncSession) -> None:
    """Drop every provider's cached entry (the "Jetzt prüfen" button)."""
    try:
        redis = await get_redis()
    except Exception as exc:  # noqa: BLE001
        logger.warning("model catalog: cache invalidation skipped (%s)", exc)
        return
    for target in await build_provider_targets(session):
        try:
            await redis.delete(RedisKeys.model_catalog_provider(target.key))
        except Exception as exc:  # noqa: BLE001
            logger.warning("model catalog: delete %s failed (%s)", target.key, exc)


async def discover_provider(session: AsyncSession, target: ProviderTarget) -> Discovery:
    """Run one adapter and translate every failure mode into a status.

    Nothing escapes: a broken provider yields a status, never a 500 on an
    endpoint whose job is to describe broken providers.
    """
    adapter = ADAPTERS.get(target.protocol)
    if adapter is None:
        return Discovery(STATUS_UNREACHABLE, [], f"Kein Adapter für '{target.protocol}'.")

    try:
        return await adapter(session, target)
    except _CredentialUnavailable as exc:
        return _fallback(target.protocol, STATUS_CREDENTIAL_MISSING, str(exc))
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            return _fallback(
                target.protocol,
                STATUS_CREDENTIAL_MISSING,
                f"HTTP {code} — Credential abgelehnt.",
            )
        return _fallback(target.protocol, STATUS_UNREACHABLE, f"HTTP {code}")
    except httpx.HTTPError as exc:
        return _fallback(target.protocol, STATUS_UNREACHABLE, f"{exc.__class__.__name__}: {exc}")
    except (ValueError, KeyError, TypeError) as exc:
        # Malformed body on a 200 (HTML captive portal, empty response, ...)
        return _fallback(target.protocol, STATUS_UNREACHABLE, f"Ungültige Antwort: {exc}")


async def get_provider_catalog(
    session: AsyncSession, target: ProviderTarget, *, force: bool = False
) -> dict:
    """Cached provider entry. Success caches for CACHE_TTL, failure only for
    NEGATIVE_CACHE_TTL so a transient outage clears itself quickly."""
    if not force:
        cached = await _cache_get(target.key)
        if cached is not None:
            return {**cached, "cached": True}

    discovery = await discover_provider(session, target)
    payload = {
        "key": target.key,
        "protocol": target.protocol,
        "label": target.label,
        "endpoint": target.endpoint,
        "runtime_slugs": target.runtime_slugs,
        "status": discovery.status,
        "reason": discovery.reason,
        "error": discovery.error,
        "models": discovery.models,
        "cached_at": _utcnow_iso(),
    }
    # cli_config caches long like a success: it is a stable answer read from a
    # local file, not a transient outage that a 60-second retry could heal. The
    # short negative TTL is reserved for states that really might fix themselves
    # (provider 5xx, runtime just booting).
    ttl = (
        CACHE_TTL
        if discovery.status in (STATUS_OK, STATUS_CLI_CONFIG)
        else NEGATIVE_CACHE_TTL
    )
    await _cache_set(target.key, payload, ttl)
    return {**payload, "cached": False}


async def build_catalog(session: AsyncSession, *, force: bool = False) -> list[dict]:
    """Full catalog with the derived ``bound`` flag per model.

    ``bound`` = some runtime row already carries this ``model_identifier``. That
    is the whole "is this model new?" mechanism — derived at read time, so no
    schema and no second model store are introduced.

    Providers are probed SEQUENTIALLY, not via gather: the adapters resolve
    credentials through the shared AsyncSession, and SQLAlchemy sessions are not
    safe for concurrent use. Worst case on a fully cold cache is
    n_providers x (_HTTP_TIMEOUT x _RETRIES) — same first-boot tradeoff the CLI
    cockpit already makes; the 15-minute cache keeps it off the hot path.
    """
    targets = await build_provider_targets(session)
    bound_ids = {
        (rt.model_identifier or "").strip()
        for rt in (await session.exec(select(Runtime))).all()
        if rt.model_identifier
    }

    providers = []
    for target in targets:
        entry = await get_provider_catalog(session, target, force=force)
        entry = {
            **entry,
            "models": [{**m, "bound": m["id"] in bound_ids} for m in entry["models"]],
        }
        # cli_only models never count as "new": they cannot be bound, so a badge
        # nagging about them would never go away no matter what the operator does.
        entry["new_count"] = sum(
            1 for m in entry["models"] if not m["bound"] and not m.get("cli_only")
        )
        providers.append(entry)
    return providers
