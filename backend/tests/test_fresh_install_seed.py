"""A fresh install starts without example hardware; an existing one keeps every row.

What a stranger sees in the first five minutes must be real. Until 2026-10 a
fresh database came up with 14 runtimes — seven of them pointed at placeholder
addresses (vLLM/LM Studio/Unsloth/llama.cpp boxes, a host-side Hermes worker,
a power-managed Windows box) — plus a seeded ``Hermes`` agent. None of that
hardware exists on a new machine, so ``/runtimes`` showed boxes that were
permanently offline.

What stays in the seed are the cloud provider anchors only (Anthropic, Ollama
Cloud, Grok, the Jarvis voice rows). They are not examples: the model catalog
binds new cloud runtimes from them (routers/models.py "Vorlage-Runtime"), the
grok harness binds to the ``grok`` row and Jarvis to the ``voice_*`` rows.

Three guarantees, three tests:

1. The seed file ships no runtime that points at a host address.
2. Seeding an empty database creates no local runtime and no host.
3. Seeding an EXISTING database changes nothing — the seeder never deletes,
   so an install that still has the old example rows (edited or not) keeps
   them byte for byte.

The Postgres test at the bottom runs the real chain (``alembic upgrade head``
on an empty database, then the boot seeders) — migrations 0078 and 0095 used
to insert example rows and the Hermes agent on their own.
"""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.host import Host
from app.models.runtime import Runtime
from app.services.host_seeder import seed_hosts
from app.services.runtime_seeder import seed_runtimes

BACKEND = Path(__file__).resolve().parents[1]
SEED_FILE = BACKEND / "config" / "runtimes.json"

#: The runtime types that are cloud provider anchors. Everything else in a
#: seed would be somebody's hardware.
CLOUD_ANCHOR_TYPES = {"cloud", "grok", "kimi", "voice_openai", "voice_xai"}


def _seed_entries() -> list[dict]:
    return json.loads(SEED_FILE.read_text())


def _is_address(hostname: str | None) -> bool:
    if not hostname:
        return False
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return hostname in {"localhost"} or hostname.endswith(".local")
    return True


# ── 1. the seed file ──────────────────────────────────────────────────────


def test_seed_file_ships_no_runtime_on_a_host_address():
    offenders = [
        e["id"] for e in _seed_entries()
        if _is_address(urlsplit(e.get("endpoint") or "").hostname)
        or e.get("host") or e.get("control_url") or e.get("wol_mac_address")
    ]
    assert offenders == [], (
        f"runtimes.json seeds hardware a fresh install does not have: {offenders}"
    )


def test_seed_file_ships_only_cloud_provider_anchors():
    types = {e["id"]: e.get("runtime_type") for e in _seed_entries()}
    local = {slug: t for slug, t in types.items() if t not in CLOUD_ANCHOR_TYPES}
    assert local == {}, f"local example runtimes in the seed: {local}"
    for entry in _seed_entries():
        assert (entry.get("endpoint") or "").startswith("https://"), entry["id"]


# ── 2. empty database ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seeding_an_empty_database_creates_no_local_runtime_and_no_host(async_session):
    inserted, _ = await seed_runtimes(async_session)
    await seed_hosts(async_session)

    rows = (await async_session.exec(select(Runtime))).scalars().all()
    assert inserted == len(rows) == len(_seed_entries())
    assert {r.runtime_type for r in rows} <= CLOUD_ANCHOR_TYPES
    assert not [r.slug for r in rows if _is_address(urlsplit(r.endpoint or "").hostname)]
    assert (await async_session.exec(select(Host))).scalars().all() == []
    assert (await async_session.exec(select(Agent))).scalars().all() == []


# ── 3. existing database ──────────────────────────────────────────────────


def _snapshot(rows) -> list[dict]:
    return sorted((r.model_dump() for r in rows), key=lambda d: str(d.get("slug") or d.get("name")))


