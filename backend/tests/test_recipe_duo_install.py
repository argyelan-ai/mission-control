"""Installing a two-box (duo) recipe from the catalog — the generic install gap.

Before this, ``POST /local-registry/{slug}/install`` only knew single boxes:
the install command was rendered without any notion of a second box, and the
recipe's ``.env`` (``env_file``/``env_map``) was written only by a START. A duo
recipe whose installer itself reaches the worker (image + weights on both
boxes) therefore had nothing telling it WHICH worker. The rule now:

  * for ``topology.nodes >= 2`` the install picks a worker exactly like the
    start does (named one, else ``role=worker`` first),
  * writes the recipe's ``.env`` on the head (same upsert, backup and
    read-back as the start) BEFORE the install job is launched,
  * and renders the install template with the duo placeholders
    (``{worker_ssh}`` & co.) on top of the usual ones.

Single-box installs must stay exactly as they were — tested below too.

Also here: the seeded TensorFold recipe (its fields, and that its commands are
valid bash once rendered), plus the drop_page_cache flag's way from the
catalog into a recipe instance.

Only RFC 5737 / RFC 1918 placeholder addresses, no device names — public repo.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.host import Host
from app.models.local_recipe import LocalRecipe
from app.services import launch_template, local_registry, recipe_env, recipe_install, recipe_switcher
from app.services.host_resolver import ResolvedHost

TF_SLUG = "glm53-flash-exl3-tensorfold"
SEED = Path(__file__).resolve().parents[1] / "config" / "local-recipes.json"
MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
ENV_FILE = "~/code/mc-engines/duo-engine/.env"
DUO_INSTALL = "mkdir -p {src_dir}/duo-engine && cd {src_dir}/duo-engine && ./prepare.sh --worker {worker_ssh}"


# ── helpers ──────────────────────────────────────────────────────────────────


async def _host(session: AsyncSession, slug: str, *, ssh_host: str = "192.0.2.10", **kw) -> Host:
    host = Host(slug=slug, display_name=slug.upper(), kind="ssh", ssh_host=ssh_host, **kw)
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


async def _duo_recipe(session: AsyncSession, slug: str = "duo-engine", **kw) -> LocalRecipe:
    fields = dict(
        display_name="Duo Engine",
        engine="ssh_process",
        model_identifier="org/duo",
        launch_template="cd {src_dir}/duo-engine && ./start.sh",
        install_template=DUO_INSTALL,
        stop_template="cd {src_dir}/duo-engine && ./stop.sh",
        process_name="duo-engine",
        topology={"nodes": 2},
        port=8000,
        exclusive=True,
        env_file=ENV_FILE,
        env_map={"WORKER": "{worker_ssh}", "FABRIC_PEER": "{worker_fabric_ip}", "PORT": "{port}"},
    )
    fields.update(kw)
    recipe = LocalRecipe(slug=slug, **fields)
    session.add(recipe)
    await session.commit()
    await session.refresh(recipe)
    return recipe


class _Box:
    """SSH fake for the head: remembers the commands and the .env content,
    answers ``cat`` with what the upsert promised to write (same trick as the
    P3 tests — the read-back check runs for real)."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.content = ""
        self.lie = False

    async def __call__(self, command: str, **kwargs):
        self.commands.append(command)
        if command.startswith("cat "):
            return ("PORT=lie\n" if self.lie else self.content), "", 0
        values: dict[str, str] = {}
        for line in command.splitlines():
            if "MC_K=" in line and "MC_V=" in line:
                key = line.split("MC_K=", 1)[1].split(" ", 1)[0].strip("'")
                value = line.split("MC_V=", 1)[1].split(" ", 1)[0].strip("'")
                values[key] = value
        if values:
            parsed = recipe_env.parse_env_text(self.content)
            parsed.update(values)
            self.content = "".join(f"{k}={v}\n" for k, v in parsed.items())
        return "", "", 0


