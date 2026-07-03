"""Host Registry (ADR-048) — Model + Bootstrap Seed Tests (Task B1).

Covers:
- Host model: defaults, persistence, unique slug
- Runtime.host_id FK (nullable, host delete → SET NULL semantics via ondelete)
- Seed: dgx-spark from settings, porsche from the legacy unsloth-porsche runtime
- Seed idempotency (second run = no-op)
- Fresh install without dgx_ssh_host → 0 hosts, no error

Public repo rule: only doc placeholder IPs (192.0.2.x, RFC 5737) and
dummy MACs in fixtures.
"""
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.host import Host
from app.models.runtime import Runtime
from app.services.host_seeder import seed_hosts


def _make_runtime(slug: str = "test-runtime", **kwargs) -> Runtime:
    defaults = dict(
        display_name=slug,
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.10:8000/v1",
    )
    defaults.update(kwargs)
    return Runtime(slug=slug, **defaults)


# ── Host model ─────────────────────────────────────────────────────────────


async def test_host_model_persists_with_defaults(session):
    host = Host(slug="dgx-spark", display_name="DGX Spark", kind="ssh", ssh_host="192.0.2.10")
    session.add(host)
    await session.commit()
    await session.refresh(host)

    assert host.id is not None
    assert host.kind == "ssh"
    assert host.enabled is True
    assert host.power_managed is False
    assert host.ui_order == 0
    assert host.ssh_user is None
    assert host.control_url is None
    assert host.notes is None
    assert host.created_at is not None


async def test_host_slug_is_unique(session):
    session.add(Host(slug="dup", display_name="A", kind="ssh"))
    await session.commit()
    session.add(Host(slug="dup", display_name="B", kind="local"))
    with pytest.raises(Exception):  # sqlite IntegrityError via unique index
        await session.commit()


async def test_runtime_host_id_fk_nullable(session):
    host = Host(slug="box", display_name="Box", kind="ssh", ssh_host="192.0.2.20")
    session.add(host)
    await session.commit()

    rt = _make_runtime("bound-rt", host_id=host.id)
    rt_free = _make_runtime("free-rt")
    session.add(rt)
    session.add(rt_free)
    await session.commit()
    await session.refresh(rt)
    await session.refresh(rt_free)

    assert rt.host_id == host.id
    assert rt_free.host_id is None


# ── Fresh install: 0 hosts, 0 errors ───────────────────────────────────────


async def test_fresh_install_without_dgx_env_seeds_nothing(session, monkeypatch):
    monkeypatch.setattr(settings, "dgx_ssh_host", "")

    inserted, linked = await seed_hosts(session)

    assert (inserted, linked) == (0, 0)
    result = await session.exec(select(Host))
    assert result.scalars().all() == []


# ── dgx-spark seed from settings ───────────────────────────────────────────


async def test_seed_dgx_spark_from_settings(session, monkeypatch):
    monkeypatch.setattr(settings, "dgx_ssh_host", "192.0.2.10")
    monkeypatch.setattr(settings, "dgx_ssh_user", "testuser")
    monkeypatch.setattr(settings, "dgx_ssh_key_path", "/keys/id_test")

    inserted, _ = await seed_hosts(session)
    assert inserted == 1

    result = await session.exec(select(Host).where(Host.slug == "dgx-spark"))
    host = result.scalars().one()
    assert host.kind == "ssh"
    assert host.ssh_host == "192.0.2.10"
    assert host.ssh_user == "testuser"
    assert host.ssh_key_path == "/keys/id_test"
    assert host.enabled is True


async def test_seed_is_idempotent(session, monkeypatch):
    monkeypatch.setattr(settings, "dgx_ssh_host", "192.0.2.10")

    first = await seed_hosts(session)
    second = await seed_hosts(session)

    assert first[0] == 1
    assert second == (0, 0)  # second run: nothing inserted, nothing newly linked
    result = await session.exec(select(Host))
    assert len(result.scalars().all()) == 1


async def test_seed_skips_when_ssh_host_already_registered(session, monkeypatch):
    """User already registered the box (under a different slug) → no duplicate."""
    monkeypatch.setattr(settings, "dgx_ssh_host", "192.0.2.10")
    session.add(Host(slug="my-box", display_name="My Box", kind="ssh", ssh_host="192.0.2.10"))
    await session.commit()

    inserted, _ = await seed_hosts(session)

    assert inserted == 0
    result = await session.exec(select(Host))
    assert len(result.scalars().all()) == 1


# ── porsche seed from the legacy runtime ───────────────────────────────────


