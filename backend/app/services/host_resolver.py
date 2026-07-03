"""Host resolver — maps a runtime to the physical box its lifecycle runs on.

ADR-048 (Host Registry): the control-plane no longer assumes "exactly one GPU
box behind settings.dgx_ssh_*". Every runtime resolves its host through this
back-compat chain (first hit wins):

  1. runtime.host_id set        → Host row from the registry. A disabled host
                                  is still returned (with a loud warning) —
                                  silently falling back to another box would
                                  run docker/lms commands on the wrong machine.
  2. runtime.host legacy string → ad-hoc ResolvedHost from that IP/hostname,
                                  ssh_user/key from settings.dgx_ssh_* (the
                                  pre-registry per-runtime override).
  3. settings.dgx_ssh_host set  → ad-hoc ResolvedHost from settings (today's
                                  single-box behaviour, byte-identical).
  4. otherwise                  → None. Lifecycle ops surface a clear
                                  "Runtime hat keinen Host" error; HTTP-only
                                  probes (cloud/openai_compatible) never need
                                  a host and keep working.

runtime_manager works exclusively with ResolvedHost — never directly with
settings.dgx_ssh_* (the settings fallback is built HERE, in one place).
"""
import logging
import uuid
from dataclasses import dataclass

from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.host import Host

logger = logging.getLogger("mc.host_resolver")


@dataclass(frozen=True)
class ResolvedHost:
    """Lightweight, session-free view of the box a runtime lives on.

    Carries everything runtime_manager needs to reach the machine (SSH creds
    for kind=ssh, control plane + WoL for kind=flask_wol) plus registry
    metadata for logging/UI. `source` says which chain stage produced it.
    """

    ssh_host: str | None = None
    ssh_user: str | None = None
    ssh_key_path: str | None = None
    control_url: str | None = None
    wol_mac_address: str | None = None
    power_managed: bool = False
    kind: str = "ssh"  # ssh | flask_wol | local
    slug: str | None = None
    display_name: str | None = None
    host_id: uuid.UUID | None = None
    enabled: bool = True
    source: str = "settings"  # registry | legacy_host_field | settings


def _get(runtime, key: str):
    """Field access that works for both Runtime rows and registry dicts —
    runtime_manager still passes dicts (model_dump / to_registry_dict)."""
    if isinstance(runtime, dict):
        return runtime.get(key)
    return getattr(runtime, key, None)


def _from_host_row(host: Host) -> ResolvedHost:
    return ResolvedHost(
        ssh_host=host.ssh_host,
        ssh_user=host.ssh_user,
        ssh_key_path=host.ssh_key_path,
        control_url=host.control_url,
        wol_mac_address=host.wol_mac_address,
        power_managed=host.power_managed,
        kind=host.kind,
        slug=host.slug,
        display_name=host.display_name,
        host_id=host.id,
        enabled=host.enabled,
        source="registry",
    )


def resolved_host_from_row(host: Host) -> ResolvedHost:
    """Public Row→ResolvedHost mapper — for call sites that already loaded the
    host row and have no runtime in play (e.g. GET /hosts/{id}/metrics)."""
    return _from_host_row(host)


def settings_fallback_host() -> ResolvedHost | None:
    """Chain stage 3: the classic single-DGX setup from settings.dgx_ssh_*.

    Returns None when no DGX host is configured (cloud-only fresh install).
    """
    if not settings.dgx_ssh_host:
        return None
    return ResolvedHost(
        ssh_host=settings.dgx_ssh_host,
        ssh_user=settings.dgx_ssh_user or None,
        ssh_key_path=settings.dgx_ssh_key_path or None,
        slug=None,
        source="settings",
    )


def resolve_host_from_runtime_fields(runtime) -> ResolvedHost | None:
    """Chain stages 2-4 — no DB access needed.

    Used by runtime_manager as the in-function fallback when a caller had no
    session to run the full chain (e.g. legacy code paths, schedule service).
    Stage 1 (host_id) requires a session — see resolve_host_for_runtime().
    """
    legacy_host = _get(runtime, "host")
    if legacy_host:
        return ResolvedHost(
            ssh_host=legacy_host,
            ssh_user=settings.dgx_ssh_user or None,
            ssh_key_path=settings.dgx_ssh_key_path or None,
            source="legacy_host_field",
        )
    return settings_fallback_host()


async def resolve_host_for_runtime(
    session: AsyncSession, runtime
) -> ResolvedHost | None:
    """Full 4-stage resolution chain (see module docstring).

    `runtime` may be a Runtime row or a registry dict — dicts from
    Runtime.model_dump() carry host_id, to_registry_dict() dicts do not
    (those fall through to the legacy stages, same as before ADR-048).
    """
    host_id = _get(runtime, "host_id")
    if host_id:
        if not isinstance(host_id, uuid.UUID):
            try:
                host_id = uuid.UUID(str(host_id))
            except ValueError:
                logger.warning(
                    "Runtime '%s': host_id %r ist keine gültige UUID — Legacy-Kette.",
                    _get(runtime, "slug") or _get(runtime, "id"), host_id,
                )
                host_id = None
        if host_id is not None:
            host = await session.get(Host, host_id)
            if host is None:
                # Should not happen (FK ondelete=SET NULL) — SQLite test DBs
                # without FK enforcement can get here. Warn, then legacy chain.
                logger.warning(
                    "Runtime '%s': host_id %s zeigt auf keinen Host — Legacy-Kette.",
                    _get(runtime, "slug") or _get(runtime, "id"), host_id,
                )
            else:
                if not host.enabled:
                    # Explicitly NO silent fallback to another box: a disabled
                    # host stays the target, the operator sees why ops fail.
                    logger.warning(
                        "Runtime '%s' ist an disabled Host '%s' gebunden — "
                        "Host wird trotzdem verwendet (kein Silent-Fallback).",
                        _get(runtime, "slug") or _get(runtime, "id"), host.slug,
                    )
                return _from_host_row(host)
    return resolve_host_from_runtime_fields(runtime)


async def resolve_host_by_slug(session: AsyncSession, slug: str) -> ResolvedHost | None:
    """Registry lookup by slug (e.g. the /runtimes/spark/metrics alias →
    host 'dgx-spark'). Returns None when no such host exists."""
    from sqlalchemy import select

    result = await session.exec(select(Host).where(Host.slug == slug))
    host = result.scalars().first()
    return _from_host_row(host) if host else None