async def _install(auth_client, slug: str, body: dict, box: _Box | None = None):
    box = box or _Box()
    calls: list[dict] = []

    async def fake_start(host_id, slug_, resolved, **kwargs):
        # The .env must already be on the box when the install job starts.
        calls.append({"host_id": host_id, "slug": slug_, "env_at_start": box.content,
                      "resolved": resolved, **kwargs})

    with (
        patch("app.services.runtime_manager._ssh_run", box),
        patch.object(recipe_install, "start_install", fake_start),
    ):
        resp = await auth_client.post(f"/api/v1/local-registry/{slug}/install", json=body)
    return resp, calls, box


# ── 1. rendering: duo placeholders reach the install template only ──────────


def test_install_template_renders_the_duo_placeholders():
    command = launch_template.build_install_command(
        slug="duo-engine",
        install_template=DUO_INSTALL,
        duo={"worker_ssh": "mc@192.0.2.11", "worker_fabric_ip": "10.0.0.2"},
    )
    assert "--worker mc@192.0.2.11" in command
    assert "{" not in command


def test_duo_placeholders_are_the_env_map_address_placeholders():
    """One vocabulary for the .env and the install template — pinned, not copied."""
    assert launch_template.DUO_PLACEHOLDERS == recipe_env.ADDRESS_PLACEHOLDERS
    assert set(recipe_env.PLACEHOLDERS) == set(recipe_env.ADDRESS_PLACEHOLDERS) | {"port"}


def test_a_duo_placeholder_without_a_worker_is_a_readable_error():
    with pytest.raises(ValueError, match="worker_ssh"):
        launch_template.build_install_command(slug="duo-engine", install_template=DUO_INSTALL)


def test_launch_templates_still_do_not_know_the_duo_placeholders():
    """ADR-077 rule 5: the start talks to the head and the recipe's .env says
    who the worker is — the launch command itself stays box-agnostic."""
    with pytest.raises(ValueError, match="Unbekannte Platzhalter"):
        launch_template.build_launch_command(
            engine="ssh_process", model_identifier="m", slug="s", port=8000,
            launch_template="./start.sh --worker {worker_ssh}",
        )


def test_env_map_knows_the_port():
    head = Host(slug="a", display_name="A", kind="ssh", ssh_host="192.0.2.10")
    out = recipe_env.render_env_map({"PORT": "{port}", "URL": "http://{head_ip}:{port}"}, head, None, port=8000)
    assert out == {"PORT": "8000", "URL": "http://192.0.2.10:8000"}
    with pytest.raises(recipe_env.EnvRenderError, match="port"):
        recipe_env.render_env_map({"PORT": "{port}"}, head, None)


def test_upsert_can_create_the_recipe_folder_when_asked(tmp_path):
    """An install writes the .env BEFORE the clone exists. Only that caller
    may create the folder; the start keeps refusing a missing one."""
    target = tmp_path / "not-yet-cloned" / ".env"
    command = recipe_env._upsert_command(str(target), {"K": "v"}, create_dir=True)
    subprocess.run(["/bin/sh", "-c", command], check=True)
    assert recipe_env.parse_env_text(target.read_text()) == {"K": "v"}

    refused = subprocess.run(
        ["/bin/sh", "-c", recipe_env._upsert_command(str(tmp_path / "other" / ".env"), {"K": "v"})],
        capture_output=True,
    )
    assert refused.returncode != 0


# ── 2. the install endpoint for a duo recipe ─────────────────────────────────


@pytest.mark.asyncio
async def test_duo_install_writes_the_env_before_the_job_and_renders_the_worker(auth_client, session):
    await _host(session, "box-a", ssh_user="mc", fabric_ip="10.0.0.1")
    await _host(session, "box-c", ssh_host="192.0.2.12", ssh_user="mc")
    await _host(session, "box-b", ssh_host="192.0.2.11", ssh_user="mc", fabric_ip="10.0.0.2", role="worker")
    await _duo_recipe(session)

    resp, calls, box = await _install(auth_client, "duo-engine", {"host_id": "box-a"})

    assert resp.status_code == 202, resp.text
    assert resp.json()["worker_slug"] == "box-b"          # role=worker first
    assert len(calls) == 1
    env = recipe_env.parse_env_text(calls[0]["env_at_start"])
    assert env == {"WORKER": "mc@192.0.2.11", "FABRIC_PEER": "10.0.0.2", "PORT": "8000"}
    assert "--worker mc@192.0.2.11" in calls[0]["command"]
    # The worker is handed to the job too (disk check on both boxes).
    assert calls[0]["worker"].ssh_host == "192.0.2.11"
    # Created the folder: the clone does not exist yet at this point.
    assert any('mkdir -p "$d"' in c for c in box.commands)


