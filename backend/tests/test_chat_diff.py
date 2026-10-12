"""chat_diff — one diff shape for every harness (chat package P1).

ACP agents send whole-file old/new text per edit; Claude Code transcripts
carry the edit's own old/new strings. Both become the same compact unified
diff the tool card renders.
"""
from app.services.chat_diff import MAX_DIFF_LINES, build_diff, diffs_from_claude_tool


def kinds(diff):
    return [ln["t"] for ln in diff["lines"]]


def test_whole_file_edit_becomes_a_compact_unified_diff():
    d = build_diff("/w/demo.py", "def value():\n    return 1\n", "def value():\n    # doubled\n    return 2\n")
    assert d["path"] == "/w/demo.py"
    assert d["kind"] == "edit"
    assert (d["additions"], d["deletions"]) == (2, 1)
    assert kinds(d) == ["hunk", "ctx", "del", "add", "add"]
    assert [ln["text"] for ln in d["lines"][1:]] == [
        "def value():", "    return 1", "    # doubled", "    return 2",
    ]
    assert d["truncated"] is False


def test_null_old_text_is_a_created_file_all_additions():
    d = build_diff("/w/new.txt", None, "a\nb\n")
    assert d["kind"] == "create"
    assert (d["additions"], d["deletions"]) == (2, 0)
    assert kinds(d) == ["hunk", "add", "add"]


def test_identical_texts_have_no_diff():
    assert build_diff("/w/a", "same\n", "same\n") is None


def test_long_diff_is_line_capped_but_counts_stay_true():
    old = "".join(f"o{i}\n" for i in range(1000))
    new = "".join(f"n{i}\n" for i in range(1000))
    d = build_diff("/w/big", old, new)
    assert len(d["lines"]) == MAX_DIFF_LINES
    assert d["truncated"] is True
    assert (d["additions"], d["deletions"]) == (1000, 1000)


def test_input_flagged_truncated_stays_truncated():
    assert build_diff("/w/a", "x\n", "y\n", truncated=True)["truncated"] is True


def test_very_long_line_is_clipped():
    d = build_diff("/w/a", "", "z" * 5000)
    assert max(len(ln["text"]) for ln in d["lines"]) <= 501


def test_claude_edit_multiedit_and_write_inputs():
    edit = diffs_from_claude_tool("Edit", {"file_path": "/r/a.py", "old_string": "x = 1", "new_string": "x = 2"})
    assert [(d["path"], d["kind"], d["additions"], d["deletions"]) for d in edit] == [("/r/a.py", "edit", 1, 1)]

    multi = diffs_from_claude_tool("MultiEdit", {"file_path": "/r/b.py", "edits": [
        {"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "c"},
        "garbage", {"old_string": "d", "new_string": "e\nf"},
    ]})
    assert [(d["additions"], d["deletions"]) for d in multi] == [(1, 1), (2, 1)]

    write = diffs_from_claude_tool("Write", {"file_path": "/r/c.md", "content": "hi\nthere\n"})
    # The prior content is unknown to the transcript: "write", never "create".
    assert [(d["kind"], d["additions"], d["deletions"]) for d in write] == [("write", 2, 0)]

    assert diffs_from_claude_tool("Bash", {"command": "ls"}) == []
    assert diffs_from_claude_tool("Edit", {"file_path": "/r/a.py"}) == []


def test_stats_ignore_write_diffs_whose_before_is_unknown():
    from app.services.chat_diff import stats_of

    edit = build_diff("/a", "x\n", "y\nz\n")
    write = build_diff("/b", None, "1\n2\n3\n", kind="write")
    assert stats_of([edit, write]) == {"additions": 2, "deletions": 1}
    assert stats_of([write]) is None
    assert stats_of([]) is None
