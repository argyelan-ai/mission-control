"""The test suite never writes into the real home or the real checkout.

Incident 2026-10-20: every local `pytest` wrote into the operator's machine —
new folders ~/.mc/agents/cardhostagent-host + cardhostagentoff-host, rewritten
skills/, settings.json and plugin files in existing agent folders, and a
rewrite of the real checkout's docker/docker-compose.agents.yml, the file the
live backend renders its fleet from. Cause: settings.home_host, HOME_HOST and
settings.mc_repo_path default to the real machine, and several modules
resolve them once at import.

tests/conftest.py now points all of them at a stand-in host home (section
"Host paths") and fails the run when the real places change anyway (the
guard at its end). These tests check both halves:

- the oracle for "real" here is independent of conftest's own helper: the
  real HOME, the real HOME_HOST and the real checkout path, as they were
  before conftest changed the env (kept in MC_TEST_ORIGINAL_*);
- every check that could write runs only after a precondition proved the
  target is NOT real — so even with the isolation broken these tests fail
  without touching the real machine.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

import app.config
from tests.conftest import test_engine


# ── Oracle: what is the real machine? ────────────────────────────────────

def _real_roots() -> tuple[Path, ...]:
    home = Path.home()
    host = Path(
        os.environ.get("MC_TEST_ORIGINAL_HOME_HOST", os.environ.get("HOME_HOST", ""))
        or home
    )
    repo = Path(
        os.environ.get("MC_TEST_ORIGINAL_MC_REPO_PATH", "")
        or host / "Workspace" / "Projects" / "mission-control"
    )
    return (home, host, repo)


def _stand_in() -> str:
    """The stand-in host home, when there is one (HOME_HOST not real)."""
    value = os.environ.get("HOME_HOST", "")
    if not value or any(value == str(root) for root in _real_roots()):
        return ""
    return value


def _real_hits(value) -> list[str]:
    """The real roots a value mentions (after removing the stand-in), plus
    '~' when it holds a ~-path (expands to the real HOME)."""
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return [hit for item in value for hit in _real_hits(item)]
    if not isinstance(value, (str, os.PathLike)):
        return []
    text = os.fspath(value)
    stand_in = _stand_in()
    if stand_in:
        text = text.replace(stand_in, "<stand-in>")
    hits = [
        str(root) for root in _real_roots()
        if text == str(root) or f"{root}{os.sep}" in text
    ]
    if text.startswith("~") or " ~" in text or ":~" in text or ";~" in text:
        hits.append("~")
    return hits


def _assert_not_real(label: str, value) -> None:
    hits = _real_hits(value)
    assert not hits, f"{label} = {value!r} points at the real machine ({', '.join(hits)})"


# Paths of the container, not of the host this suite runs on. Inside the
# backend image HOME is /home/mcuser, so they would match there — and there
# they are the real mounts the backend works with, not test territory.
_CONTAINER_ONLY_FIELDS = {"free_code_projects_path", "dgx_ssh_key_path"}


# ── 1. Settings and env ──────────────────────────────────────────────────

def test_no_setting_points_at_the_real_machine():
    """Walks EVERY settings field. A new field whose default derives from the
    host home or the checkout fails here until conftest redirects it
    (_TEST_HOST_SETTINGS)."""
    settings = app.config.settings
    for name in type(settings).model_fields:
        if name in _CONTAINER_ONLY_FIELDS:
            continue
        _assert_not_real(f"settings.{name}", getattr(settings, name))


def test_a_fresh_settings_object_also_lands_in_the_stand_in():
    """Class defaults like mc_repo_path / wake_request_dir are computed once
    when app.config is imported — from HOME_HOST at that moment. A test that
    builds its own Settings() must get the stand-in too."""
    fresh = app.config.Settings(_env_file=None)
    for name in type(fresh).model_fields:
        if name in _CONTAINER_ONLY_FIELDS:
            continue
        _assert_not_real(f"Settings().{name}", getattr(fresh, name))


@pytest.mark.parametrize("key", ["HOME_HOST", "MC_AGENTS_ROOT", "MC_MCP_REGISTRY_DIR"])
def test_host_path_env_is_set_and_not_real(key):
    value = os.environ.get(key)
    assert value, f"{key} is unset — the app then falls back to expanduser('~'), the real home"
    _assert_not_real(key, value)


def test_mc_repo_path_env_is_gone():
    """Scripts use MC_REPO_PATH to find their own code — unset, they use the
    checkout under test instead of the real one."""
    assert "MC_REPO_PATH" not in os.environ


# ── 2. Paths the app resolved at import, and its resolver functions ──────

def test_paths_resolved_at_import_are_not_real():
    from app.routers import cli_terminal
    from app.services import (
        compose_renderer,
        dispatch,
        docker_agent_sync,
        pane_stream,
        visual_proof,
    )

    _assert_not_real("docker_agent_sync.AGENTS_DIR", docker_agent_sync.AGENTS_DIR)
    _assert_not_real("pane_stream.AGENTS_ROOT", pane_stream.AGENTS_ROOT)
    _assert_not_real("compose_renderer.DEFAULT_COMPOSE_PATH", compose_renderer.DEFAULT_COMPOSE_PATH)
    _assert_not_real(
        "compose_renderer.DEFAULT_COMPOSE_TEMPLATE_PATH",
        compose_renderer.DEFAULT_COMPOSE_TEMPLATE_PATH,
    )
    _assert_not_real("cli_terminal._HOST_LAUNCH_AGENTS", cli_terminal._HOST_LAUNCH_AGENTS)
    _assert_not_real("dispatch._BACKEND_MOUNTED_ROOTS", dispatch._BACKEND_MOUNTED_ROOTS)
    _assert_not_real("visual_proof._EVIDENCE_HOME", visual_proof._EVIDENCE_HOME)


def test_resolver_functions_are_not_real():
    from app.models.agent import Agent
    from app.services import (
        agent_bootstrap,
        fs_roots,
        host_provisioning,
        mcp_registry,
        mcp_sync,
        obsidian_export,
        plugin_manager,
        token_harvester,
        vault_migration_helpers,
    )

    _assert_not_real("agent_bootstrap._home_host()", agent_bootstrap._home_host())
    _assert_not_real("host_provisioning._home_host()", host_provisioning._home_host())
    _assert_not_real("plugin_manager._agents_dir()", plugin_manager._agents_dir())
    _assert_not_real("token_harvester._host_home()", token_harvester._host_home())
    _assert_not_real("fs_roots.mc_home()", fs_roots.mc_home())
    _assert_not_real("fs_roots.attachments_root()", fs_roots.attachments_root())
    _assert_not_real("obsidian_export._vault_root()", obsidian_export._vault_root())
    _assert_not_real("vault_migration_helpers._vault_root()", vault_migration_helpers._vault_root())
    _assert_not_real("mcp_registry._registry_root()", mcp_registry._registry_root())
    _assert_not_real(
        "mcp_sync._agent_claude_config_dir()",
        mcp_sync._agent_claude_config_dir(Agent(name="Guard Probe")),
    )


# ── 3. The two writes of the incident ────────────────────────────────────

async def test_host_launcher_sync_writes_into_the_stand_in(tmp_path):
    """The incident path: sync_host_agent_files for a native-claude host agent
    writes AGENTS_DIR/<slug>-host/start-claude.sh (test_operating_card's
    CardHostAgent created ~/.mc/agents/cardhostagent-host on every run)."""
    from app.models.agent import Agent
    from app.services import docker_agent_sync

    # Precondition first — with the isolation broken this fails BEFORE the
    # sync could write anything real.
    _assert_not_real("docker_agent_sync.AGENTS_DIR", docker_agent_sync.AGENTS_DIR)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    agent = Agent(
        id=uuid.uuid4(),
        name="HomeGuardHostAgent",
        role="orchestrator",
        emoji="🧪",
        is_board_lead=True,
        agent_runtime="host",
        harness="claude",
        workspace_path=str(workspace),
        scopes=["heartbeat"],
        tools_md="# TOOLS\n",
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        await docker_agent_sync.sync_host_agent_files(s, agent)

    launcher = docker_agent_sync.AGENTS_DIR / "homeguardhostagent-host" / "start-claude.sh"
    assert launcher.is_file(), "the launcher must land in the stand-in host home"
    assert Path(os.environ["HOME_HOST"]) in launcher.parents
    for root in _real_roots()[:2]:
        assert not (root / ".mc" / "agents" / "homeguardhostagent-host").exists()


async def test_compose_write_without_a_path_never_reaches_a_real_checkout():
    """write_compose_agents() without compose_path writes DEFAULT_COMPOSE_PATH
    (<mc_repo_path>/docker/docker-compose.agents.yml). The stand-in checkout
    does not exist — as on CI — so the write fails instead of landing in the
    real one (the live backend renders its fleet from that file)."""
    from app.services import compose_renderer

    _assert_not_real("compose_renderer.DEFAULT_COMPOSE_PATH", compose_renderer.DEFAULT_COMPOSE_PATH)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with pytest.raises(FileNotFoundError) as excinfo:
            await compose_renderer.write_compose_agents(s)
    assert str(compose_renderer.DEFAULT_COMPOSE_PATH.parent) in str(excinfo.value)


# ── 4. The per-test fixture ──────────────────────────────────────────────

def test_every_test_starts_in_an_empty_stand_in_home(request):
    assert "isolate_host_paths" in request.fixturenames
    home = Path(os.environ["HOME_HOST"])
    assert home.is_dir()
    assert list(home.iterdir()) == [], "the stand-in home must be empty at the start of a test"


def test_a_real_value_left_behind_is_put_back_on_the_stand_in():
    """A test that set a host path to the real machine without restoring it
    (or deleted HOME_HOST) must not hand that to the next test."""
    from tests import conftest

    home = Path.home()
    leaked = app.config.Settings(
        _env_file=None,
        home_host=str(home),
        mc_repo_path=str(home / "Workspace" / "Projects" / "mission-control"),
        wake_request_dir=home / ".mc" / "wake-requests",
        chat_media_root=home / ".mc" / "chat-media",
        token_harvest_paths=["~/.mc/agents"],
    )
    environ = {
        "HOME_HOST": str(home),
        "MC_AGENTS_ROOT": "~/.mc/agents",
        "MC_REPO_PATH": str(home / "Workspace" / "Projects" / "mission-control"),
    }

    conftest._redirect_host_paths(leaked, environ)

    for name in conftest._TEST_HOST_SETTINGS:
        _assert_not_real(name, getattr(leaked, name))
    for key in conftest._TEST_HOST_ENV:
        assert key in environ, f"{key} must be set again"
        _assert_not_real(key, environ[key])
    assert "MC_REPO_PATH" not in environ


def test_a_deliberate_value_is_kept():
    from tests import conftest

    chosen = app.config.Settings(_env_file=None, home_host="/srv/deliberate-home")
    environ = dict(
        conftest._TEST_HOST_ENV,
        HOME_HOST="/srv/deliberate-home",
        MC_REPO_PATH="/srv/deliberate-checkout",
    )

    conftest._redirect_host_paths(chosen, environ)

    assert chosen.home_host == "/srv/deliberate-home"
    assert environ["HOME_HOST"] == "/srv/deliberate-home"
    assert environ["MC_REPO_PATH"] == "/srv/deliberate-checkout"


def test_points_into_real_host_reads_lists_mappings_and_tilde(tmp_path):
    from tests import conftest

    real = tmp_path / "real"
    stand_in = tmp_path / "real" / "tmp" / "stand-in"  # stand-in inside a real root
    roots = (real,)

    def hit(value):
        return conftest._points_into_real_host(value, real_roots=roots, test_home=stand_in)

    assert hit(str(real))
    assert hit(real / ".mc" / "agents")
    assert hit([str(tmp_path / "elsewhere"), str(real / "x")])
    assert hit(f"/container/projects:{real}/projects")
    assert hit("~/.mc/agents")
    assert hit({"a": str(real / "x")})
    assert not hit(stand_in / ".mc" / "agents")
    assert not hit(str(tmp_path / "real-but-other"))
    assert not hit("http://localhost:8000/api")
    assert not hit(42)
    assert not hit(None)


def test_empty_test_host_home_removes_files_folders_and_links(tmp_path):
    from tests import conftest

    home = tmp_path / "home"
    (home / ".mc" / "agents" / "x-host").mkdir(parents=True)
    (home / ".mc" / "agents" / "x-host" / "start-claude.sh").write_text("#!/bin/sh\n")
    (home / "file.txt").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (home / "link").symlink_to(outside, target_is_directory=True)

    conftest._empty_test_host_home(home)

    assert list(home.iterdir()) == []
    assert (outside / "keep.txt").read_text() == "keep", "a link is removed, never followed"

    gone = tmp_path / "gone"
    conftest._empty_test_host_home(gone)
    assert gone.is_dir(), "a deleted stand-in home is created again"


# ── 5. The guard around the whole run ────────────────────────────────────

def _fake_install(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    agents = home / ".mc" / "agents"
    (agents / "alpha" / "claude-config" / "skills" / "a-skill").mkdir(parents=True)
    (agents / "alpha" / "claude-config" / "plugins").mkdir(parents=True)
    (agents / "alpha" / "claude-config" / "plugins" / "known_marketplaces.json").write_text("{}")
    (agents / "alpha" / "settings.json").write_text("{}")
    (agents / "alpha" / "logs").mkdir()
    (agents / "alpha" / "logs" / "poll.log").write_text("tick\n")
    (agents / "alpha" / "claude-config" / "projects" / "p").mkdir(parents=True)
    repo = tmp_path / "checkout"
    (repo / "docker").mkdir(parents=True)
    (repo / "docker" / "docker-compose.agents.yml").write_text("services: {}\n")
    return home, repo


def _touch_later(path: Path, text: str | None = None) -> None:
    if text is not None:
        path.write_text(text)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def test_guard_watches_only_installs_that_exist(tmp_path):
    from tests import conftest

    home, repo = _fake_install(tmp_path)
    bare_home = tmp_path / "bare-home"
    bare_home.mkdir()
    bare_repo = tmp_path / "bare-checkout"
    (bare_repo / "docker").mkdir(parents=True)  # a checkout without a fleet file (CI)

    targets = conftest._host_guard_targets(homes=(home, bare_home), repos=(repo, bare_repo))

    assert targets == (
        ("tree", (home / ".mc" / "agents").resolve()),
        ("compose", (repo / "docker").resolve()),
    )
    assert conftest._host_guard_targets(homes=(bare_home,), repos=(bare_repo,)) == ()


def test_guard_sees_what_tests_wrote_and_skips_live_noise(tmp_path):
    from tests import conftest

    home, repo = _fake_install(tmp_path)
    agents = home / ".mc" / "agents"
    targets = conftest._host_guard_targets(homes=(home,), repos=(repo,))
    before = conftest._host_guard_snapshot(targets)
    assert conftest._host_guard_diff(before, conftest._host_guard_snapshot(targets)) == []

    # What live agents write all the time: not reported.
    _touch_later(agents / "alpha" / "logs" / "poll.log", "tock\n")
    (agents / "alpha" / "claude-config" / "projects" / "p" / "t.jsonl").write_text("{}\n")
    (agents / "alpha" / "claude-config" / "pane.log").write_text("x")
    assert conftest._host_guard_diff(before, conftest._host_guard_snapshot(targets)) == []

    # What tests wrote (incident 2026-10-20): reported.
    (agents / "cardhostagent-host").mkdir()
    (agents / "cardhostagent-host" / "start-claude.sh").write_text("#!/bin/sh\n")
    _touch_later(agents / "alpha" / "settings.json", '{"x": 1}')
    _touch_later(agents / "alpha" / "claude-config" / "plugins" / "known_marketplaces.json")
    (agents / "alpha" / "claude-config" / "skills" / "b-skill").mkdir()
    _touch_later(repo / "docker" / "docker-compose.agents.yml", "services: {x: {}}\n")
    (repo / "docker" / "docker-compose.agents.yml.bak").write_text("services: {}\n")

    diff = conftest._host_guard_diff(before, conftest._host_guard_snapshot(targets))
    a = (home / ".mc" / "agents").resolve()
    d = (repo / "docker").resolve()
    assert sorted(diff) == sorted([
        f"new:     {a}/cardhostagent-host/",
        f"new:     {a}/cardhostagent-host/start-claude.sh",
        f"new:     {a}/alpha/claude-config/skills/b-skill/",
        f"new:     {d}/docker-compose.agents.yml.bak",
        f"changed: {d}/docker-compose.agents.yml",
        f"changed: {a}/alpha/claude-config/plugins/known_marketplaces.json",
        f"changed: {a}/alpha/settings.json",
    ])


def test_guard_sees_a_folder_that_was_removed(tmp_path):
    from tests import conftest

    home, repo = _fake_install(tmp_path)
    targets = conftest._host_guard_targets(homes=(home,), repos=(repo,))
    before = conftest._host_guard_snapshot(targets)
    (home / ".mc" / "agents" / "alpha" / "settings.json").unlink()

    diff = conftest._host_guard_diff(before, conftest._host_guard_snapshot(targets))

    assert diff == [f"gone:    {(home / '.mc' / 'agents').resolve()}/alpha/settings.json"]


def test_main_checkout_behind_a_worktree(tmp_path):
    from tests import conftest

    main = tmp_path / "main"
    gitdir = main / ".git" / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {gitdir}\n")

    assert conftest._main_checkout(wt) == main.resolve()
    assert conftest._main_checkout(main) == main, "a main checkout is its own"
    assert conftest._main_checkout(tmp_path / "no-git") == tmp_path / "no-git"


def _fake_session(exitstatus=0, workerinput=None):
    config = SimpleNamespace(
        stash=pytest.Stash(),
        pluginmanager=SimpleNamespace(get_plugin=lambda name: None),
    )
    if workerinput is not None:
        config.workerinput = workerinput
    return SimpleNamespace(config=config, exitstatus=exitstatus)


def test_guard_fails_the_run_with_a_clear_message(tmp_path, monkeypatch, capsys):
    from tests import conftest

    home, repo = _fake_install(tmp_path)
    targets = conftest._host_guard_targets(homes=(home,), repos=(repo,))
    monkeypatch.setattr(conftest, "_host_guard_targets", lambda: targets)

    session = _fake_session()
    conftest.pytest_sessionstart(session)
    (home / ".mc" / "agents" / "cardhostagent-host").mkdir()
    conftest.pytest_sessionfinish(session, 0)

    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    err = capsys.readouterr().err
    assert "real home or checkout changed during this test run" in err
    assert f"new:     {(home / '.mc' / 'agents').resolve()}/cardhostagent-host/" in err


def test_guard_stays_quiet_when_nothing_changed(tmp_path, monkeypatch, capsys):
    from tests import conftest

    home, repo = _fake_install(tmp_path)
    targets = conftest._host_guard_targets(homes=(home,), repos=(repo,))
    monkeypatch.setattr(conftest, "_host_guard_targets", lambda: targets)

    session = _fake_session()
    conftest.pytest_sessionstart(session)
    conftest.pytest_sessionfinish(session, 0)

    assert session.exitstatus == 0
    assert capsys.readouterr().err == ""


def test_guard_keeps_a_worse_exit_status(tmp_path, monkeypatch):
    from tests import conftest

    home, repo = _fake_install(tmp_path)
    targets = conftest._host_guard_targets(homes=(home,), repos=(repo,))
    monkeypatch.setattr(conftest, "_host_guard_targets", lambda: targets)

    session = _fake_session(exitstatus=pytest.ExitCode.INTERRUPTED)
    conftest.pytest_sessionstart(session)
    (home / ".mc" / "agents" / "new-host").mkdir()
    conftest.pytest_sessionfinish(session, pytest.ExitCode.INTERRUPTED)

    assert session.exitstatus == pytest.ExitCode.INTERRUPTED


def test_guard_runs_in_the_xdist_controller_not_in_workers(tmp_path, monkeypatch):
    from tests import conftest

    home, repo = _fake_install(tmp_path)
    targets = conftest._host_guard_targets(homes=(home,), repos=(repo,))
    monkeypatch.setattr(conftest, "_host_guard_targets", lambda: targets)

    worker = _fake_session(workerinput={"workerid": "gw0"})
    conftest.pytest_sessionstart(worker)
    assert worker.config.stash.get(conftest._HOST_GUARD_KEY, None) is None

    controller = _fake_session()
    conftest.pytest_sessionstart(controller)
    assert controller.config.stash.get(conftest._HOST_GUARD_KEY, None) is not None
