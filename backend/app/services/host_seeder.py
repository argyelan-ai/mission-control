"""Host seeder — bootstraps the hosts table from existing config on startup.

Called during app startup lifespan, right after the runtime seed (ADR-028
pattern). Idempotent — the DB stays the single source of truth, edits go
through the hosts API only. Three steps, each a no-op when nothing matches:

1. `settings.dgx_ssh_host` set and no host row with that ssh_host yet
   → seed host `dgx-spark` (kind ssh) from the settings values.
2. Runtime `unsloth-porsche` exists, is ENABLED and has a `control_url`
   → seed host `porsche` (kind flask_wol) from its legacy power fields.
   The enabled-guard keeps the disabled example seed from runtimes.json
   from materialising a phantom host on fresh installs (spec: 0 hosts,
   0 errors without a real GPU box). Dedupe mirrors step 1: an existing
   host with the same control_url or ssh_host (e.g. the seeded porsche
   after a slug rename) blocks a duplicate row.
3. Link runtimes: every runtime with `host_id IS NULL` whose endpoint IP
   matches a host's ssh_host gets the FK set.

Fresh install without a GPU box (no DGX env, no porsche runtime): 0 hosts,
0 links, 0 errors — cloud runtimes never need a host (ADR-048).
"""
import logging
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.host import Host
from app.models.runtime import Runtime

logger = logging.getLogger("mc.host_seeder")


def _endpoint_host(endpoint: str | None) -> str | None:
    """Extract the IP/hostname from a runtime endpoint URL (None if unparsable)."""
    if not endpoint:
        return None
    try:
        return urlsplit(endpoint).hostname
    except ValueError:
        return None


async def seed_hosts(session: AsyncSession) -> tuple[int, int]:
    """Seed hosts from settings + legacy runtime fields, then link runtimes.

    Returns (inserted, linked) counts.
    """
    result = await session.exec(select(Host))
    hosts = list(result.scalars().all())
    existing_slugs = {h.slug for h in hosts}
    existing_ssh_hosts = {h.ssh_host for h in hosts if h.ssh_host}
    existing_control_urls = {h.control_url for h in hosts if h.control_url}

    inserted = 0

    # ── 1. dgx-spark from settings.dgx_ssh_* ──────────────────────────────
    if (
        settings.dgx_ssh_host
        and settings.dgx_ssh_host not in existing_ssh_hosts
        and "dgx-spark" not in existing_slugs
    ):
        dgx = Host(
            slug="dgx-spark",
            display_name="DGX Spark",
            kind="ssh",
            ssh_host=settings.dgx_ssh_host,
            ssh_user=settings.dgx_ssh_user or None,
            ssh_key_path=settings.dgx_ssh_key_path or None,
            ui_order=0,
        )
        session.add(dgx)
        hosts.append(dgx)
        inserted += 1

    # ── 2. porsche from the legacy unsloth-porsche runtime ────────────────
    rt_result = await session.exec(select(Runtime).where(Runtime.slug == "unsloth-porsche"))
    porsche_rt = rt_result.scalars().first()
    if (
        porsche_rt is not None
        # enabled-Guard: der Beispiel-Seed aus runtimes.json ist disabled —
        # ein OSS-Fresh-Install ohne echte PORSCHE darf keinen Phantom-Host
        # bekommen, den die Metrics-Bar dann dauerhaft als offline probt.
        and porsche_rt.enabled
        and porsche_rt.control_url
        and "porsche" not in existing_slugs
        # Dedupe analog Schritt 1: derselbe Host unter anderem Slug (z.B.
        # nach PATCH slug='workstation') darf keinen Duplikat-Row erzeugen —
        # zwei Rows mit gleicher ssh_host machen das by_ssh_host-Linking
        # unten nichtdeterministisch.
        and porsche_rt.control_url not in existing_control_urls
    ):
        porsche_ip = porsche_rt.host or _endpoint_host(porsche_rt.endpoint)
        if porsche_ip is None or porsche_ip not in existing_ssh_hosts:
            porsche = Host(
                slug="porsche",
                display_name="PORSCHE",
                kind="flask_wol",
                # ssh_host doubles as the box IP for endpoint↔host linking below
                ssh_host=porsche_ip,
                control_url=porsche_rt.control_url,
                wol_mac_address=porsche_rt.wol_mac_address,
                power_managed=porsche_rt.power_managed,
                ui_order=1,
            )
            session.add(porsche)
            hosts.append(porsche)
            inserted += 1

    # Capture plain values BEFORE commit — a session with expire_on_commit=True
    # would otherwise lazy-load on attribute access (async no-go).
    by_ssh_host = {h.ssh_host: h.id for h in hosts if h.ssh_host}

    if inserted:
        await session.commit()

    # ── 3. link unbound runtimes by endpoint IP ───────────────────────────
    linked = 0
    if by_ssh_host:
        rt_result = await session.exec(select(Runtime).where(Runtime.host_id == None))  # noqa: E711
        for runtime in rt_result.scalars().all():
            host_id = by_ssh_host.get(_endpoint_host(runtime.endpoint))
            if host_id is not None:
                runtime.host_id = host_id
                session.add(runtime)
                linked += 1
        if linked:
            await session.commit()

    if inserted or linked:
        logger.info("host seed: inserted=%d linked=%d", inserted, linked)
    else:
        logger.debug("host seed: nothing to do (0 hosts is fine — cloud-only install)")

    return (inserted, linked)