@pytest.mark.asyncio
async def test_duo_install_honours_a_named_worker(auth_client, session):
    await _host(session, "box-a")
    await _host(session, "box-b", ssh_host="192.0.2.11", role="worker")
    box_c = await _host(session, "box-c", ssh_host="192.0.2.12")
    await _duo_recipe(session)

    resp, calls, _ = await _install(
        auth_client, "duo-engine", {"host_id": "box-a", "worker_host_id": str(box_c.id)}
    )
    assert resp.status_code == 202, resp.text
    assert recipe_env.parse_env_text(calls[0]["env_at_start"])["FABRIC_PEER"] == "192.0.2.12"


@pytest.mark.asyncio
async def test_duo_install_refuses_the_head_or_an_unknown_box_as_worker(auth_client, session):
    box_a = await _host(session, "box-a")
    await _host(session, "box-b", ssh_host="192.0.2.11")
    await _duo_recipe(session)

    for worker in (str(box_a.id), "ghost"):
        resp, calls, box = await _install(
            auth_client, "duo-engine", {"host_id": "box-a", "worker_host_id": worker}
        )
        assert resp.status_code == 409, resp.text
        assert calls == [] and box.commands == []


@pytest.mark.asyncio
async def test_duo_install_without_a_second_box_is_409_and_touches_nothing(auth_client, session):
    await _host(session, "box-a")
    await _duo_recipe(session)
    resp, calls, box = await _install(auth_client, "duo-engine", {"host_id": "box-a"})
    assert resp.status_code == 409
    assert calls == [] and box.commands == []


@pytest.mark.asyncio
async def test_duo_install_without_env_map_is_422_and_touches_nothing(auth_client, session):
    await _host(session, "box-a")
    await _host(session, "box-b", ssh_host="192.0.2.11")
    await _duo_recipe(session, env_map=None)
    resp, calls, box = await _install(auth_client, "duo-engine", {"host_id": "box-a"})
    assert resp.status_code == 422
    assert calls == [] and box.commands == []


@pytest.mark.asyncio
async def test_duo_install_refuses_another_src_dir(auth_client, session):
    """env_file is a fixed path in the catalog — an install into another
    folder would write the .env where the clone is not."""
    await _host(session, "box-a")
    await _host(session, "box-b", ssh_host="192.0.2.11")
    await _duo_recipe(session)
    resp, calls, box = await _install(
        auth_client, "duo-engine", {"host_id": "box-a", "src_dir": "/opt/elsewhere"}
    )
    assert resp.status_code == 422
    assert calls == [] and box.commands == []


@pytest.mark.asyncio
async def test_a_env_that_reads_back_wrong_stops_the_install(auth_client, session):
    """Wirk-Beweis: no install with a .env that does not say what MC wrote."""
    await _host(session, "box-a")
    await _host(session, "box-b", ssh_host="192.0.2.11")
    await _duo_recipe(session)
    box = _Box()
    box.lie = True
    resp, calls, _ = await _install(auth_client, "duo-engine", {"host_id": "box-a"}, box)
    assert resp.status_code == 502
    assert calls == []


@pytest.mark.asyncio
async def test_a_running_duo_install_is_409_before_the_env_is_touched(auth_client, session):
    box_a = await _host(session, "box-a")
    await _host(session, "box-b", ssh_host="192.0.2.11")
    await _duo_recipe(session)
    await recipe_install.job_for(str(box_a.id), "duo-engine").set_status(
        recipe_install.STATUS_RUNNING, phase="install"
    )
    resp, calls, box = await _install(auth_client, "duo-engine", {"host_id": "box-a"})
    assert resp.status_code == 409
    assert calls == [] and box.commands == []


