"""Branch diff for the task cockpit: `git diff base...HEAD` parsed with the same
structure the per-commit diff uses (files → hunks → lines), plus totals.
"""
from __future__ import annotations

import pytest

from app.services.git_service import GitService, parse_unified_diff

SAMPLE = """diff --git a/backend/app/services/task_lifecycle.py b/backend/app/services/task_lifecycle.py
index 1111111..2222222 100644
--- a/backend/app/services/task_lifecycle.py
+++ b/backend/app/services/task_lifecycle.py
@@ -212,3 +212,4 @@ async def transition(
     async with session.begin_nested():
-        task = await session.get(Task, task_id)
+        stmt = select(Task).where(Task.id == task_id).with_for_update()
+        task = (await session.exec(stmt)).one()
         if task.status == new_status:
diff --git a/backend/app/services/outbox.py b/backend/app/services/outbox.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/backend/app/services/outbox.py
@@ -0,0 +1,2 @@
+class Outbox:
+    pass
"""


def test_parse_unified_diff_counts_files_and_lines():
    files = parse_unified_diff(SAMPLE)
    assert [f["filename"] for f in files] == [
        "backend/app/services/task_lifecycle.py",
        "backend/app/services/outbox.py",
    ]
    lifecycle, outbox = files
    assert (lifecycle["additions"], lifecycle["deletions"]) == (2, 1)
    assert (outbox["additions"], outbox["deletions"]) == (2, 0)
    assert lifecycle["hunks"][0]["lines"][1] == {
        "type": "del",
        "content": "        task = await session.get(Task, task_id)",
        "old_no": 213,
        "new_no": None,
    }


@pytest.mark.asyncio
async def test_get_branch_diff_runs_three_dot_diff_against_base(monkeypatch):
    calls: list[tuple] = []

    async def fake_run(self, *args, cwd=None):
        calls.append(args)
        if args[:2] == ("git", "merge-base"):
            return "abc1234\n"
        if args[:2] == ("git", "rev-list"):
            return "14\n"
        if args[:2] == ("git", "diff"):
            return SAMPLE
        raise AssertionError(f"unexpected git call {args}")

    monkeypatch.setattr(GitService, "_run_cmd", fake_run)
    svc = GitService()
    diff = await svc.get_branch_diff("/tmp/ws", base="main")

    assert ("git", "diff", "--unified=3", "--no-color", "main...HEAD") in calls
    assert diff["base"] == "main"
    assert diff["merge_base"] == "abc1234"
    assert diff["commits"] == 14
    assert diff["stats"] == {"files": 2, "additions": 4, "deletions": 1}
    assert diff["files"][1]["filename"] == "backend/app/services/outbox.py"


@pytest.mark.asyncio
async def test_get_branch_diff_empty_when_branch_has_no_changes(monkeypatch):
    async def fake_run(self, *args, cwd=None):
        if args[:2] == ("git", "merge-base"):
            return "abc1234\n"
        if args[:2] == ("git", "rev-list"):
            return "0\n"
        return ""

    monkeypatch.setattr(GitService, "_run_cmd", fake_run)
    diff = await GitService().get_branch_diff("/tmp/ws")
    assert diff["files"] == []
    assert diff["stats"] == {"files": 0, "additions": 0, "deletions": 0}
    assert diff["commits"] == 0