async def test_seed_porsche_from_legacy_runtime(session, monkeypatch):
    monkeypatch.setattr(settings, "dgx_ssh_host", "")
    session.add(
        _make_runtime(
            "unsloth-porsche",
            runtime_type="unsloth_porsche",
            endpoint="http://192.0.2.77:8000/v1",
            host="192.0.2.77",
            control_url="http://192.0.2.77:5555",
            wol_mac_address="00:00:5E:00:53:01",
            power_managed=True,
        )
    )
    await session.commit()

    inserted, linked = await seed_hosts(session)

    assert inserted == 1
    result = await session.exec(select(Host).where(Host.slug == "porsche"))
    host = result.scalars().one()
    assert host.kind == "flask_wol"
    assert host.ssh_host == "192.0.2.77"
    assert host.control_url == "http://192.0.2.77:5555"
    assert host.wol_mac_address == "00:00:5E:00:53:01"
    assert host.power_managed is True

    # Runtime was bound to the host via endpoint IP
    assert linked == 1
    result = await session.exec(select(Runtime).where(Runtime.slug == "unsloth-porsche"))
    assert result.scalars().one().host_id == host.id


async def test_no_porsche_seed_without_control_url(session, monkeypatch):
    monkeypatch.setattr(settings, "dgx_ssh_host", "")
    session.add(_make_runtime("unsloth-porsche", runtime_type="unsloth_porsche"))
    await session.commit()

    inserted, linked = await seed_hosts(session)

    assert (inserted, linked) == (0, 0)


async def test_no_porsche_seed_for_disabled_runtime(session, monkeypatch):
    """The example seed from runtimes.json is enabled=false — an OSS
    fresh install must NOT materialize a phantom host from it
    (spec goal: 0 hosts, 0 errors without a real GPU box)."""
    monkeypatch.setattr(settings, "dgx_ssh_host", "")
    session.add(
        _make_runtime(
            "unsloth-porsche",
            runtime_type="unsloth_porsche",
            endpoint="http://192.0.2.20:8000/v1",
            host="192.0.2.20",
            control_url="http://192.0.2.20:5555",
            enabled=False,
        )
    )
    await session.commit()

    inserted, linked = await seed_hosts(session)

    assert (inserted, linked) == (0, 0)
    result = await session.exec(select(Host))
    assert result.scalars().all() == []


async def test_no_duplicate_porsche_after_slug_rename(session, monkeypatch):
    """Slug rename of the seeded porsche host (PATCH slug) must not create
    a duplicate host with the same control_url on the next boot —
    two rows with the same ssh_host make linking nondeterministic."""
    monkeypatch.setattr(settings, "dgx_ssh_host", "")
    session.add(
        _make_runtime(
            "unsloth-porsche",
            runtime_type="unsloth_porsche",
            endpoint="http://192.0.2.77:8000/v1",
            host="192.0.2.77",
            control_url="http://192.0.2.77:5555",
            power_managed=True,
        )
    )
    # The already-seeded host was renamed by the admin
    session.add(
        Host(
            slug="workstation",
            display_name="Workstation",
            kind="flask_wol",
            ssh_host="192.0.2.77",
            control_url="http://192.0.2.77:5555",
        )
    )
    await session.commit()

    inserted, _ = await seed_hosts(session)

    assert inserted == 0
    result = await session.exec(select(Host))
    assert [h.slug for h in result.scalars().all()] == ["workstation"]


# ── Runtime linking ────────────────────────────────────────────────────────


async def test_seed_links_runtimes_by_endpoint_ip(session, monkeypatch):
    monkeypatch.setattr(settings, "dgx_ssh_host", "192.0.2.10")
    session.add(_make_runtime("vllm-a", endpoint="http://192.0.2.10:8001/v1"))
    session.add(_make_runtime("cloud-x", runtime_type="cloud", endpoint="https://api.example.com/v1"))
    await session.commit()

    inserted, linked = await seed_hosts(session)
    assert inserted == 1
    assert linked == 1

    result = await session.exec(select(Host).where(Host.slug == "dgx-spark"))
    host = result.scalars().one()
    result = await session.exec(select(Runtime).where(Runtime.slug == "vllm-a"))
    assert result.scalars().one().host_id == host.id
    result = await session.exec(select(Runtime).where(Runtime.slug == "cloud-x"))
    assert result.scalars().one().host_id is None  # cloud runtime stays hostless


async def test_seed_never_overwrites_existing_binding(session, monkeypatch):
    """host_id != NULL is left untouched by the seed (user rebinding persists)."""
    monkeypatch.setattr(settings, "dgx_ssh_host", "192.0.2.10")
    other = Host(slug="other", display_name="Other", kind="ssh", ssh_host="192.0.2.99")
    session.add(other)
    await session.commit()
    session.add(_make_runtime("pinned", endpoint="http://192.0.2.10:8001/v1", host_id=other.id))
    await session.commit()

    await seed_hosts(session)

    result = await session.exec(select(Runtime).where(Runtime.slug == "pinned"))
    assert result.scalars().one().host_id == other.id