@pytest.mark.asyncio
async def test_a_single_box_install_is_unchanged(auth_client, session):
    """No .env, no worker, the same command as before this change."""
    await local_registry.seed_local_recipes(session)
    await _host(session, "box-a")
    await _host(session, "box-b", ssh_host="192.0.2.11", role="worker")
    recipe = (
        await session.exec(select(LocalRecipe).where(LocalRecipe.slug == "deepseek-v4-flash-ds4"))
    ).one()
    expected = launch_template.build_install_command(
        slug=recipe.slug, install_template=recipe.install_template, port=8888,
        model_identifier=recipe.model_identifier, ctx=262144, env=recipe.env,
    )

    resp, calls, box = await _install(
        auth_client, "deepseek-v4-flash-ds4", {"host_id": "box-a", "port": 8888, "ctx": 262144}
    )
    assert resp.status_code == 202, resp.text
    assert box.commands == []                     # nothing written before the job
    assert calls[0]["command"] == expected
    assert calls[0].get("worker") is None
    assert resp.json().get("worker_host_id") is None


# ── 3. the install job checks the disk on both boxes ─────────────────────────


@pytest.mark.asyncio
async def test_the_install_job_checks_the_disk_on_the_worker_too():
    head = ResolvedHost(ssh_host="192.0.2.10", ssh_user="mc", kind="ssh", source="registry")
    worker = ResolvedHost(ssh_host="192.0.2.11", ssh_user="mc", kind="ssh", source="registry")
    seen: list[str | None] = []

    async def handler(cmd, *, host=None, **kw):
        seen.append(host.ssh_host if host else None)
        return ("Filesystem 1024-blocks Used Available Capacity\n/dev/x 1 1 1048576 1%", "", 0)

    run = recipe_install._Run("h", "duo", head, command="true", est_weights_gb=100.0, worker=worker)
    with patch("app.services.runtime_manager._ssh_run", AsyncMock(side_effect=handler)):
        await run.check_disk()
    assert seen == ["192.0.2.10", "192.0.2.11"]
    lines = (await recipe_install.read_log("h", "duo"))["lines"]
    assert sum("WARNUNG" in line["text"] for line in lines) == 2


# ── 4. drop_page_cache: catalog → instance ───────────────────────────────────


def test_migration_adds_drop_page_cache_to_recipes_and_runtimes():
    source = (MIGRATIONS / "0210_recipe_drop_page_cache.py").read_text(encoding="utf-8")
    for table in ("local_recipes", "runtimes"):
        assert f'op.add_column(\n        "{table}",' in source or f'op.add_column("{table}"' in source
        assert f'op.drop_column("{table}", "drop_page_cache")' in source
    assert 'server_default=sa.text("true")' in source
    assert 'down_revision = "0209_drop_board_groups"' in source
    for verb in ("INSERT", "UPDATE", "op.execute", "op.bulk_insert"):
        assert verb not in source, verb


def test_recipe_spec_carries_drop_page_cache_and_defaults_to_true():
    spec = local_registry.RecipeSpec(slug="x", display_name="X", engine="ssh_process", model_identifier="m")
    assert spec.drop_page_cache is True
    row = local_registry._row_from_spec(spec.model_copy(update={"drop_page_cache": False}))
    assert row.drop_page_cache is False
    assert local_registry._apply_update(row, spec) is True
    assert row.drop_page_cache is True


@pytest.mark.asyncio
async def test_a_recipe_instance_inherits_drop_page_cache(session):
    host = await _host(session, "box-a")
    recipe = await _duo_recipe(session, drop_page_cache=False)
    resolved = ResolvedHost(ssh_host="192.0.2.10", kind="ssh", source="registry")
    with patch("app.services.runtime_manager._host_ip", lambda h: "192.0.2.10"):
        runtime = await recipe_switcher.build_runtime_from_recipe(session, recipe, host, resolved)
    assert runtime.drop_page_cache is False
    assert runtime.model_dump()["drop_page_cache"] is False

    other = await _duo_recipe(session, slug="duo-two")
    with patch("app.services.runtime_manager._host_ip", lambda h: "192.0.2.10"):
        runtime = await recipe_switcher.build_runtime_from_recipe(session, other, host, resolved)
    assert runtime.drop_page_cache is True


# ── 5. the seeded TensorFold recipe ──────────────────────────────────────────