@pytest.mark.asyncio
async def test_seeding_an_existing_database_changes_no_row(async_session):
    """An install seeded by the OLD file keeps every row, edited or not.

    The rows below are the old example slugs (as an existing install has them,
    some edited by the operator), the current cloud anchors, a host and an
    agent bound to an example runtime. The seed run must not add, remove or
    touch any of them.
    """
    old_examples = [
        Runtime(slug="qwen-general", display_name="My Qwen", runtime_type="vllm_docker",
                endpoint="http://192.0.2.10:8000/v1", enabled=True),
        Runtime(slug="qwen-coder-lms", display_name="LM Studio", runtime_type="lmstudio",
                endpoint="http://192.0.2.10:1234/v1", enabled=False),
        Runtime(slug="hermes-vllm", display_name="Hermes", runtime_type="hermes",
                endpoint="http://192.0.2.10:8000/v1", enabled=False, single_instance=True),
        Runtime(slug="unsloth-studio", display_name="Unsloth Studio", runtime_type="unsloth",
                endpoint="http://192.0.2.10:8888", enabled=False),
        Runtime(slug="llamacpp-example", display_name="llama.cpp", runtime_type="llamacpp_docker",
                endpoint="http://192.0.2.10:8080/v1", enabled=False),
        Runtime(slug="gemma4-nvfp4", display_name="Gemma", runtime_type="vllm_docker",
                endpoint="http://192.0.2.10:8002/v1", enabled=False),
        Runtime(slug="my-box-runtime", display_name="My box", runtime_type="openai_compatible",
                endpoint="http://192.0.2.30:8000/v1", enabled=True),
    ]
    taken = {r.slug for r in old_examples}
    anchors = [
        Runtime(slug=e["id"], display_name=e.get("display_name") or e["id"],
                runtime_type=e["runtime_type"], endpoint=e["endpoint"],
                model_identifier=e.get("model_identifier"), enabled=True)
        for e in _seed_entries() if e["id"] not in taken
    ]
    # An existing install has run the boot seeders before: its own box is a
    # host row and the runtime on it is already linked.
    host = Host(slug="my-box", display_name="My box", kind="ssh", ssh_host="192.0.2.30")
    async_session.add(host)
    await async_session.commit()
    old_examples[-1].host_id = host.id
    for row in old_examples + anchors:
        async_session.add(row)
    await async_session.commit()
    agent = Agent(name="Hermes", agent_runtime="host", runtime_id=old_examples[2].id)
    async_session.add(agent)
    await async_session.commit()
    async_session.expire_all()  # compare DB state with DB state, not the identity map

    before = (
        _snapshot((await async_session.exec(select(Runtime))).scalars().all()),
        _snapshot((await async_session.exec(select(Host))).scalars().all()),
        _snapshot((await async_session.exec(select(Agent))).scalars().all()),
    )
    inserted, skipped = await seed_runtimes(async_session)
    await seed_hosts(async_session)
    async_session.expire_all()
    after = (
        _snapshot((await async_session.exec(select(Runtime))).scalars().all()),
        _snapshot((await async_session.exec(select(Host))).scalars().all()),
        _snapshot((await async_session.exec(select(Agent))).scalars().all()),
    )

    assert inserted == 0
    for b, a in zip(before, after):
        for rb, ra in zip(b, a):
            diff = {k: (rb[k], ra.get(k)) for k in rb if rb[k] != ra.get(k)}
            assert not diff, (rb.get("slug") or rb.get("name"), diff)
    assert after == before


# ── 4. the real chain on Postgres: migrations + boot seeders ──────────────


def _admin_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_fresh_postgres_install_has_no_examples_and_no_demo_agent():
    """``alembic upgrade head`` on an EMPTY database, then the boot seeders.

    Runs in its own throw-away database (created and dropped here), so the
    lane's shared test database is never touched.
    """
    import asyncpg
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.services.slot_runtimes import ensure_slot_runtimes

    base = os.environ.get("MC_TEST_DATABASE_URL", "")
    parts = urlsplit(base)
    fresh_name = f"test_fresh_seed_{uuid.uuid4().hex[:8]}"
    fresh_url = urlunsplit(parts._replace(path=f"/{fresh_name}"))

    admin = await asyncpg.connect(_admin_url(base))
    try:
        await admin.execute(f'CREATE DATABASE "{fresh_name}"')
    finally:
        await admin.close()
    try:
        env = {**os.environ, "DATABASE_URL": fresh_url}
        # The alembic CLI next to this interpreter: ``python -m alembic`` from
        # backend/ would import the migrations folder instead of the package.
        alembic_bin = Path(sys.executable).with_name("alembic")
        done = subprocess.run(
            [str(alembic_bin), "upgrade", "head"],
            cwd=BACKEND, env=env, capture_output=True, text=True, timeout=600,
        )
        assert done.returncode == 0, done.stderr[-4000:]

        engine = create_async_engine(fresh_url)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                await seed_runtimes(session)
                await seed_hosts(session)
                await ensure_slot_runtimes(session)
                runtimes = (await session.exec(select(Runtime))).scalars().all()
                agents = (await session.exec(select(Agent))).scalars().all()
                hosts = (await session.exec(select(Host))).scalars().all()
        finally:
            await engine.dispose()

        assert sorted(r.slug for r in runtimes) == sorted(e["id"] for e in _seed_entries())
        assert {r.runtime_type for r in runtimes} <= CLOUD_ANCHOR_TYPES
        assert [a.name for a in agents] == []
        assert hosts == []
    finally:
        admin = await asyncpg.connect(_admin_url(base))
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{fresh_name}" WITH (FORCE)')
        finally:
            await admin.close()
