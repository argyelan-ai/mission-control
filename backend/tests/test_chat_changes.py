"""chat_changes — the Changes panel's "This chat" list, per file, from the
transcript's per-edit diffs. Fixtures are real transcript lines (omp over
the ACP bridge, Claude Code Edit/Write) run through ``read_history``, so the
whole chain parser -> merged tool event -> aggregation is covered.

Also the ``GET /agents/{id}/chat/changes`` router: a chat in a plain folder
lists its edits with ``repo: None`` — never another workspace."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.services.chat_changes import MAX_COMBINED_LINES, aggregate_changes
from app.services.chat_diff import capture_texts
from app.services.transcript_adapters import adapter_for
from app.services.transcript_chat import read_history


class _Omp:
    harness = "omp"


OMP = adapter_for(_Omp())
CLAUDE = adapter_for(None)

OMP_SESSION_LINE = json.dumps({
    "type": "session", "version": 3, "id": "01a1-test-session",
    "timestamp": "2026-10-12T16:00:00.000Z", "cwd": "/workspace",
})


def _omp_edit(n: int, path: str, old: str | None, new: str, *, error: bool = False,
              ts: str = "2026-10-12T16:18:09.000Z") -> list[str]:
    """One ACP edit as the bridge writes it: the tool call, then its settled
    result carrying ``details.diffs`` (shape of the recorded omp-18.10.1 run)."""
    call_id = f"call_{n}"
    seed = {
        "type": "message", "id": f"acp{n:05d}a", "parentId": None, "timestamp": ts,
        "message": {"role": "assistant", "model": None, "content": [{
            "type": "toolCall", "id": call_id, "name": "edit",
            "arguments": {"path": path, "i": f"edit: {path}"}, "locations": [path],
        }]},
    }
    result = {
        "type": "message", "id": f"acp{n:05d}b", "parentId": None, "timestamp": ts,
        "message": {"role": "toolResult", "toolCallId": call_id, "toolName": "edit",
                    "content": [{"type": "text", "text": "ok"}],
                    "details": {"title": f"edit: {path}", "status": "failed" if error else "completed",
                                "diffs": [{"path": path, "oldText": old, "newText": new}],
                                "locations": [path]},
                    "isError": error},
    }
    return [json.dumps(seed), json.dumps(result)]


def _claude_tool(n: int, name: str, tool_input: dict, *, error: bool = False,
                 ts: str = "2026-10-12T16:00:00Z") -> list[str]:
    call = {
        "type": "assistant", "uuid": f"a{n}", "timestamp": ts, "isSidechain": False,
        "message": {"role": "assistant", "model": "claude-sonnet-4-6", "id": f"msg_{n}",
                    "content": [{"type": "tool_use", "id": f"t{n}", "name": name, "input": tool_input}]},
    }
    block = {"type": "tool_result", "tool_use_id": f"t{n}", "content": "Error: no match" if error else "ok"}
    if error:
        block["is_error"] = True
    result = {
        "type": "user", "uuid": f"r{n}", "timestamp": ts, "isSidechain": False,
        "message": {"role": "user", "content": [block]},
    }
    return [json.dumps(call), json.dumps(result)]


def _events(tmp_path: Path, lines: list[str], adapter) -> list[dict]:
    f = tmp_path / "sess.jsonl"
    f.write_text("\n".join(lines) + "\n")
    # Same as the router: read inside capture_texts() so each diff carries
    # ``_texts`` — without it, aggregate_changes cannot compute a net diff
    # and always falls back to the per-edit sum (also covered below).
    with capture_texts():
        return read_history(f, adapter, limit=10**9)["events"]


def test_omp_acp_edits_fold_into_one_entry_per_file_newest_first(tmp_path):
    lines = [OMP_SESSION_LINE]
    lines += _omp_edit(1, "/workspace/demo.py", "a\nb\n", "a\nB\n", ts="2026-10-12T16:01:00.000Z")
    lines += _omp_edit(2, "notes/new.md", None, "x\ny\n", ts="2026-10-12T16:02:00.000Z")
    lines += _omp_edit(3, "/workspace/demo.py", "a\nB\n", "a\nB\nc\n", ts="2026-10-12T16:03:00.000Z")

    out = aggregate_changes(_events(tmp_path, lines, OMP), cwd="/workspace")

    assert [f["path"] for f in out["files"]] == ["demo.py", "notes/new.md"]
    demo, new = out["files"]
    assert (demo["additions"], demo["deletions"], demo["edits"]) == (2, 1, 2)
    assert demo["created"] is False and demo["lastAt"] == "2026-10-12T16:03:00.000Z"
    # Both edits, oldest first, each opened by its own hunk separator.
    assert [ln["t"] for ln in demo["diff"]["lines"]].count("hunk") == 2
    assert demo["diff"]["kind"] == "edit"
    assert (new["created"], new["additions"], new["diff"]["kind"]) == (True, 2, "create")
    assert out["stats"] == {"files": 2, "additions": 4, "deletions": 1}


def test_failed_edits_and_mc_helper_files_are_not_changes(tmp_path):
    lines = [OMP_SESSION_LINE]
    lines += _omp_edit(1, "/workspace/bad.py", "a\n", "b\n", error=True)
    lines += _omp_edit(2, "/workspace/.mc-expected-remote", None, "https://example.invalid/r.git\n")
    lines += _omp_edit(3, "/workspace/ok.py", "a\n", "b\n")

    out = aggregate_changes(_events(tmp_path, lines, OMP), cwd="/workspace")

    assert [f["path"] for f in out["files"]] == ["ok.py"]


def test_claude_edit_and_write_the_same_harness_neutral_list(tmp_path):
    lines = []
    lines += _claude_tool(1, "Edit", {"file_path": "/workspace/app/a.py", "old_string": "x = 1", "new_string": "x = 2"})
    lines += _claude_tool(2, "Write", {"file_path": "/workspace/app/b.py", "content": "print(1)\nprint(2)\n"})
    lines += _claude_tool(3, "Edit", {"file_path": "/workspace/app/c.py", "old_string": "nope", "new_string": "x"}, error=True)
    lines += _claude_tool(4, "Read", {"file_path": "/workspace/app/d.py"})

    out = aggregate_changes(_events(tmp_path, lines, CLAUDE), cwd="/workspace")

    assert [f["path"] for f in out["files"]] == ["app/b.py", "app/a.py"]
    written, edited = out["files"]
    # A Write's predecessor is unknown: no +/− claimed, but said that it happened.
    assert (written["written"], written["additions"], written["diff"]["kind"]) == (True, 0, "write")
    assert (edited["additions"], edited["deletions"], edited["written"]) == (1, 1, False)


def test_created_file_edited_twice_reports_the_net_diff_not_the_sum(tmp_path):
    """The live bug (12./13.10.2026): a file created at ``price = 1`` then
    edited to ``price = 2`` and ``price = 3`` summed to "+3 -2" — wrong, the
    net change against what this chat actually left behind is "+1 -0"."""
    lines = [OMP_SESSION_LINE]
    lines += _omp_edit(1, "/workspace/prices.py", None, "price = 1\n", ts="2026-10-12T16:01:00.000Z")
    lines += _omp_edit(2, "/workspace/prices.py", "price = 1\n", "price = 2\n", ts="2026-10-12T16:02:00.000Z")
    lines += _omp_edit(3, "/workspace/prices.py", "price = 2\n", "price = 3\n", ts="2026-10-12T16:03:00.000Z")

    out = aggregate_changes(_events(tmp_path, lines, OMP), cwd="/workspace")

    (f,) = out["files"]
    assert (f["created"], f["edits"]) == (True, 3)
    assert (f["additions"], f["deletions"]) == (1, 0)
    assert out["stats"] == {"files": 1, "additions": 1, "deletions": 0}


def test_write_then_edit_without_a_known_predecessor_falls_back_to_the_sum(tmp_path):
    """A Claude ``Write`` never carries its predecessor, so the net diff
    cannot be computed from it — the sum of the per-edit diffs stands, same
    as before this fix."""
    lines = []
    lines += _claude_tool(1, "Write", {"file_path": "/workspace/app/b.py", "content": "x = 1\ny = 2\n"})
    lines += _claude_tool(2, "Edit", {"file_path": "/workspace/app/b.py", "old_string": "y = 2", "new_string": "y = 3"})

    out = aggregate_changes(_events(tmp_path, lines, CLAUDE), cwd="/workspace")

    (f,) = out["files"]
    assert (f["additions"], f["deletions"]) == (1, 1)


def test_paths_outside_the_session_folder_stay_whole():
    events = [{"kind": "tool", "status": "done", "ts": "t", "diffs": [
        {"path": "/home/agent/.bashrc", "kind": "edit", "lines": [], "additions": 1, "deletions": 0, "truncated": False},
        {"path": "/workspace/./src/../x.py", "kind": "edit", "lines": [], "additions": 1, "deletions": 0, "truncated": False},
    ]}]
    out = aggregate_changes(events, cwd="/workspace")
    assert {f["path"] for f in out["files"]} == {"/home/agent/.bashrc", "x.py"}


def test_combined_diff_is_capped_and_says_so():
    big = [{"t": "add", "text": "x"}] * 700
    events = [
        {"kind": "tool", "status": "done", "ts": str(i), "diffs": [
            {"path": "f.py", "kind": "edit", "lines": big, "additions": 700, "deletions": 0, "truncated": False}]}
        for i in range(3)
    ]
    (f,) = aggregate_changes(events, cwd=None)["files"]
    assert len(f["diff"]["lines"]) == MAX_COMBINED_LINES
    assert f["diff"]["truncated"] is True
    assert f["additions"] == 2100, "the counts stay true even when lines are cut"


def test_odd_input_never_raises():
    assert aggregate_changes([None, {"kind": "tool"}, {"kind": "tool", "diffs": [None, {"path": ""}]}])["files"] == []


# ══════════════════════════════════════════════════════════════════════════
# Router: GET /agents/{id}/chat/changes
# ══════════════════════════════════════════════════════════════════════════


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def omp_home(tmp_path, monkeypatch):
    from app.services import omp_chat, transcript_chat
    from app.services import workspace_diff as wd
    home = tmp_path / "home"
    monkeypatch.setattr(omp_chat, "_host_home", lambda: home)
    monkeypatch.setattr(wd, "_host_home", lambda: home)
    monkeypatch.setattr(transcript_chat, "_host_home", lambda: home)
    return home


def _write_session(home: Path, cwd: str, lines: list[str]) -> None:
    folder = home / ".mc" / "agents" / "alpha" / "omp-sessions" / "--workspace--"
    folder.mkdir(parents=True, exist_ok=True)
    header = json.dumps({"type": "session", "version": 3, "id": "01a1-test-session",
                         "timestamp": "2026-10-12T16:00:00.000Z", "cwd": cwd})
    (folder / "2026-10-12T16-00-00-000Z_01a1-test-session.jsonl").write_text(
        "\n".join([header, *lines]) + "\n"
    )


async def test_changes_router_plain_folder_lists_edits_without_a_repo(
    auth_client: AsyncClient, make_agent, omp_home
):
    """The live case: omp works in /workspace (no repo) above an old,
    unrelated worktree. The list shows the chat's own edits; ``repo`` is
    None — never that worktree."""
    root = omp_home / ".mc" / "workspaces" / "alpha"
    bench = root / "worktrees" / "bench-garden"
    bench.mkdir(parents=True)
    _git("init", "-b", "main", cwd=bench)
    _write_session(omp_home, "/workspace", [
        *_omp_edit(1, "/workspace/hello.py", "a\n", "a\nb\n"),
        *_omp_edit(2, "/workspace/README.md", None, "# hi\n"),
    ])
    agent = await make_agent(name="Alpha", slug="alpha", harness="omp",
                             agent_runtime="cli-bridge", workspace_path=str(root))

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/changes")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["repo"] is None
    assert [f["path"] for f in body["files"]] == ["README.md", "hello.py"]
    assert body["stats"] == {"files": 2, "additions": 2, "deletions": 0}


async def test_changes_router_names_the_chat_repo_when_there_is_one(
    auth_client: AsyncClient, make_agent, omp_home
):
    root = omp_home / ".mc" / "workspaces" / "alpha"
    proj = root / "proj"
    proj.mkdir(parents=True)
    _git("init", "-b", "feat/x", cwd=proj)
    _write_session(omp_home, "/workspace/proj", _omp_edit(1, "src/a.py", "1\n", "2\n"))
    agent = await make_agent(name="Alpha", slug="alpha", harness="omp",
                             agent_runtime="cli-bridge", workspace_path=str(root))

    body = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/changes")).json()

    assert body["repo"]["repo"] == "proj" and body["repo"]["branch"] == "feat/x"
    assert body["repo"]["kind"] == "session"
    assert [f["path"] for f in body["files"]] == ["src/a.py"]


async def test_changes_router_without_a_transcript_is_an_empty_list(
    auth_client: AsyncClient, make_agent, omp_home, tmp_path
):
    plain = tmp_path / "plain"
    plain.mkdir()
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", workspace_path=str(plain))

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/changes")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"files": [], "stats": {"files": 0, "additions": 0, "deletions": 0}, "repo": None}


async def test_changes_router_requires_auth(client: AsyncClient, make_agent, tmp_path):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", workspace_path=str(tmp_path))
    assert (await client.get(f"/api/v1/agents/{agent.id}/chat/changes")).status_code == 401