def _tf_spec() -> dict:
    entries = json.loads(SEED.read_text(encoding="utf-8"))
    return next(e for e in entries if e["slug"] == TF_SLUG)


def test_tf_recipe_is_a_generic_duo_entry():
    tf = _tf_spec()
    assert tf["engine"] == "ssh_process"
    assert tf["model_identifier"] == "GLM-5.3-Flash-EXL3"   # same served name as the vLLM recipe
    assert tf["port"] == 8000
    assert tf["topology"] == {"nodes": 2, "tp": 2}
    assert tf["exclusive"] is True
    assert tf["context_len"] == 1048576
    assert tf["process_name"] == "glm53-flash-tf"            # = start.sh's CONTAINER_NAME
    assert tf["drop_page_cache"] is False
    assert tf["recipe_ref"].startswith("https://github.com/MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold")
    assert tf["env_map"]["WORKER"] == "{worker_ssh}"
    assert tf["env_map"]["FABRIC_PEER"] == "{worker_fabric_ip}"
    assert tf["env_map"]["PORT"] == "{port}"
    assert tf["env_map"]["PREPARE"] == "0"                    # the install prepares, a start only starts
    assert tf["env_map"]["DRAFTER"] in ("dflash2", "mtp")
    assert tf["env_file"].endswith("/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold/.env")
    assert tf["env_file"].startswith(launch_template.DEFAULT_SRC_DIR + "/")
    # Operator decision (option A): dflash2 stays the default, and the user
    # reads "non-commercial" BEFORE installing — first words of the notes
    # (the install dialog shows them above its buttons) and of the description.
    assert tf["env_map"]["DRAFTER"] == "dflash2"
    assert tf["notes"].startswith("LICENCE - NON-COMMERCIAL USE ONLY (CC BY-NC-ND drafter)")
    assert tf["description"].startswith("NON-COMMERCIAL USE ONLY")
    assert "DRAFTER=mtp" in tf["notes"] and "ONE request at a time" in tf["notes"]
    # Pinned, never a moving branch.
    assert "1f3d909b00b7be7aa8f00d3a33e0b9e7aa56d221" in tf["install_template"]
    # No operator data anywhere in the entry.
    blob = json.dumps(tf)
    assert not re.search(r"\b(?:192\.168|100\.\d+)\.\d+\.\d+\b", blob)
    assert "/Users/" not in blob and "/home/" not in blob


@pytest.mark.asyncio
async def test_tf_recipe_seeds_and_is_env_ready(session):
    await local_registry.seed_local_recipes(session)
    row = (await session.exec(select(LocalRecipe).where(LocalRecipe.slug == TF_SLUG))).one()
    assert recipe_switcher.recipe_env_ready(row)
    assert recipe_switcher.recipe_nodes(row.topology) == 2
    assert row.drop_page_cache is False


def _rendered_tf_commands() -> dict[str, str]:
    tf = _tf_spec()
    duo = {"worker_ssh": "mc@192.0.2.11", "worker_fabric_ip": "10.0.0.2", "head_ip": "192.0.2.10",
           "worker_ip": "192.0.2.11", "head_fabric_ip": "10.0.0.1", "head_ssh": "mc@192.0.2.10"}
    install = launch_template.build_install_command(
        slug=TF_SLUG, install_template=tf["install_template"], port=8000,
        model_identifier=tf["model_identifier"], ctx=tf["context_len"], duo=duo,
    )
    launch = launch_template.build_launch_command(
        engine="ssh_process", model_identifier=tf["model_identifier"], slug="tf-box-a",
        port=8000, launch_template=tf["launch_template"], ctx=tf["context_len"],
    )
    stop = launch_template.render_launch_template(
        tf["stop_template"], {"src_dir": launch_template.DEFAULT_SRC_DIR, "port": 8000},
    )
    return {"install": install, "launch": launch, "stop": stop}


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_tf_commands_are_valid_bash_once_rendered():
    for name, command in _rendered_tf_commands().items():
        # Exactly the wrapper recipe_install puts around it.
        wrapped = f"{command}; echo \"MC_EXIT:$?\"" if name == "install" else command
        done = subprocess.run(["bash", "-n", "-c", wrapped], capture_output=True, text=True)
        assert done.returncode == 0, f"{name}: {done.stderr}"


