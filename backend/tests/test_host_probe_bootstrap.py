"""Box-Wizard backend (PR 4) — host probe, bootstrap state machine, launch templates.

Only RFC 5737 placeholder IPs (192.0.2.x) in fixtures — public repo.

Everything that talks to a box is exercised through a fake ``_ssh_run``: the
tests assert on WHICH commands would run and what the parser/state machine
makes of their output. The one real SSH round-trip lives in the E2E run
documented in the PR description, not here.
"""

import json
import uuid
from unittest.mock import patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.host import Host
from app.services import host_bootstrap, host_probe
from app.services.host_resolver import ResolvedHost
from app.services.launch_template import (
    build_launch_command,
    render_launch_template,
)
from tests.conftest import test_engine

# ── Fixtures / helpers ───────────────────────────────────────────────────────

_FULL_PROBE_STDOUT = """
MC_PROBE_BEGIN
arch=aarch64
os=Linux
kernel=6.11.0-1004-nvidia
user=mcuser
gpu=NVIDIA GB10, 131072 MiB
nvidia_smi=/usr/bin/nvidia-smi
docker_version=Docker version 27.3.1, build ce12230
docker_runtimes=map[io.containerd.runc.v2:{runc [] } nvidia:{nvidia-container-runtime [] } runc:{runc [] }]
nvidia_ctk=/usr/bin/nvidia-ctk
disk_free_kb=838860800
ram_kb=125829120
ram_bytes=
in_docker_group=yes
sudo_nopasswd=yes
pkg_manager=/usr/bin/apt-get
MC_PROBE_END
""".strip()

# A fresh x86 box: no GPU, no docker, no sudo without password.
_BARE_PROBE_STDOUT = """
MC_PROBE_BEGIN
arch=x86_64
os=Linux
kernel=6.8.0-45-generic
user=ubuntu
nvidia_smi=
docker_version=
docker_runtimes=
nvidia_ctk=
disk_free_kb=104857600
ram_kb=65536000
ram_bytes=
in_docker_group=no
sudo_nopasswd=no
pkg_manager=/usr/bin/apt-get
MC_PROBE_END
""".strip()


@pytest.fixture(autouse=True)
def _redis_singleton(fake_redis):
    """Point the module-level redis singleton at fakeredis.

    host_bootstrap calls ``get_redis()`` directly (not via Depends), so the
    ``client`` fixture's dependency override doesn't reach it — the service
    tests here would otherwise try to open a real connection.
    """
    import app.redis_client

    original = app.redis_client._redis
    app.redis_client._redis = fake_redis
    yield
    app.redis_client._redis = original


def _resolved(**overrides) -> ResolvedHost:
    return ResolvedHost(
        ssh_host=overrides.pop("ssh_host", "192.0.2.10"),
        ssh_user=overrides.pop("ssh_user", "mcuser"),
        ssh_key_path=overrides.pop("ssh_key_path", "/home/mcuser/.ssh/id_ed25519"),
        kind="ssh",
        **overrides,
    )


class FakeSsh:
    """Scripted ``_ssh_run`` replacement.

    ``responses`` maps a substring of the command to ``(stdout, stderr, exit)``.
    The first matching entry wins, so a specific pattern must be listed before
    a more general one. Unmatched commands return exit 127 — a test that
    triggers an unexpected command fails loudly instead of silently passing.
    """

    def __init__(self, responses: list[tuple[str, tuple[str, str, int]]]):
        self.responses = responses
        self.commands: list[str] = []

    async def __call__(self, command, *, host=None, timeout=None):
        self.commands.append(command)
        for needle, result in self.responses:
            if needle in command:
                return result
        return ("", f"command not scripted: {command[:60]}", 127)

    def ran(self, needle: str) -> bool:
        return any(needle in c for c in self.commands)


def _patch_ssh(fake: FakeSsh):
    """``_ssh_run`` is imported lazily inside the services, so patching the
    attribute on runtime_manager is what both call sites actually see."""
    return patch("app.services.runtime_manager._ssh_run", new=fake)


# ── Probe parsing ────────────────────────────────────────────────────────────


