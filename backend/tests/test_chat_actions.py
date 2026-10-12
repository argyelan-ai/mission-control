"""chat_actions — the Changes panel's write actions (Changes panel, part 3):
revert one file the chat changed, commit, open a pull request.

The guards are the point, so most tests are refusals: a path out of the
chat's folder, a file changed after the chat, a push to the default branch,
a call without the operator's confirmation. Fixtures are real transcript
lines (omp over the ACP bridge, Claude Code Edit/Write) run through
``read_history`` and the HTTP endpoints, so the whole chain is covered."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.services import chat_actions
from app.services.chat_actions import Edit, Refused, reconstruct_original
from app.services.chat_diff import build_diff, capture_texts, diffs_from_claude_tool

from tests.test_chat_changes import _claude_tool, _omp_edit, _write_session


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout


def _repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", branch, cwd=path)
    _git("config", "user.email", "t@example.invalid", cwd=path)
    _git("config", "user.name", "T", cwd=path)
    return path


def _commit(repo: Path, msg: str = "init") -> None:
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", msg, cwd=repo)


# ══════════════════════════════════════════════════════════════════════════
# Raw texts only inside capture_texts()
# ══════════════════════════════════════════════════════════════════════════


def test_raw_texts_never_ride_a_normal_diff():
    assert "_texts" not in build_diff("a.py", "1\n", "2\n")
    with capture_texts():
        d = build_diff("a.py", "1\n", "2\n")
    assert d["_texts"] == {"old": "1\n", "new": "2\n", "scope": "file", "complete": True, "replaceAll": False}
    assert "_texts" not in build_diff("a.py", "1\n", "2\n"), "the context ends with the block"


def test_claude_edit_texts_are_snippets():
    with capture_texts():
        (d,) = diffs_from_claude_tool("Edit", {"file_path": "/w/a.py", "old_string": "x = 1",
                                               "new_string": "x = 2", "replace_all": True})
    assert d["_texts"]["scope"] == "snippet" and d["_texts"]["replaceAll"] is True


# ══════════════════════════════════════════════════════════════════════════
# reconstruct_original — what was there before the chat
# ══════════════════════════════════════════════════════════════════════════


def _file(old, new, kind="edit", complete=True):
    return Edit(kind=kind, scope="file", old=old, new=new, complete=complete)


def _snip(old, new, replace_all=False):
    return Edit(kind="edit", scope="snippet", old=old, new=new, complete=True, replace_all=replace_all)


def test_whole_file_chain_walks_back_to_the_first_old_text():
    edits = [_file("a\n", "a\nb\n"), _file("a\nb\n", "a\nb\nc\n")]
    assert reconstruct_original(edits, "a\nb\nc\n") == chat_actions.Original(True, "a\n")


def test_a_created_file_was_absent():
    assert reconstruct_original([_file(None, "x\n", kind="create")], "x\n") == chat_actions.Original(True, None)


def test_file_changed_after_the_chat_is_refused():
    with pytest.raises(Refused) as exc:
        reconstruct_original([_file("a\n", "b\n")], "b\nhand edit\n")
    assert exc.value.reason == "changed_externally"


def test_deleted_file_is_changed_externally():
    with pytest.raises(Refused) as exc:
        reconstruct_original([_file("a\n", "b\n")], None)
    assert exc.value.reason == "changed_externally"


def test_snippet_chain_is_swapped_back_in_reverse():
    edits = [_snip("x = 1", "x = 2"), _snip("y = 1", "y = 9")]
    assert reconstruct_original(edits, "x = 2\ny = 9\n").content == "x = 1\ny = 1\n"


def test_snippet_gone_is_changed_externally_and_ambiguous_is_cannot_restore():
    with pytest.raises(Refused) as gone:
        reconstruct_original([_snip("x = 1", "x = 2")], "x = 3\n")
    assert gone.value.reason == "changed_externally"
    with pytest.raises(Refused) as twice:
        reconstruct_original([_snip("x = 1", "x = 2")], "x = 2\nx = 2\n")
    assert twice.value.reason == "cannot_restore"
    with pytest.raises(Refused) as all_:
        reconstruct_original([_snip("x = 1", "x = 2", replace_all=True)], "x = 2\n")
    assert all_.value.reason == "cannot_restore"


def test_a_write_is_verified_but_its_predecessor_unknown():
    out = reconstruct_original([_file(None, "new\n", kind="write")], "new\n")
    assert out == chat_actions.Original(False, reason="unknown_original")
    with pytest.raises(Refused) as exc:
        reconstruct_original([_file(None, "new\n", kind="write")], "new\nmore\n")
    assert exc.value.reason == "changed_externally"


def test_cut_texts_are_too_large():
    with pytest.raises(Refused) as exc:
        reconstruct_original([_file("a", "b", complete=False)], "b")
    assert exc.value.reason == "too_large"


# ══════════════════════════════════════════════════════════════════════════
# resolve_target — only inside the chat's folder
# ══════════════════════════════════════════════════════════════════════════


def test_target_must_stay_inside_the_session_folder(tmp_path):
    root = tmp_path / "ws"
    sess = root / "proj"
    sess.mkdir(parents=True)
    (root / "secret.txt").write_text("s")
    kw = {"runtime": "cli-bridge", "workspace_root": root, "allowed_roots": [sess]}

    target, base = chat_actions.resolve_target("/workspace/proj/a.py", **kw)
    assert target == sess.resolve() / "a.py" and base == sess.resolve()

    for bad in ("/workspace/secret.txt", "/workspace/proj/../secret.txt", "/home/agent/.bashrc",
                "/etc/passwd", "relative.py", "/workspace/proj/.git/config"):
        with pytest.raises(Refused) as exc:
            chat_actions.resolve_target(bad, **kw)
        assert exc.value.reason == "path_outside", bad


def test_symlinks_do_not_lead_out(tmp_path):
    root = tmp_path / "ws"
    sess = root / "proj"
    sess.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (sess / "link").symlink_to(outside)
    (sess / "f.py").symlink_to(outside / "f.py")
    kw = {"runtime": "cli-bridge", "workspace_root": root, "allowed_roots": [sess]}
    for bad in ("/workspace/proj/link/x.py", "/workspace/proj/f.py"):
        with pytest.raises(Refused) as exc:
            chat_actions.resolve_target(bad, **kw)
        assert exc.value.reason == "path_outside", bad


# ══════════════════════════════════════════════════════════════════════════
# Commit + PR guards (git, local only)
# ══════════════════════════════════════════════════════════════════════════


def test_commit_on_a_branch_takes_everything_but_mc_helper_files(tmp_path):
    repo = _repo(tmp_path / "r", "feat/x")
    (repo / "a.py").write_text("1\n")
    _commit(repo)
    (repo / "a.py").write_text("2\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "n.md").write_text("n\n")
    (repo / ".mc-expected-remote").write_text("https://example.invalid/r.git\n")

    files = chat_actions.pending_files(repo)
    assert {f["path"] for f in files} == {"a.py", "docs/n.md"}
    msg = chat_actions.suggest_commit_message(files)
    assert msg.startswith("chore: update a.py and n.md\n\n") and "- docs/n.md (+1)" in msg

    out = chat_actions.commit_all(repo, msg)
    assert out["status"] == "committed" and out["branch"] == "feat/x"
    assert _git("status", "--porcelain", cwd=repo).strip() == "?? .mc-expected-remote"
    assert _git("log", "-1", "--format=%s", cwd=repo).strip() == "chore: update a.py and n.md"
    # A second tap is harmless.
    assert chat_actions.commit_all(repo, msg)["status"] == "nothing_to_commit"


def test_commit_on_the_default_branch_of_a_remote_repo_is_refused(tmp_path):
    repo = _repo(tmp_path / "r", "main")
    (repo / "a.py").write_text("1\n")
    _git("remote", "add", "origin", "https://github.com/example/r.git", cwd=repo)
    with pytest.raises(Refused) as exc:
        chat_actions.commit_all(repo, "feat: x")
    assert exc.value.reason == "default_branch"
    assert _git("status", "--porcelain", cwd=repo).strip() == "?? a.py", "nothing staged"


def test_commit_without_a_remote_may_use_main(tmp_path):
    repo = _repo(tmp_path / "r", "main")
    (repo / "a.py").write_text("1\n")
    assert chat_actions.commit_all(repo, "feat: add a.py")["status"] == "committed"


def test_pr_guard_never_offers_the_default_branch(tmp_path):
    repo = _repo(tmp_path / "r", "main")
    (repo / "a.py").write_text("1\n")
    _commit(repo)
    with pytest.raises(Refused) as no_remote:
        chat_actions.pr_guard(repo)
    assert no_remote.value.reason == "no_remote"

    _git("remote", "add", "origin", "https://gitlab.example/r.git", cwd=repo)
    _git("checkout", "-q", "-b", "feat/y", cwd=repo)
    with pytest.raises(Refused) as gitlab:
        chat_actions.pr_guard(repo)
    assert gitlab.value.reason == "not_github"

    _git("remote", "set-url", "origin", "https://github.com/example/r.git", cwd=repo)
    _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=repo)
    with pytest.raises(Refused) as no_commits:
        chat_actions.pr_guard(repo)
    assert no_commits.value.reason == "no_commits"

    (repo / "a.py").write_text("2\n")
    _commit(repo, "feat: two")
    plan = chat_actions.pr_guard(repo)
    assert plan == {"branch": "feat/y", "base": "main", "title": "feat: two", "ahead": 1, "uncommitted": 0}

    _git("checkout", "-q", "main", cwd=repo)
    with pytest.raises(Refused) as main:
        chat_actions.pr_guard(repo)
    assert main.value.reason == "default_branch"

    # A remote whose default is "develop": main is still never pushed.
    _git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop", cwd=repo)
    _git("update-ref", "refs/remotes/origin/develop", "HEAD", cwd=repo)
    with pytest.raises(Refused) as protected:
        chat_actions.pr_guard(repo)
    assert protected.value.reason == "default_branch"


# ══════════════════════════════════════════════════════════════════════════
# Router: POST /agents/{id}/chat/revert
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def omp_home(tmp_path, monkeypatch):
    from app.services import omp_chat, transcript_chat
    from app.services import workspace_diff as wd
    home = tmp_path / "home"
    monkeypatch.setattr(omp_chat, "_host_home", lambda: home)
    monkeypatch.setattr(wd, "_host_home", lambda: home)
    monkeypatch.setattr(transcript_chat, "_host_home", lambda: home)
    return home


async def _omp_agent(make_agent, home: Path, cwd: str, lines: list[str]):
    root = home / ".mc" / "workspaces" / "alpha"
    root.mkdir(parents=True, exist_ok=True)
    _write_session(home, cwd, lines)
    agent = await make_agent(name="Alpha", slug="alpha", harness="omp",
                             agent_runtime="cli-bridge", workspace_path=str(root))
    return agent, root


async def _events(agent_id, event_type: str):
    from app.models.activity import ActivityEvent
    from tests.conftest import test_engine
    async with AsyncSession(test_engine) as s:
        rows = (await s.exec(select(ActivityEvent).where(
            ActivityEvent.agent_id == agent_id, ActivityEvent.event_type == event_type))).all()
    return rows


async def test_revert_puts_the_file_back_and_a_repeat_is_harmless(
    auth_client: AsyncClient, make_agent, omp_home
):
    agent, root = await _omp_agent(make_agent, omp_home, "/workspace", [
        *_omp_edit(1, "/workspace/prices.py", "price = 1\n", "price = 2\n"),
        *_omp_edit(2, "/workspace/prices.py", "price = 2\n", "price = 3\n"),
    ])
    (root / "prices.py").write_text("price = 3\n")
    url = f"/api/v1/agents/{agent.id}/chat/revert"

    resp = await auth_client.post(url, json={"path": "prices.py", "confirm": True})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "reverted", "path": "prices.py", "method": "chat",
                           "deleted": False, "edits": 2}
    assert (root / "prices.py").read_text() == "price = 1\n"
    (ev,) = await _events(agent.id, "chat.file_reverted")
    assert ev.detail["actor"] == "operator" and ev.detail["key"] == "/workspace/prices.py"

    again = await auth_client.post(url, json={"path": "prices.py", "confirm": True})
    assert again.status_code == 200 and again.json()["status"] == "already"
    assert len(await _events(agent.id, "chat.file_reverted")) == 1

    listed = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/changes")).json()
    assert listed["files"][0]["reverted"] is True


async def test_revert_deletes_a_file_the_chat_created(auth_client: AsyncClient, make_agent, omp_home):
    agent, root = await _omp_agent(make_agent, omp_home, "/workspace",
                                   _omp_edit(1, "/workspace/new.md", None, "# hi\n"))
    (root / "new.md").write_text("# hi\n")
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/revert",
                                  json={"path": "new.md", "confirm": True})
    assert resp.status_code == 200 and resp.json()["deleted"] is True
    assert not (root / "new.md").exists()


async def test_revert_refuses_a_file_changed_after_the_chat(auth_client: AsyncClient, make_agent, omp_home):
    agent, root = await _omp_agent(make_agent, omp_home, "/workspace",
                                   _omp_edit(1, "/workspace/a.py", "1\n", "2\n"))
    (root / "a.py").write_text("2\nedited by bash\n")

    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/revert",
                                  json={"path": "a.py", "confirm": True})

    assert resp.status_code == 409 and resp.json() == {"reason": "changed_externally"}
    assert (root / "a.py").read_text() == "2\nedited by bash\n", "never overwritten"
    (ev,) = await _events(agent.id, "chat.action_refused")
    assert ev.detail["reason"] == "changed_externally" and ev.detail["actor"] == "operator"


async def test_revert_without_confirmation_does_nothing(auth_client: AsyncClient, make_agent, omp_home):
    agent, root = await _omp_agent(make_agent, omp_home, "/workspace",
                                   _omp_edit(1, "/workspace/a.py", "1\n", "2\n"))
    (root / "a.py").write_text("2\n")
    for body in ({"path": "a.py"}, {"path": "a.py", "confirm": False}):
        resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/revert", json=body)
        assert resp.status_code == 400 and resp.json() == {"reason": "confirm_required"}
    assert (root / "a.py").read_text() == "2\n"


async def test_revert_refuses_paths_outside_the_chat_folder(auth_client: AsyncClient, make_agent, omp_home):
    """Even a file the chat DID edit is refused when it lies outside the
    chat's folder — and a path the chat never touched is not even looked up."""
    agent, root = await _omp_agent(make_agent, omp_home, "/workspace/proj", [
        *_omp_edit(1, "/workspace/other.txt", "a\n", "b\n"),
        *_omp_edit(2, "/workspace/proj/../other.txt", "b\n", "c\n"),
        *_omp_edit(3, "/home/agent/.bashrc", "a\n", "b\n"),
    ])
    (root / "proj").mkdir()
    (root / "other.txt").write_text("c\n")
    url = f"/api/v1/agents/{agent.id}/chat/revert"

    for path in ("/workspace/other.txt", "../other.txt", "/home/agent/.bashrc"):
        resp = await auth_client.post(url, json={"path": path, "confirm": True})
        assert resp.status_code == 400 and resp.json() == {"reason": "path_outside"}, path
    assert (root / "other.txt").read_text() == "c\n"

    resp = await auth_client.post(url, json={"path": "../../../etc/hosts", "confirm": True})
    assert resp.status_code == 404 and resp.json() == {"reason": "not_changed_by_chat"}


