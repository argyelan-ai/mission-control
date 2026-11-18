"""The DeepSeek-V4.1-Flash-1017 TensorFold recipe for two DGX Sparks (MiaAI-Lab).

Second two-box entry in the live catalog, modelled on the GLM-5.3 Flash
TensorFold entry (tests/test_recipe_duo_install.py). What differs, and is
pinned here:

  * no published image for the pinned commit: upstream's scripts/config.sh
    leaves IMAGE_TAG / IMAGE_DIGEST empty, so the install must let prepare.sh
    BUILD the image (and copy it to the worker) instead of dying for want of a
    digest - and it must never pull a tag that only names a moving ref
    (PULL=0 unless a digest is pinned);
  * after prepare.sh, MC checks itself that head and worker hold the same image
    content (upstream's image_ident) and that prepare.sh left its marker;
  * the worker reads the head's Hugging Face cache over NFS. The export on the
    head needs root (README, NFS) - MC cannot set it up, so the install checks
    that the head serves NFS at all before hours of downloads;
  * the ABLIT=1 overlay is gated: ABLIT=0 is the catalog default.

Only RFC 5737 / RFC 1918 placeholder addresses, no device names - public repo.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlmodel import select

from app.models.local_recipe import LocalRecipe
from app.services import launch_template, local_registry, recipe_env, recipe_switcher

SLUG = "deepseek-v41-flash-1017-exl3-tensorfold"
REPO = "DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks"
REV = "e65785cd3cde8e23c38d60a732a014abb6ccb533"
SEED = Path(__file__).resolve().parents[1] / "config" / "local-recipes.json"
DUO = {"worker_ssh": "mc@192.0.2.11", "worker_fabric_ip": "10.0.0.2", "head_ip": "192.0.2.10",
       "worker_ip": "192.0.2.11", "head_fabric_ip": "10.0.0.1", "head_ssh": "mc@192.0.2.10"}
needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _spec() -> dict:
    entries = json.loads(SEED.read_text(encoding="utf-8"))
    found = [e for e in entries if e["slug"] == SLUG]
    assert found, f"{SLUG} is not in the live catalog"
    return found[0]


# ── 1. the entry ─────────────────────────────────────────────────────────────


def test_dsv41_recipe_is_a_generic_duo_entry():
    r = _spec()
    assert r["engine"] == "ssh_process"
    assert r["model_identifier"] == "DeepSeek-V4.1-Flash-1017"      # = config.sh SERVED_NAME
    assert r["port"] == 8000
    assert r["topology"] == {"nodes": 2, "tp": 2}
    assert r["exclusive"] is True
    assert r["drop_page_cache"] is True
    assert r["supports_vision"] is True                             # images always on (rank 0 runs the vision tower)
    assert r["context_len"] == 1048576
    assert r["process_name"] == "dsv41-1017-exl3-tf"                # = config.sh CONTAINER_NAME
    assert r["min_vram_gb"] >= 112                                  # = config.sh MIN_AVAIL_GIB
    assert r["author"] and r["author_url"] == f"https://github.com/MiaAI-Lab/{REPO}"
    assert r["recipe_ref"] == r["source_url"] == f"https://github.com/MiaAI-Lab/{REPO}"
    for tag in ("reasoning", "vision", "long-context", "duo", "ssh-process", "community", "abliteration-optional"):
        assert tag in r["tags"], tag
    assert set(r["summary"]) == {"en", "de"}


def test_dsv41_env_map_uses_only_known_placeholders_and_defaults_to_stock_weights():
    r = _spec()
    env = r["env_map"]
    assert env == {"WORKER": "{worker_ssh}", "FABRIC_PEER": "{worker_fabric_ip}", "PORT": "{port}",
                   "PREPARE": "0", "ABLIT": "0"}
    # NFS_SERVER stays unset on purpose: nodes.sh then mounts from the head's
    # address on the link it found ({head_fabric_ip} would fall back to the
    # head's SSH address when no fabric address is set - the wrong network).
    assert "NFS_SERVER" not in env
    for value in env.values():
        for name in re.findall(r"\{([^{}]*)\}", value):
            assert name in recipe_env.PLACEHOLDERS
    assert r["env_file"] == f"{launch_template.DEFAULT_SRC_DIR}/{REPO}/.env"


def test_dsv41_notes_carry_the_licences_and_the_gated_abliteration_option():
    r = _spec()
    notes, desc = r["notes"], r["description"]
    assert notes.startswith("LICENCE")
    for fact in ("Apache-2.0", "MIT", "DeepSeek", "Mia-AiLab/DeepSeek-V4.1-Flash-1017-EXL3-EAQ-v1-2.9-bpw"):
        assert fact in notes, fact
    assert "drowzeys/keys-DeepSeek-V4.1-Flash-1017-Abliterated" in notes
    assert "ABLIT=1" in notes and "gated" in notes and "HF_TOKEN" in notes
    assert "ABLIT=1" in desc
    assert "NFS" in notes and "exportfs" in notes
    assert "not yet measured" in notes


def test_dsv41_is_pinned_and_public_safe():
    r = _spec()
    assert f"REV={REV}\n" in r["install_template"]
    blob = json.dumps(r)
    assert not re.search(r"\b(?:192\.168|100\.\d+)\.\d+\.\d+\b", blob)
    assert "/Users/" not in blob and "/home/" not in blob


@pytest.mark.asyncio
async def test_dsv41_recipe_seeds_and_is_env_ready(session):
    await local_registry.seed_local_recipes(session)
    row = (await session.exec(select(LocalRecipe).where(LocalRecipe.slug == SLUG))).one()
    assert recipe_switcher.recipe_env_ready(row)
    assert recipe_switcher.recipe_nodes(row.topology) == 2
    assert row.supports_vision is True
    assert row.drop_page_cache is True


# ── 2. the commands, rendered ────────────────────────────────────────────────


def _rendered() -> dict[str, str]:
    r = _spec()
    install = launch_template.build_install_command(
        slug=SLUG, install_template=r["install_template"], port=8000,
        model_identifier=r["model_identifier"], ctx=r["context_len"], duo=DUO,
    )
    launch = launch_template.build_launch_command(
        engine="ssh_process", model_identifier=r["model_identifier"], slug="dsv41-box-a",
        port=8000, launch_template=r["launch_template"], ctx=r["context_len"],
    )
    stop = launch_template.render_launch_template(
        r["stop_template"], {"src_dir": launch_template.DEFAULT_SRC_DIR, "port": 8000},
    )
    return {"install": install, "launch": launch, "stop": stop}


@needs_bash
def test_dsv41_commands_are_valid_bash_once_rendered():
    for name, command in _rendered().items():
        wrapped = f'{command}; echo "MC_EXIT:$?"' if name == "install" else command
        done = subprocess.run(["bash", "-n", "-c", wrapped], capture_output=True, text=True)
        assert done.returncode == 0, f"{name}: {done.stderr}"


def test_dsv41_install_keeps_the_exit_marker_reachable_and_launch_is_detached():
    cmds = _rendered()
    assert cmds["install"].lstrip().startswith("(") and cmds["install"].rstrip().endswith(")")
    assert "setsid" in cmds["launch"] and "./start.sh" in cmds["launch"] and "< /dev/null" in cmds["launch"]
    assert REPO in cmds["launch"]
    assert cmds["stop"].endswith("./stop.sh")
    assert cmds["install"].count("./scripts/prepare.sh") == 1


# ── 3. the install, run against a stand-in checkout ──────────────────────────
# config.sh / nodes.sh with the helper names upstream has at the pin
# (scripts/config.sh: prebuilt_image, prepared_state; scripts/nodes.sh: worker,
# need_worker, image_ident, worker_image_ident), and a docker / ss / flock that
# answer per side.

_FAKE_CONFIG = """\
WORKER="${WORKER:-mc@192.0.2.11}"
TP=2
[ -z "${FAKE_TP:-}" ] || TP=$FAKE_TP
IMAGE=tensorfold-dsv41:exl3-tp2-abc
BASE_IMAGE=nvcr.example/pytorch:base
MODEL_ID=example/DSV41-Model
ENGRAM_ID=example/DSV41-Base
ABLIT="${ABLIT:-0}"
ABLIT_ID=example/DSV41-Overlay
NFS_SERVER="${NFS_SERVER:-}"
NFS_PATH="${NFS_PATH:-$STUB/head-hf}"
HF_CACHE="${HF_CACHE:-$STUB/head-hf}"
STATE_DIR="$STUB/state"
PREPARED_MARKER="$STATE_DIR/prepared"
die() { echo "DIE: $*"; exit 7; }
prebuilt_image() { if [ -n "${FAKE_DIGEST:-}" ]; then echo "ghcr.io/example/dsv41@sha256:abc"; else echo "ghcr.io/example/dsv41:exl3-tp2-abc"; fi; }
prepared_state() { echo "image=$(image_ident "$IMAGE") w=$WORKER ablit=$ABLIT"; }
"""
_FAKE_NODES = """\
worker() {
  local i=$1; shift; [[ "$i" =~ ^[0-9]+$ ]] || { echo "BAD-WORKER-INDEX $i"; exit 9; }
  if [ $# = 1 ]; then SIDE=worker bash -c "$1"; else SIDE=worker "$@"; fi
}
need_worker() { local i=$1; [ "$i" = 1 ] || die "need_worker $i"; }
image_ident() { [ -f "$STUB/image-head" ] && echo "ident-$(cat "$STUB/image-head")" || echo missing; }
worker_image_ident() { [ -f "$STUB/image-worker" ] && echo "ident-$(cat "$STUB/image-worker")" || echo missing; }
"""
# prepare.sh stand-in: records PULL, "builds" the image on the head, "copies"
# it to the worker (unless told not to) and writes its marker - as the real one
# does at its end (prepare.sh: prepared_state > "$PREPARED_MARKER").
_FAKE_PREPARE = """\
#!/usr/bin/env bash
set -euo pipefail
source ./scripts/config.sh
source ./scripts/nodes.sh
echo "PREPARE-RAN PULL=${PULL:-unset} ABLIT=$ABLIT"
[ -f "$STUB/image-head" ] || echo built > "$STUB/image-head"
[ -f "$STUB/worker-keeps-other-image" ] || cp "$STUB/image-head" "$STUB/image-worker"
mkdir -p "$STATE_DIR"
[ -f "$STUB/no-marker" ] || prepared_state > "$PREPARED_MARKER"
"""
_FAKE_DOCKER = """\
#!/usr/bin/env bash
echo "${SIDE:-head} docker $*" >> "$STUB/calls"
case "$*" in
  *"--entrypoint chown"*) [ -f "$STUB/chown-fails" ] || touch "$STUB/chowned-${SIDE:-head}" ;;
  "image inspect"*) case "$*" in *tensorfold-dsv41*) [ -f "$STUB/image-head" ] || exit 1 ;; esac ;;
