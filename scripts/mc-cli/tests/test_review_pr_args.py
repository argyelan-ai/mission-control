"""Task 27ab2ef9: `--pr`/`--pr-url` on `mc review` and `mc patch --status review`.

Registry-Repo-Karten (task.repo_id) muessen die PR-Nummer beim Uebergang auf
review mitschicken — das Backend legt dort keinen PR automatisch an und lehnt
den Review-Handoff sonst mit 400 ab. Die CLI-Verben sind der user-facing half:
diese Tests pruefen die echte argparse-Oberflaeche (build_parser) und den
PATCH-Body, den die Verben senden — nicht nur den Backend-Route-Handler
(siehe backend/tests/test_review_pr_number_gate.py).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli import commands  # noqa: E402
from mc_cli.errors import UsageError  # noqa: E402

import pytest  # noqa: E402


CALLS: list = []


class _Client:
    def request(self, method, path, body=None, **kw):
        CALLS.append((method, path, body))
        return {"id": "t1", "status": (body or {}).get("status")}


def _patch_bodies():
    return [b for m, _, b in CALLS if m == "PATCH"]


@pytest.fixture(autouse=True)
def _reset():
    CALLS.clear()


def test_review_registered_with_pr_args():
    spec = commands.REGISTRY["review"]
    assert "PATCH /boards/{board_id}/tasks/{task_id}" in spec.endpoints
    from mc_cli.__main__ import build_parser
    ns = build_parser().parse_args(["review", "--pr", "631", "--pr-url", "https://example.invalid/pull/631"])
    assert ns.command == "review"
    assert ns.pr == 631
    assert ns.pr_url == "https://example.invalid/pull/631"


def test_review_defaults_to_pr_none():
    from mc_cli.__main__ import build_parser
    ns = build_parser().parse_args(["review"])
    assert ns.pr is None and ns.pr_url is None


def test_review_sends_pr_number_and_url():
    rc = commands._cmd_review(_ns(pr=631, pr_url="https://example.invalid/pull/631"), _Client(), _cfg())
    assert rc == 0
    body = _patch_bodies()[-1]
    assert body == {"status": "review", "pr_number": 631, "pr_url": "https://example.invalid/pull/631"}


def test_review_without_pr_sends_bare_status_body():
    rc = commands._cmd_review(_ns(), _Client(), _cfg())
    assert rc == 0
    assert _patch_bodies()[-1] == {"status": "review"}


def test_patch_registered_with_pr_args():
    from mc_cli.__main__ import build_parser
    ns = build_parser().parse_args(["patch", "--status", "review", "--pr", "631"])
    assert ns.status == "review" and ns.pr == 631 and ns.pr_url is None


def test_patch_status_review_sends_pr_number():
    rc = commands._cmd_patch(_ns(status="review", pr=631, pr_url=None), _Client(), _cfg())
    assert rc == 0
    assert _patch_bodies()[-1] == {"status": "review", "pr_number": 631}


def test_patch_pr_outside_review_rejected():
    with pytest.raises(UsageError, match="--status review"):
        commands._cmd_patch(_ns(status="waiting", pr=5, pr_url=None), _Client(), _cfg())
    assert _patch_bodies() == []  # nichts rausgeschickt


# ── helpers ────────────────────────────────────────────────────────────────

def _ns(**kw):
    """argparse-Namespace-Stub mit den Defaults aus _add_pr_args."""

    class _Args:
        pr = None
        pr_url = None

        def __init__(self, **kw2):
            self.__dict__.update(kw2)

    return _Args(**kw)


def _cfg():
    from unittest.mock import MagicMock
    cfg = MagicMock()
    cfg.require_task_context.return_value = ("b1", "t1")
    return cfg