async def test_revert_in_a_repo_uses_git_when_the_original_is_the_last_commit(
    auth_client: AsyncClient, make_agent, omp_home
):
    """Claude Code Edit (snippets) on a tracked file: walked back, and since
    that equals HEAD, git restores it — the working tree is clean again."""
    home = omp_home
    root = home / ".mc" / "workspaces" / "rex"
    repo = _repo(root / "proj", "feat/x")
    (repo / "app.py").write_text("x = 1\ny = 1\n")
    _commit(repo)
    (repo / "app.py").write_text("x = 2\ny = 1\n")
    tdir = home / ".mc" / "agents" / "rex" / "claude-config" / "projects" / "-home-agent"
    tdir.mkdir(parents=True)
    header = json.dumps({"type": "user", "uuid": "u0", "timestamp": "2026-10-12T16:00:00Z",
                         "cwd": "/workspace/proj", "isSidechain": False,
                         "message": {"role": "user", "content": "go"}})
    lines = [header, *_claude_tool(1, "Edit", {"file_path": "/workspace/proj/app.py",
                                               "old_string": "x = 1", "new_string": "x = 2"})]
    (tdir / "s1.jsonl").write_text("\n".join(lines) + "\n")
    agent = await make_agent(name="Rex", slug="rex", agent_runtime="cli-bridge", workspace_path=str(root))

    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/revert",
                                  json={"path": "app.py", "confirm": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["method"] == "git"
    assert (repo / "app.py").read_text() == "x = 1\ny = 1\n"
    assert _git("status", "--porcelain", cwd=repo).strip() == ""


async def test_revert_requires_auth(client: AsyncClient, make_agent, tmp_path):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", workspace_path=str(tmp_path))
    resp = await client.post(f"/api/v1/agents/{agent.id}/chat/revert", json={"path": "a", "confirm": True})
    assert resp.status_code == 401


# ══════════════════════════════════════════════════════════════════════════
# Router: commit + PR
# ══════════════════════════════════════════════════════════════════════════


async def _repo_agent(make_agent, home: Path, branch: str, remote: str | None = None):
    root = home / ".mc" / "workspaces" / "alpha"
    repo = _repo(root / "proj", branch)
    (repo / "a.py").write_text("1\n")
    _commit(repo)
    if remote:
        _git("remote", "add", "origin", remote, cwd=repo)
        _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=repo)
    _write_session(home, "/workspace/proj", _omp_edit(1, "a.py", "1\n", "2\n"))
    agent = await make_agent(name="Alpha", slug="alpha", harness="omp",
                             agent_runtime="cli-bridge", workspace_path=str(root))
    return agent, repo