esac
exit 0
"""
_FAKE_SS = """\
#!/usr/bin/env bash
echo "State Recv-Q Send-Q Local Address:Port Peer Address:Port"
[ -f "$STUB/no-nfs-server" ] || echo "LISTEN 0 64 0.0.0.0:2049 0.0.0.0:*"
echo "LISTEN 0 4096 0.0.0.0:8000 0.0.0.0:*"
"""
_FAKE_ID = """\
#!/usr/bin/env bash
side=${SIDE:-head}
if [ "$1" = -u ] && [ -f "$STUB/foreign-$side" ] && [ ! -f "$STUB/chowned-$side" ]; then echo 4242; exit 0; fi
PATH=${PATH#*:} exec id "$@"
"""
_FAKE_FLOCK = """\
#!/usr/bin/env bash
[ -f "$STUB/prepare-running" ] && exit 1
exit 0
"""


def _run_install(tmp_path: Path, *, env: dict[str, str] | None = None, flags: tuple[str, ...] = (),
                 head_cache: tuple[str, ...] = (), template: str | None = None) -> tuple[str, str]:
    src = tmp_path / "engines"
    repo = src / REPO
    (repo / "scripts").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "scripts" / "config.sh").write_text(_FAKE_CONFIG)
    (repo / "scripts" / "nodes.sh").write_text(_FAKE_NODES)
    (repo / "scripts" / "prepare.sh").write_text(_FAKE_PREPARE)
    (repo / "scripts" / "prepare.sh").chmod(0o755)
    stub = tmp_path / "stub"
    stub.mkdir()
    for tool, body in (("docker", _FAKE_DOCKER), ("git", "#!/usr/bin/env bash\nexit 0\n"), ("ss", _FAKE_SS),
                       ("id", _FAKE_ID), ("flock", _FAKE_FLOCK)):
        (stub / tool).write_text(body)
        (stub / tool).chmod(0o755)
    for flag in flags:
        (stub / flag).write_text("")
    for name in head_cache:
        target = stub / "head-hf" / "hub" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
    r = _spec()
    install = launch_template.build_install_command(
        slug=SLUG, install_template=template or r["install_template"], port=8000,
        model_identifier=r["model_identifier"], ctx=r["context_len"], src_dir=str(src), duo=DUO,
    )
    run_env = {k: v for k, v in os.environ.items() if k not in ("WORKER", "TP", "PULL", "ABLIT", "NFS_SERVER")}
    run_env.update(PATH=f"{stub}:{os.environ['PATH']}", STUB=str(stub), **(env or {}))
    done = subprocess.run(["bash", "-c", f'{install}; echo "MC_EXIT:$?"'], cwd=tmp_path,
                          capture_output=True, text=True, env=run_env)
    calls = (stub / "calls").read_text() if (stub / "calls").exists() else ""
    return done.stdout + done.stderr, calls


@needs_bash
def test_without_a_pinned_digest_the_install_builds_and_never_pulls_a_tag(tmp_path):
    """config.sh at the pin has no IMAGE_DIGEST: prepare.sh must build the
    image from the pinned sources (PULL=0), not die and not pull a tag."""
    out, calls = _run_install(tmp_path)
    assert "MC_EXIT:0" in out, out
    assert "PREPARE-RAN PULL=0" in out
    assert "docker pull" not in calls
    assert "builds it locally" in out
    assert "same image content on both Sparks" in out


@needs_bash
def test_with_a_pinned_digest_prepare_sh_may_pull_it(tmp_path):
    out, _ = _run_install(tmp_path, env={"FAKE_DIGEST": "1"})
    assert "MC_EXIT:0" in out, out
    assert "PREPARE-RAN PULL=1" in out


@needs_bash
def test_the_install_refuses_a_worker_with_other_image_content(tmp_path):
    out, _ = _run_install(tmp_path, flags=("worker-keeps-other-image",))
    assert "MC_EXIT:7" in out, out
    assert "PREPARE-RAN" in out
    assert "different" in out


@needs_bash
def test_the_install_refuses_when_prepare_sh_left_no_marker(tmp_path):
    out, _ = _run_install(tmp_path, flags=("no-marker",))
    assert "MC_EXIT:7" in out, out


@needs_bash
def test_the_install_refuses_a_worker_mc_did_not_choose(tmp_path):
    out, _ = _run_install(tmp_path, env={"WORKER": "mc@192.0.2.99"})
    assert "MC_EXIT:7" in out, out
    assert "PREPARE-RAN" not in out


@needs_bash
def test_the_install_refuses_a_third_spark(tmp_path):
    out, _ = _run_install(tmp_path, env={"FAKE_TP": "3"})
    assert "MC_EXIT:7" in out, out
    assert "PREPARE-RAN" not in out


@needs_bash
def test_the_install_fails_loudly_without_upstreams_image_ident(tmp_path):
    template = _spec()["install_template"].replace("source scripts/nodes.sh",
                                                   "source scripts/nodes.sh\nunset -f image_ident")
    out, _ = _run_install(tmp_path, template=template)
    assert "MC_EXIT:7" in out, out
    assert "PREPARE-RAN" not in out


@needs_bash
def test_the_install_stops_before_any_download_when_the_head_serves_no_nfs(tmp_path):
    out, _ = _run_install(tmp_path, flags=("no-nfs-server",))
    assert "MC_EXIT:7" in out, out
    assert "PREPARE-RAN" not in out
    assert "exportfs" in out


@needs_bash
def test_an_nfs_server_elsewhere_is_not_checked_on_the_head(tmp_path):
    out, _ = _run_install(tmp_path, flags=("no-nfs-server",), env={"NFS_SERVER": "10.0.0.9"})
    assert "MC_EXIT:0" in out, out


@needs_bash
def test_the_install_takes_back_root_owned_cache_files_before_prepare(tmp_path):
    cache = (".locks/models--example--DSV41-Model/abc.lock", "models--example--DSV41-Model/blobs/abc",
             "models--example--DSV41-Base/blobs/def", ".locks/models--other--Model/zzz.lock")
    out, calls = _run_install(tmp_path, flags=("foreign-head",), head_cache=cache)
    assert "MC_EXIT:0" in out, out
    hub = tmp_path / "stub/head-hf/hub"
    assert "belong to another user" in out
    assert not (hub / ".locks/models--example--DSV41-Model/abc.lock").exists()
    assert (hub / ".locks/models--other--Model/zzz.lock").exists()
    chown = [c for c in calls.splitlines() if "--entrypoint chown" in c]
    assert chown and all(c.startswith("head docker run --rm --network none --user 0:0") for c in chown), calls
    # no engine image yet (it is built by prepare.sh): the base image does the chown
    assert "nvcr.example/pytorch:base" in chown[-1]
    assert "models--other--Model" not in "".join(chown)
    assert out.index("belong to another user") < out.index("PREPARE-RAN")


@needs_bash
def test_the_install_never_touches_locks_while_prepare_sh_runs(tmp_path):
    cache = (".locks/models--example--DSV41-Model/abc.lock",)
    out, calls = _run_install(tmp_path, flags=("foreign-head", "prepare-running"), head_cache=cache)
    assert "MC_EXIT:7" in out, out
    assert "PREPARE-RAN" not in out
    assert "prepare.sh is still running" in out
    assert (tmp_path / "stub/head-hf/hub/.locks/models--example--DSV41-Model/abc.lock").exists()
    assert "--entrypoint chown" not in calls