def test_tf_install_keeps_the_exit_marker_reachable():
    """recipe_install appends `; echo MC_EXIT:$?`. A `set -e` or `exit` at the
    top level would end the wrapper shell before the marker — the job would
    read as 'lost' instead of 'failed'. Everything strict runs in a subshell."""
    install = _rendered_tf_commands()["install"]
    assert install.lstrip().startswith("(")
    assert install.rstrip().endswith(")")


def test_tf_launch_is_detached_and_stop_stops_both_boxes():
    cmds = _rendered_tf_commands()
    assert "setsid" in cmds["launch"] and "./start.sh" in cmds["launch"]
    assert "< /dev/null" in cmds["launch"]
    assert cmds["stop"].endswith("./stop.sh")


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("upstream_changed", [False, True])
def test_tf_install_skips_only_prepare_sh_image_steps(tmp_path, upstream_changed):
    """The splice the install runs when the two image stores report different
    image IDs: steps 2 and 3 of prepare.sh go, everything else stays — and the
    guard after it refuses to run anything when upstream renamed a section
    marker (then the image lines would survive the splice). Executed for real against a
    prepare.sh with upstream's section markers, including the header comment
    that mentions `docker save` (a first guard tripped over exactly that)."""
    install = _rendered_tf_commands()["install"]
    lines = install.splitlines()
    first = next(i for i, line in enumerate(lines) if "P=scripts/.mc-prepare-weights.sh" in line)
    last = next(i for i, line in enumerate(lines) if line.strip() == 'bash "$P"')
    splice = "\n".join(lines[first:last])
    second = (
        "# ---------------------------------------------------------------- 2. image (head)"
        if not upstream_changed
        else "# ---------------------------------------------------------------- 2. the image here"
    )
    fake = "\n".join([
        "#!/usr/bin/env bash",
        "#   3. the same image on the worker: pulled there, else streamed (docker save | ssh docker load)",
        "# ---------------------------------------------------------------- 1. preflight",
        "echo preflight",
        second,
        'docker pull "$prebuilt"',
        "# ---------------------------------------------------------------- 3. image (worker)",
        'docker save "$IMAGE" | worker docker load >/dev/null',
        "# ---------------------------------------------------------------- 4. download (head)",
        "echo download",
        "# ---------------------------------------------------------------- 6. the same files on the worker",
        "echo rsync",
        "",
    ])
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "prepare.sh").write_text(fake)
    script = 'die() { echo "DIE: $*"; exit 7; }\n' + splice + '\necho SPLICED-OK\n'
    done = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True)
    kept = (tmp_path / "scripts" / ".mc-prepare-weights.sh").read_text()
    if upstream_changed:
        assert done.returncode == 7, done.stdout + done.stderr
        assert "prepare.sh changed shape" in done.stdout
        return
    assert done.returncode == 0, done.stdout + done.stderr
    assert "SPLICED-OK" in done.stdout
    assert "echo preflight" in kept and "echo download" in kept and "echo rsync" in kept
    assert 'docker pull "$prebuilt"' not in kept and 'docker save "$IMAGE"' not in kept


@pytest.mark.asyncio
async def test_a_duo_start_writes_the_port_into_the_env(auth_client, session):
    """{port} reaches the .env on START too — the recipe's own default port
    (8888 upstream) would otherwise win over the port MC probes."""
    box_a = await _host(session, "box-a")
    await _host(session, "box-b", ssh_host="192.0.2.11", role="worker")
    await _duo_recipe(session)
    box = _Box()
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})

    async def _not_running(runtime, **kw):
        return False

    with (
        patch("app.services.recipe_switcher.probe_running", _not_running),
        patch("app.services.runtime_manager._ssh_run", box),
        patch("app.services.runtime_manager.start_runtime", start),
    ):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/duo-engine/start")
    assert resp.status_code == 200, resp.text
    assert recipe_env.parse_env_text(box.content)["PORT"] == "8000"
    # …and the instance it started carries the recipe's drop_page_cache.
    started = start.await_args.args[0]
    assert started["drop_page_cache"] is True
