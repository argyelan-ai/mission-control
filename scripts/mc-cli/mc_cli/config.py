"""Env-based config for `mc` CLI.

Poll.sh injects these on every dispatch. Missing task/board vars are only a
hard-fail for commands that need them — `mc --version` etc. must still work.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# W5 (2026-09-13): Kontextdatei pro Host-Agent statt geteilter /tmp-Datei.
# MC_CONTEXT_ENV_PATH nennt den Pfad; ohne die Variable gilt der Legacy-Pfad,
# damit laufende Zuege (Container, nicht-umgestellte Bridges) weiterarbeiten.
DEFAULT_CONTEXT_ENV_PATH = "/tmp/mc-context.env"


def context_env_path() -> str:
    """Resolve the task-context env file: MC_CONTEXT_ENV_PATH wins, the legacy
    /tmp path is the fallback. Single source of truth for readers
    (Config.from_env) AND writers (commands._write_context_file, recover).

    Also the test-isolation seam (2026-09-14 incident, #579): tests/conftest.py
    redirects MC_CONTEXT_ENV_PATH to a per-test tmp_path so a suite run never
    touches the real, host-shared /tmp/mc-context.env other agents rely on.
    """
    return os.environ.get("MC_CONTEXT_ENV_PATH") or DEFAULT_CONTEXT_ENV_PATH


@dataclass(frozen=True)
class Config:
    api_url: str
    agent_token: str
    task_id: str | None
    board_id: str | None
    dispatch_attempt_id: str | None
    context_task_id: str | None = None

    def __post_init__(self) -> None:
        # Attempt-Header und Task-Kontext gehoeren zusammen: der Header in
        # dispatch_attempt_id wurde vom Dispatch der Karte context_task_id
        # ausgestellt. Ohne explizite Angabe ist das die aktuelle task_id.
        if self.context_task_id is None:
            object.__setattr__(self, "context_task_id", self.task_id)

    @classmethod
    def from_env(cls) -> "Config":
        # Fallback: read the task-context env file (MC_CONTEXT_ENV_PATH, legacy
        # /tmp/mc-context.env) when the task context env vars are missing from
        # the process environment. This covers the case where claude's Bash
        # tool spawns a fresh shell whose env was set by tmux set-environment
        # but hasn't propagated yet. poll.sh writes the file on every dispatch.
        file_ctx: dict[str, str] = {}
        ctx_path = context_env_path()
        if os.path.isfile(ctx_path):
            try:
                with open(ctx_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or "=" not in line or line.startswith("#"):
                            continue
                        k, _, v = line.partition("=")
                        file_ctx[k.strip()] = v.strip()
            except OSError:
                pass

        def _resolve(key: str) -> str | None:
            # File wins over env: poll.sh schreibt /tmp/mc-context.env bei
            # JEDEM neuen Dispatch frisch — die Prozess-Env behält dagegen
            # alte TASK_ID/BOARD_ID aus dem vorhergehenden Dispatch im
            # selben tmux-Window. Davinci self-reflection 2026-05-10
            # cf319ff1: stale env hat 404/400 ausgelöst, mc CLI-Recovery
            # erst über X-Dispatch-Attempt-Id Hint möglich.
            return file_ctx.get(key) or os.environ.get(key) or None

        return cls(
            api_url=os.environ.get("MC_API_URL", "http://localhost:8000").rstrip("/"),
            agent_token=os.environ.get("MC_AGENT_TOKEN", ""),
            task_id=_resolve("TASK_ID"),
            board_id=_resolve("BOARD_ID"),
            dispatch_attempt_id=_resolve("X_DISPATCH_ATTEMPT_ID"),
        )

    def require_task_context(self) -> tuple[str, str]:
        """Return (board_id, task_id) or raise with clear message."""
        if not self.board_id or not self.task_id:
            from .errors import UsageError
            raise UsageError(
                "TASK_ID und BOARD_ID müssen in der Env gesetzt sein. "
                "poll.sh injiziert die bei jedem Dispatch — wenn du die CLI "
                "manuell aufrufst, setze sie explizit (oder gib die task-id "
                "bei status-commands wie `mc ack <task-id>` als Argument mit)."
            )
        return self.board_id, self.task_id

    def with_task_id(self, task_id: str) -> "Config":
        """Return a copy with task_id overridden — used when status commands
        accept the task-id as a positional argument (Boss live-bug 2026-04-25:
        `mc ack <task-id>` warf 'unrecognized arguments' weil das CLI nur
        env-vars unterstuetzte). Immutable dataclass → replace pattern.

        context_task_id bleibt unangetastet: es bezeichnet die Karte, zu der
        der aktuell GEHALTENE dispatch_attempt_id-Header gehoert. Erst ein
        erfolgreicher Kontextwechsel (ack/recover) schreibt beides zusammen
        fort — genau die Kopplung, mit der _cmd_ack/_cmd_recover zwischen
        "fremde Karte heilen" und "eigene Karte neu dispatcht" trennen.
        """
        from dataclasses import replace
        return replace(self, task_id=task_id)

    def require_token(self) -> str:
        if not self.agent_token:
            from .errors import UsageError
            raise UsageError("MC_AGENT_TOKEN fehlt in der Env.")
        return self.agent_token