async def test_commit_draft_and_commit(auth_client: AsyncClient, make_agent, omp_home):
    agent, repo = await _repo_agent(make_agent, omp_home, "feat/x")
    (repo / "a.py").write_text("2\n")
    base = f"/api/v1/agents/{agent.id}/chat"

    draft = (await auth_client.get(f"{base}/commit/draft")).json()
    assert draft["branch"] == "feat/x" and draft["message"].startswith("chore: update a.py")
    changes = (await auth_client.get(f"{base}/changes")).json()
    assert changes["actions"]["commit"] == {"ok": True, "reason": None, "files": 1}

    assert (await auth_client.post(f"{base}/commit", json={"message": "x"})).status_code == 400
    resp = await auth_client.post(f"{base}/commit", json={"message": "fix: two", "confirm": True})
    assert resp.status_code == 200 and resp.json()["status"] == "committed"
    assert _git("log", "-1", "--format=%s", cwd=repo).strip() == "fix: two"
    (ev,) = await _events(agent.id, "chat.committed")
    assert ev.detail["actor"] == "operator" and ev.detail["branch"] == "feat/x"


async def test_commit_on_main_with_a_remote_is_refused(auth_client: AsyncClient, make_agent, omp_home):
    agent, repo = await _repo_agent(make_agent, omp_home, "main", "https://github.com/example/r.git")
    (repo / "a.py").write_text("2\n")
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/commit",
                                  json={"message": "x", "confirm": True})
    assert resp.status_code == 409 and resp.json() == {"reason": "default_branch"}


