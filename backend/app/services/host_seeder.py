"""Host seeder — bootstraps the hosts table from existing config on startup.

Called during app startup lifespan, right after the runtime seed (ADR-028
pattern). Idempotent — the DB stays the single source of truth, edits go
through the hosts API only. Three steps, each a no-op when nothing matches:

1. `settings.dgx_ssh_host` set and no host row with that ssh_host yet
   → seed host `dgx-spark` (kind ssh) from the settings values.
2. Every ENABLED runtime of type `flask_wol` (power-managed box, ADR-042)
   that has a `control_url` but no host yet → seed one host (kind flask_wol)
   from its legacy power fields, slug `<runtime-slug>-box`. The enabled-guard
   keeps a disabled template row from materialising a phantom host (spec:
   0 hosts, 0 errors without a real GPU box). Dedupe mirrors step 1: an
   existing host with the same control_url or ssh_host (e.g. a seeded box
   after a slug rename) blocks a duplicate row.
3. Link runtimes: every runtime with `host_id IS NULL` whose endpoint IP
   matches a host's ssh_host gets the FK set.

Fresh install without a GPU box (no DGX env, no flask_wol runtime): 0 hosts,
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

    # ── 2. flask_wol boxes from their legacy runtime rows ─────────────────
    rt_result = await session.exec(
        select(Runtime)
        # A sleeping box is a runtime MC powers itself — selected by that
        # capability, not by a type name (harness-comparison ratchet, #848).
        .where(Runtime.power_managed == True)  # noqa: E712
        .where(Runtime.host_id == None)  # noqa: E711
        .order_by(Runtime.slug)
    )
    for box_rt in rt_result.scalars().all():
        box_slug = f"{box_rt.slug}-box"
        box_ip = box_rt.host or _endpoint_host(box_rt.endpoint)
        if (
            # enabled-guard: a disabled template row must not give a fresh
            # install a phantom host that the metrics bar then permanently
            # probes as offline.
            not box_rt.enabled
            or not box_rt.control_url
            or box_slug in existing_slugs
            # Dedupe analogous to step 1: the same box under a different slug
            # (e.g. after PATCH slug='workstation') must not create a duplicate
            # row — two rows with the same ssh_host would make the by_ssh_host
            # linking below nondeterministic.
            or box_rt.control_url in existing_control_urls
            or (box_ip is not None and box_ip in existing_ssh_hosts)
        ):
            continue
        box = Host(
            slug=box_slug,
            display_name=box_rt.display_name or box_slug,
            kind="flask_wol",
            # ssh_host doubles as the box IP for endpoint↔host linking below
            ssh_host=box_ip,
            control_url=box_rt.control_url,
            wol_mac_address=box_rt.wol_mac_address,
            power_managed=box_rt.power_managed,
            ui_order=1,
        )
        session.add(box)
        hosts.append(box)
        existing_slugs.add(box_slug)
        existing_control_urls.add(box_rt.control_url)
        if box_ip:
            existing_ssh_hosts.add(box_ip)
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
