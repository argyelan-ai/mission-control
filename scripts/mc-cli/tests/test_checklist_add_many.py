"""`mc checklist add "A" "B"` — several items in one call (live finding
28.10.2026: the agent's multi-item call died with a usage error). One title
keeps working exactly as before."""
import os
import sys
from unittest.mock import MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli import commands  # noqa: E402
from mc_cli.__main__ import build_parser  # noqa: E402

BOARD_ID = "11111111-1111-1111-1111-111111111111"
TASK_ID = "22222222-2222-2222-2222-222222222222"


def _run(argv):
    args = build_parser().parse_args(argv)
    cfg = MagicMock()
    cfg.require_task_context.return_value = (BOARD_ID, TASK_ID)
    client = MagicMock()
    client.request.return_value = []
    commands._cmd_checklist(args, client, cfg)
    return client


def test_several_titles_go_in_one_bulk_request_in_order():
    client = _run(["checklist", "add", "Plan", "Build", "Check"])
    assert client.request.call_count == 1
    method, path = client.request.call_args.args
    assert method == "POST" and path.endswith(f"/tasks/{TASK_ID}/checklist")
    items = client.request.call_args.kwargs["body"]["items"]
    assert [i["title"] for i in items] == ["Plan", "Build", "Check"]
    assert [i["sort_order"] for i in items] == [0, 1, 2]


def test_one_title_is_unchanged():
    client = _run(["checklist", "add", "Only step", "--order", "7"])
    assert client.request.call_args.kwargs["body"] == {"items": [{"title": "Only step", "sort_order": 7}]}


def test_order_offsets_every_item():
    client = _run(["checklist", "add", "A", "B", "--order", "5"])
    items = client.request.call_args.kwargs["body"]["items"]
    assert [i["sort_order"] for i in items] == [5, 6]