def test_parse_probe_full_inventory():
    inv = host_probe.parse_probe_output(_FULL_PROBE_STDOUT)

    assert inv["arch"] == "aarch64"
    assert inv["os"] == "Linux"
    assert inv["user"] == "mcuser"
    assert inv["gpus"] == [{"name": "NVIDIA GB10", "vram_gb": 128.0}]
    assert inv["docker"]["installed"] is True
    assert inv["docker"]["version"].startswith("Docker version 27.3.1")
    assert inv["docker"]["nvidia_runtime"] is True
    assert inv["docker"]["toolkit_installed"] is True
    assert inv["disk_free_gb"] == 800.0
    assert inv["ram_gb"] == 120.0
    assert inv["in_docker_group"] is True
    assert inv["sudo_nopasswd"] is True


def test_parse_probe_bare_box_reports_gaps_not_errors():
    """A fresh box answers everything — with empty values. That is a valid
    inventory (and exactly what the wizard's traffic lights are for)."""
    inv = host_probe.parse_probe_output(_BARE_PROBE_STDOUT)

    assert inv["arch"] == "x86_64"
    assert inv["gpus"] == []
    assert inv["nvidia_smi"] is False
    assert inv["docker"]["installed"] is False
    assert inv["docker"]["nvidia_runtime"] is False
    assert inv["sudo_nopasswd"] is False
    assert inv["ram_gb"] == 62.5


def test_parse_probe_ignores_ssh_banner_outside_markers():
    """MOTD/banner text must not end up in the inventory."""
    noisy = "Welcome to Ubuntu 24.04 LTS\nLast login: Tue\n" + _BARE_PROBE_STDOUT + "\nbye"
    inv = host_probe.parse_probe_output(noisy)
    assert inv["arch"] == "x86_64"
    assert inv["os"] == "Linux"


def test_parse_probe_multiple_gpus_and_unparsable_memory():
    out = (
        "MC_PROBE_BEGIN\narch=x86_64\nos=Linux\n"
        "gpu=NVIDIA RTX 6000 Ada, 49140 MiB\n"
        "gpu=NVIDIA RTX 6000 Ada, N/A\n"
        "MC_PROBE_END"
    )
    inv = host_probe.parse_probe_output(out)
    assert len(inv["gpus"]) == 2
    assert inv["gpus"][0]["vram_gb"] == 48.0
    # A GPU we can see but not size stays in the list — dropping it would
    # understate the box.
    assert inv["gpus"][1]["vram_gb"] is None


def test_parse_probe_macos_style_ram_bytes():
    out = "MC_PROBE_BEGIN\narch=arm64\nos=Darwin\nram_kb=\nram_bytes=17179869184\nMC_PROBE_END"
    assert host_probe.parse_probe_output(out)["ram_gb"] == 16.0


@pytest.mark.asyncio
async def test_probe_host_ssh_error_is_unreachable_not_an_exception():
    async def boom(*_a, **_kw):
        raise OSError("Connection refused")

    with patch("app.services.runtime_manager._ssh_run", new=boom):
        result = await host_probe.probe_host(_resolved())

    assert result["reachable"] is False
    assert "Connection refused" in result["reason"]
    # Same shape as a success — the frontend never branches on key existence.
    assert result["gpus"] == []
    assert result["docker"]["installed"] is False


@pytest.mark.asyncio
async def test_probe_host_empty_output_is_unreachable():
    fake = FakeSsh([("MC_PROBE_BEGIN", ("", "bash: command not found", 127))])
    with _patch_ssh(fake):
        result = await host_probe.probe_host(_resolved())
    assert result["reachable"] is False
    assert "command not found" in result["reason"]


@pytest.mark.asyncio
async def test_probe_host_success_returns_inventory_and_raw():
    fake = FakeSsh([("MC_PROBE_BEGIN", (_FULL_PROBE_STDOUT, "", 0))])
    with _patch_ssh(fake):
        result = await host_probe.probe_host(_resolved())

    assert result["reachable"] is True
    assert result["reason"] is None
    assert result["arch"] == "aarch64"
    assert result["raw"] == _FULL_PROBE_STDOUT
    # One SSH round-trip, not one per fact.
    assert len(fake.commands) == 1