@pytest.fixture
def fake_github(monkeypatch):
    """GitService's push + PR calls recorded instead of run."""
    from app.services.git_service import git_service
    calls: list[tuple] = []

    async def push_branch(cwd, branch):
        calls.append(("push", branch))

    async def create_pr(cwd, title, body, base="main"):
        calls.append(("pr", title, base))
        return "Creating pull request…\nhttps://github.com/example/r/pull/7\n"

    async def run_cmd(*args, cwd=None):
        calls.append(("cmd", *args))
        return ""

    monkeypatch.setattr(git_service, "push_branch", push_branch)
    monkeypatch.setattr(git_service, "create_pr", create_pr)
    monkeypatch.setattr(git_service, "_run_cmd", run_cmd)
    return calls


async def test_pr_never_pushes_main(auth_client: AsyncClient, make_agent, omp_home, fake_github):
    agent, repo = await _repo_agent(make_agent, omp_home, "main", "https://github.com/example/r.git")
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/pr",
                                  json={"title": "x", "confirm": True})
    assert resp.status_code == 409 and resp.json() == {"reason": "default_branch"}
    assert fake_github == [], "nothing pushed, nothing opened"


async def test_pr_pushes_the_branch_and_opens_once(auth_client: AsyncClient, make_agent, omp_home, fake_github):
    agent, repo = await _repo_agent(make_agent, omp_home, "main", "https://github.com/example/r.git")
    _git("checkout", "-q", "-b", "feat/prices", cwd=repo)
    (repo / "a.py").write_text("2\n")
    _commit(repo, "feat: new prices")
    base = f"/api/v1/agents/{agent.id}/chat"

    draft = (await auth_client.get(f"{base}/pr/draft")).json()
    assert draft["title"] == "feat: new prices" and draft["base"] == "main"
    assert (await auth_client.post(f"{base}/pr", json={"title": "t"})).status_code == 400
    assert fake_github == []

    resp = await auth_client.post(f"{base}/pr", json={"title": "feat: new prices", "confirm": True})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "opened", "url": "https://github.com/example/r/pull/7",
                           "branch": "feat/prices", "base": "main"}
    assert ("push", "feat/prices") in fake_github and ("pr", "feat: new prices", "main") in fake_github
    assert not any("--force" in c or "-f" in c for c in fake_github)
    (ev,) = await _events(agent.id, "chat.pr_opened")
    assert ev.detail["url"].endswith("/pull/7") and ev.detail["actor"] == "operator"
