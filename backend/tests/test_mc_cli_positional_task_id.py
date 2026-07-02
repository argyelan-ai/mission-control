"""Tests fuer Bug-Fix 2026-04-25: mc-CLI status-commands akzeptieren task-id
als optional positional arg.

Live-Bug Boss 2026-04-25: Boss versuchte `mc ack 9a6d898e-...` und
`mc ack 00dd72aa-...` (intuitives Bedien-Muster: subcommand <id>).
argparse warf: 'mc: error: unrecognized arguments: 9a6d898e-...'.
Boss verbrachte mehrere Minuten im Loop weil die CLI-error-message
unklar war (kein Hinweis dass task-id als env-var erwartet wird).

Fix: optional positional `task_id` arg fuer ack/done/review/blocked/failed.
Wenn uebergeben → cfg.task_id wird ueberschrieben (immutable replace).
Env-Quelle bleibt default (poll.sh injection funktioniert weiterhin).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MC_CLI_PATH = REPO_ROOT / "scripts" / "mc-cli"
if str(MC_CLI_PATH) not in sys.path:
    sys.path.insert(0, str(MC_CLI_PATH))


def _make_cfg(task_id=None, board_id=None):
    from mc_cli.config import Config
    return Config(
        api_url="http://test:8000",
        agent_token="test-token",
        task_id=task_id,
        board_id=board_id,
        dispatch_attempt_id=None,
    )


class TestParserAcceptsPositionalTaskId:
    """Smoke: argparse soll task-id als positional fuer status-commands annehmen
    (ohne 'unrecognized arguments' zu werfen)."""

    @pytest.mark.parametrize("subcmd", ["ack", "done", "review"])
    def test_simple_status_with_positional_task_id(self, subcmd):
        from mc_cli.__main__ import build_parser
        parser = build_parser()
        # Sollte nicht raisen — vor dem Fix: SystemExit(2) 'unrecognized arguments'
        args = parser.parse_args([subcmd, "9a6d898e-ad1f-425b-8a16-1e93f8a6aee2"])
        assert args.command == subcmd
        assert args.task_id == "9a6d898e-ad1f-425b-8a16-1e93f8a6aee2"

    @pytest.mark.parametrize("subcmd", ["ack", "done", "review"])
    def test_simple_status_without_task_id_still_works(self, subcmd):
        """Backward-compat: env-only Aufruf bleibt erhalten."""
        from mc_cli.__main__ import build_parser
        parser = build_parser()
        args = parser.parse_args([subcmd])
        assert args.command == subcmd
        assert args.task_id is None

    def test_blocked_with_positional_task_id_and_question(self):
        from mc_cli.__main__ import build_parser
        parser = build_parser()
        args = parser.parse_args([
            "blocked", "00dd72aa-53f4-4041-9cd0-46b4c3f4ef11",
            "--question", "Wie soll ich vorgehen?",
        ])
        assert args.task_id == "00dd72aa-53f4-4041-9cd0-46b4c3f4ef11"
        assert args.question == "Wie soll ich vorgehen?"

    def test_failed_with_positional_task_id_and_reason(self):
        from mc_cli.__main__ import build_parser
        parser = build_parser()
        args = parser.parse_args([
            "failed", "00dd72aa-53f4-4041-9cd0-46b4c3f4ef11",
            "--reason", "Build kaputt",
        ])
        assert args.task_id == "00dd72aa-53f4-4041-9cd0-46b4c3f4ef11"
        assert args.reason == "Build kaputt"


class TestConfigOverride:
    """Config.with_task_id() ueberschreibt task_id immutable."""

    def test_with_task_id_replaces_only_task_id(self):
        cfg = _make_cfg(task_id="old", board_id="board-a")
        new = cfg.with_task_id("new-id")
        assert new.task_id == "new-id"
        assert new.board_id == "board-a"  # unchanged
        assert new.api_url == "http://test:8000"  # unchanged
        assert cfg.task_id == "old"  # original immutable

    def test_with_task_id_none_env_then_override_provides_context(self):
        """Hauptfall: env hat KEINE task_id, positional uebergibt sie."""
        cfg = _make_cfg(task_id=None, board_id="board-a")
        new = cfg.with_task_id("provided-id")
        board, task = new.require_task_context()
        assert board == "board-a"
        assert task == "provided-id"


class TestEndToEndDispatch:
    """__main__.main wendet override an bevor handler aufgerufen wird."""

    def test_main_overrides_cfg_task_id_when_positional_present(self, monkeypatch):
        """Smoke: positional task-id im argv ueberschreibt env task-id."""
        import mc_cli.__main__ as main_mod
        from mc_cli.commands import CommandSpec, REGISTRY

        captured = {}

        def fake_handler(args, client, cfg):
            captured["task_id_in_cfg"] = cfg.task_id
            captured["task_id_in_args"] = getattr(args, "task_id", None)
            return 0

        # Replace the ack handler temporarily
        original = REGISTRY["ack"]
        REGISTRY["ack"] = CommandSpec(
            name="ack",
            help=original.help,
            endpoints=original.endpoints,
            scope=original.scope,
            handler=fake_handler,
            add_args=original.add_args,
        )

        # Force from_env to return a known cfg with env task_id "env-task"
        from mc_cli.config import Config
        original_from_env = Config.from_env
        monkeypatch.setattr(
            Config, "from_env",
            classmethod(lambda cls: Config(
                api_url="http://x", agent_token="t",
                task_id="env-task", board_id="env-board",
                dispatch_attempt_id=None,
            )),
        )

        try:
            rc = main_mod.main(["ack", "positional-task"])
        finally:
            REGISTRY["ack"] = original
            monkeypatch.setattr(Config, "from_env", original_from_env)

        assert rc == 0
        assert captured["task_id_in_args"] == "positional-task"
        assert captured["task_id_in_cfg"] == "positional-task", (
            f"main() sollte cfg.task_id mit positional arg ueberschreiben. "
            f"Got cfg.task_id={captured['task_id_in_cfg']}"
        )

    def test_main_keeps_env_task_id_when_no_positional(self, monkeypatch):
        """Backward-compat: ohne positional bleibt env-Quelle."""
        import mc_cli.__main__ as main_mod
        from mc_cli.commands import CommandSpec, REGISTRY

        captured = {}

        def fake_handler(args, client, cfg):
            captured["task_id_in_cfg"] = cfg.task_id
            return 0

        original = REGISTRY["ack"]
        REGISTRY["ack"] = CommandSpec(
            name="ack",
            help=original.help,
            endpoints=original.endpoints,
            scope=original.scope,
            handler=fake_handler,
            add_args=original.add_args,
        )

        from mc_cli.config import Config
        monkeypatch.setattr(
            Config, "from_env",
            classmethod(lambda cls: Config(
                api_url="http://x", agent_token="t",
                task_id="env-task", board_id="env-board",
                dispatch_attempt_id=None,
            )),
        )

        try:
            rc = main_mod.main(["ack"])
        finally:
            REGISTRY["ack"] = original

        assert rc == 0
        assert captured["task_id_in_cfg"] == "env-task"