# ── Probe endpoint ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_endpoint_adhoc_credentials(auth_client):
    fake = FakeSsh([("MC_PROBE_BEGIN", (_FULL_PROBE_STDOUT, "", 0))])
    with _patch_ssh(fake):
        resp = await auth_client.post(
            "/api/v1/hosts/probe",
            json={"ssh_host": "192.0.2.10", "ssh_user": "mcuser",
                  "ssh_key_path": "/home/mcuser/.ssh/id_ed25519"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reachable"] is True
    assert body["gpus"][0]["vram_gb"] == 128.0


@pytest.mark.asyncio
async def test_probe_endpoint_unreachable_is_200_not_500(auth_client):
    """The wizard's step 1 must be able to SHOW an unreachable box."""
    async def boom(*_a, **_kw):
        raise TimeoutError("timed out")

    with patch("app.services.runtime_manager._ssh_run", new=boom):
        resp = await auth_client.post(
            "/api/v1/hosts/probe", json={"ssh_host": "192.0.2.99"}
        )
    assert resp.status_code == 200
    assert resp.json()["reachable"] is False


@pytest.mark.asyncio
async def test_probe_endpoint_without_host_is_422(auth_client):
    resp = await auth_client.post("/api/v1/hosts/probe", json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_probe_endpoint_existing_host_id(auth_client):
    created = (await auth_client.post("/api/v1/hosts", json={
        "slug": "probe-box", "display_name": "Probe Box", "kind": "ssh",
        "ssh_host": "192.0.2.10", "ssh_user": "mcuser",
    })).json()

    fake = FakeSsh([("MC_PROBE_BEGIN", (_BARE_PROBE_STDOUT, "", 0))])
    with _patch_ssh(fake):
        resp = await auth_client.post(
            "/api/v1/hosts/probe", json={"host_id": created["id"]}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["arch"] == "x86_64"


@pytest.mark.asyncio
async def test_probe_endpoint_rejects_non_ssh_host(auth_client):
    await auth_client.post("/api/v1/hosts", json={
        "slug": "local-box", "display_name": "Local", "kind": "local",
    })
    resp = await auth_client.post("/api/v1/hosts/probe", json={"host_id": "local-box"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_probe_endpoint_requires_admin(client):
    """Viewer token: ssh_host in a request body decides where a command lands."""
    from app.auth import create_access_token
    from app.models.user import User

    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email=f"v-{uid.hex[:8]}@mc.local", name="V",
                   role="viewer", is_active=True))
        await s.commit()
    token = create_access_token(str(uid), "viewer")

    resp = await client.post(
        "/api/v1/hosts/probe",
        json={"ssh_host": "192.0.2.10"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


# ── Bootstrap state machine ──────────────────────────────────────────────────


async def _make_host(**overrides) -> Host:
    host = Host(
        slug=overrides.pop("slug", "boot-box"),
        display_name="Boot Box",
        kind="ssh",
        ssh_host="192.0.2.10",
        ssh_user="mcuser",
        **overrides,
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(host)
        await s.commit()
        await s.refresh(host)
    return host


@pytest.mark.asyncio
async def test_bootstrap_fully_prepared_box_is_a_noop(fake_redis):
    """Idempotency: everything present → nothing runs, status done, no actions."""
    fake = FakeSsh([("MC_PROBE_BEGIN", (_FULL_PROBE_STDOUT, "", 0))])
    host_id = str(uuid.uuid4())

    with _patch_ssh(fake):
        await host_bootstrap.run_bootstrap(host_id, _resolved())

    status = await host_bootstrap.get_status(host_id)
    assert status["status"] == host_bootstrap.STATUS_DONE
    assert status["actions"] == []
    assert not fake.ran("get-docker.sh")
    assert not fake.ran("apt-get install")
    assert not fake.ran("usermod")
    log = await host_bootstrap.read_log(host_id)
    assert any("nichts zu tun" in ln["text"] for ln in log["lines"])


@pytest.mark.asyncio
async def test_bootstrap_installs_docker_when_missing(fake_redis):
    """Docker missing + sudo available → installer downloaded, hashed, run."""
    fake = FakeSsh([
        ("MC_PROBE_BEGIN", (_BARE_PROBE_STDOUT.replace("sudo_nopasswd=no", "sudo_nopasswd=yes"), "", 0)),
        ("sudo -n true", ("", "", 0)),
        ("get.docker.com", ("abc123  /tmp/mc-get-docker.sh", "", 0)),
        ("sh /tmp/mc-get-docker.sh", ("Docker installed", "", 0)),
        ("usermod -aG docker", ("", "", 0)),
    ])
    host_id = str(uuid.uuid4())

    with _patch_ssh(fake):
        await host_bootstrap.run_bootstrap(host_id, _resolved())

    status = await host_bootstrap.get_status(host_id)
    assert status["status"] == host_bootstrap.STATUS_DONE
    assert "docker_installed" in status["actions"]
    assert "docker_group_added" in status["actions"]
    # No GPU on this box → the toolkit path must stay untouched.
    assert not fake.ran("nvidia-container-toolkit")
    # The action was logged BEFORE it ran.
    log = await host_bootstrap.read_log(host_id)
    texts = [ln["text"] for ln in log["lines"]]
    announce = next(i for i, t in enumerate(texts) if "Installer wird geladen" in t)
    done = next(i for i, t in enumerate(texts) if t == "Docker installiert.")
    assert announce < done
    # No driver work — a CPU-only box gets a hint, never an install.
    assert any("Treiber installiert MC bewusst nicht" in t for t in texts)


@pytest.mark.asyncio
async def test_bootstrap_without_sudo_stops_with_needs_sudo(fake_redis):
    """No passwordless sudo → stop with a copy-pasteable command, never hang."""
    fake = FakeSsh([
        ("MC_PROBE_BEGIN", (_BARE_PROBE_STDOUT, "", 0)),
        ("sudo -n true", ("", "sudo: a password is required", 1)),
    ])
    host_id = str(uuid.uuid4())

    with _patch_ssh(fake):
        await host_bootstrap.run_bootstrap(host_id, _resolved())

    status = await host_bootstrap.get_status(host_id)
    assert status["status"] == host_bootstrap.STATUS_NEEDS_SUDO
    assert status["phase"] == "docker"
    assert "NOPASSWD" in status["message"]
    assert not fake.ran("get-docker.sh")


@pytest.mark.asyncio
async def test_bootstrap_installs_nvidia_toolkit_when_gpu_present(fake_redis):
    """GPU + docker, but no toolkit → repo + apt install + ctk configure."""
    stdout = (
        _FULL_PROBE_STDOUT
        .replace("nvidia_ctk=/usr/bin/nvidia-ctk", "nvidia_ctk=")
        .replace(
            "docker_runtimes=map[io.containerd.runc.v2:{runc [] } nvidia:{nvidia-container-runtime [] } runc:{runc [] }]",
            "docker_runtimes=map[runc:{runc [] }]",
        )
    )
    fake = FakeSsh([
        ("MC_PROBE_BEGIN", (stdout, "", 0)),
        ("sudo -n true", ("", "", 0)),
        ("libnvidia-container/gpgkey", ("", "", 0)),
        ("apt-get install", ("Setting up nvidia-container-toolkit", "", 0)),
        ("nvidia-ctk runtime configure", ("", "", 0)),
    ])
    host_id = str(uuid.uuid4())

    with _patch_ssh(fake):
        await host_bootstrap.run_bootstrap(host_id, _resolved())

    status = await host_bootstrap.get_status(host_id)
    assert status["status"] == host_bootstrap.STATUS_DONE
    assert "nvidia_toolkit_installed" in status["actions"]
    # Docker was already there — the installer must not run.
    assert not fake.ran("get-docker.sh")


@pytest.mark.asyncio
async def test_bootstrap_non_apt_box_warns_instead_of_guessing(fake_redis):
    stdout = (
        _FULL_PROBE_STDOUT
        .replace("nvidia_ctk=/usr/bin/nvidia-ctk", "nvidia_ctk=")
        .replace("nvidia:{nvidia-container-runtime [] } ", "")
        .replace("pkg_manager=/usr/bin/apt-get", "pkg_manager=/usr/bin/dnf")
    )
    fake = FakeSsh([
        ("MC_PROBE_BEGIN", (stdout, "", 0)),
        ("sudo -n true", ("", "", 0)),
    ])
    host_id = str(uuid.uuid4())

    with _patch_ssh(fake):
        await host_bootstrap.run_bootstrap(host_id, _resolved())

    status = await host_bootstrap.get_status(host_id)
    assert status["status"] == host_bootstrap.STATUS_DONE
    assert "nvidia_toolkit_installed" not in status["actions"]
    assert not fake.ran("apt-get install")
    log = await host_bootstrap.read_log(host_id)
    assert any(ln["level"] == "warn" and "kein apt" in ln["text"] for ln in log["lines"])


@pytest.mark.asyncio
async def test_bootstrap_ssh_failure_is_failed_status(fake_redis):
    async def boom(*_a, **_kw):
        raise OSError("no route to host")

    host_id = str(uuid.uuid4())
    with patch("app.services.runtime_manager._ssh_run", new=boom):
        await host_bootstrap.run_bootstrap(host_id, _resolved())

    status = await host_bootstrap.get_status(host_id)
    assert status["status"] == host_bootstrap.STATUS_FAILED
    assert "no route to host" in status["message"]


@pytest.mark.asyncio
async def test_bootstrap_installer_failure_is_reported(fake_redis):
    fake = FakeSsh([
        ("MC_PROBE_BEGIN", (_BARE_PROBE_STDOUT.replace("sudo_nopasswd=no", "sudo_nopasswd=yes"), "", 0)),
        ("sudo -n true", ("", "", 0)),
        ("get.docker.com", ("", "curl: (6) Could not resolve host", 6)),
    ])
    host_id = str(uuid.uuid4())

    with _patch_ssh(fake):
        await host_bootstrap.run_bootstrap(host_id, _resolved())

    status = await host_bootstrap.get_status(host_id)
    assert status["status"] == host_bootstrap.STATUS_FAILED
    assert "Could not resolve host" in status["message"]


# ── Log polling contract ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_log_cursor_returns_only_new_lines(fake_redis):
    host_id = str(uuid.uuid4())
    run = host_bootstrap._Run(host_id, _resolved())
    await run.log("erste Zeile")
    await run.log("zweite Zeile")

    first = await host_bootstrap.read_log(host_id, 0)
    assert [ln["text"] for ln in first["lines"]] == ["erste Zeile", "zweite Zeile"]
    assert first["cursor"] == 2
    assert first["status"] == "idle"  # no status document written yet

    await run.log("dritte Zeile")
    second = await host_bootstrap.read_log(host_id, first["cursor"])
    assert [ln["text"] for ln in second["lines"]] == ["dritte Zeile"]
    assert second["cursor"] == 3

    # Polling past the end is empty, not an error, and keeps the cursor stable.
    third = await host_bootstrap.read_log(host_id, second["cursor"])
    assert third["lines"] == []
    assert third["cursor"] == 3


@pytest.mark.asyncio
async def test_start_bootstrap_clears_the_previous_run_log(fake_redis):
    host_id = str(uuid.uuid4())
    run = host_bootstrap._Run(host_id, _resolved())
    await run.log("Zeile aus dem alten Lauf")

    fake = FakeSsh([("MC_PROBE_BEGIN", (_FULL_PROBE_STDOUT, "", 0))])
    with _patch_ssh(fake):
        await host_bootstrap.start_bootstrap(host_id, _resolved())
        # Let the spawned task finish before asserting.
        import asyncio
        for _ in range(50):
            await asyncio.sleep(0)
            if (await host_bootstrap.get_status(host_id))["status"] != "running":
                break

    log = await host_bootstrap.read_log(host_id)
    assert not any("alten Lauf" in ln["text"] for ln in log["lines"])


@pytest.mark.asyncio
async def test_bootstrap_endpoint_starts_and_polls(auth_client, fake_redis):
    host = await _make_host(slug="wizard-box")

    fake = FakeSsh([("MC_PROBE_BEGIN", (_FULL_PROBE_STDOUT, "", 0))])
    with _patch_ssh(fake):
        resp = await auth_client.post(f"/api/v1/hosts/{host.id}/bootstrap")
        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] == "started"

        import asyncio
        for _ in range(50):
            await asyncio.sleep(0)
            poll = await auth_client.get(f"/api/v1/hosts/{host.id}/bootstrap/log")
            if poll.json()["status"] != "running":
                break

    body = poll.json()
    assert body["status"] == "done"
    assert body["running"] is False
    assert body["cursor"] == len(body["lines"])


@pytest.mark.asyncio
async def test_bootstrap_endpoint_409_while_running(auth_client, fake_redis):
    host = await _make_host(slug="busy-box")
    await fake_redis.set(
        host_bootstrap.status_key(str(host.id)),
        json.dumps({"status": "running", "phase": "docker"}),
    )
    resp = await auth_client.post(f"/api/v1/hosts/{host.id}/bootstrap")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_bootstrap_endpoint_404_and_400(auth_client, fake_redis):
    assert (await auth_client.post(f"/api/v1/hosts/{uuid.uuid4()}/bootstrap")).status_code == 404
    await auth_client.post("/api/v1/hosts", json={
        "slug": "just-local", "display_name": "Local", "kind": "local",
    })
    assert (await auth_client.post("/api/v1/hosts/just-local/bootstrap")).status_code == 400


@pytest.mark.asyncio
async def test_bootstrap_log_idle_before_any_run(auth_client, fake_redis):
    host = await _make_host(slug="never-booted")
    resp = await auth_client.get(f"/api/v1/hosts/{host.id}/bootstrap/log")
    assert resp.status_code == 200
    assert resp.json() == {
        "host_id": str(host.id),
        "status": "idle",
        "phase": None,
        "message": None,
        "actions": [],
        "running": False,
        "lines": [],
        "cursor": 0,
    }


# ── launch_template rendering ────────────────────────────────────────────────


def test_render_launch_template_substitutes_known_placeholders():
    out = render_launch_template(
        "docker run --name {container_name} -p {port}:{port} {image} -hf {model}",
        {"container_name": "mc-x", "port": 8080, "image": "img:tag", "model": "Qwen/X"},
    )
    assert out == "docker run --name mc-x -p 8080:8080 img:tag -hf Qwen/X"


def test_render_launch_template_leaves_docker_format_braces_alone():
    """``{{.State.Status}}`` must survive — this is why it isn't str.format."""
    out = render_launch_template(
        "docker inspect -f '{{.State.Status}}' {container_name}",
        {"container_name": "mc-x"},
    )
    assert out == "docker inspect -f '{{.State.Status}}' mc-x"


def test_render_launch_template_rejects_unknown_placeholder():
    with pytest.raises(ValueError, match="Unbekannte Platzhalter"):
        render_launch_template("run {gpus} {model}", {"model": "m"})


def test_render_launch_template_rejects_missing_value():
    with pytest.raises(ValueError, match="Kein Wert für Platzhalter"):
        render_launch_template("run {model} on {port}", {"model": "m"})


def test_build_launch_command_llamacpp_default_template():
    cmd = build_launch_command(
        engine="llamacpp_docker",
        model_identifier="Qwen/Qwen3-8B-GGUF",
        slug="qwen3-8b",
        port=8080,
    )
    assert "--label mc.runtime.slug=qwen3-8b" in cmd
    assert "-p 8080:8080" in cmd
    assert "ghcr.io/ggml-org/llama.cpp:server-cuda" in cmd
    assert "-hf Qwen/Qwen3-8B-GGUF" in cmd
    assert "--name mc-qwen3-8b" in cmd


def test_build_launch_command_vllm_default_template():
    cmd = build_launch_command(
        engine="vllm_docker",
        model_identifier="unsloth/Qwen3.6-27B-NVFP4",
        slug="qwen36-27b",
        port=8000,
    )
    assert "--gpus all" in cmd
    assert "--model unsloth/Qwen3.6-27B-NVFP4" in cmd
    assert "--label mc.runtime.slug=qwen36-27b" in cmd


def test_build_launch_command_prefers_recipe_template():
    cmd = build_launch_command(
        engine="llamacpp_docker",
        model_identifier="M",
        slug="custom",
        port=9000,
        launch_template=(
            "docker run --name {container_name} --label mc.runtime.slug={slug} "
            "-p {port}:{port} custom/image -hf {model} --flash-attn"
        ),
    )
    assert cmd.startswith("docker run --name mc-custom")
    assert "--flash-attn" in cmd
    assert "custom/image" in cmd


def test_build_launch_command_requires_the_runtime_slug_label():
    """Without the label MC can start the container but never stop it again."""
    with pytest.raises(ValueError, match="mc.runtime.slug"):
        build_launch_command(
            engine="llamacpp_docker",
            model_identifier="M",
            slug="nolabel",
            port=8080,
            launch_template="docker run --name {container_name} -hf {model}",
        )


def test_build_launch_command_rejects_bad_slug_and_port():
    with pytest.raises(ValueError, match="alphanumerisch"):
        build_launch_command(
            engine="llamacpp_docker", model_identifier="M", slug="rm -rf /", port=8080
        )
    with pytest.raises(ValueError, match="Port"):
        build_launch_command(
            engine="llamacpp_docker", model_identifier="M", slug="ok", port=99999
        )


def test_build_launch_command_unsupported_engine():
    with pytest.raises(ValueError, match="sparkrun"):
        build_launch_command(
            engine="sparkrun", model_identifier="@official/x", slug="s", port=8000
        )
