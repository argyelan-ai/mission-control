"""Entry point for `python3 -m mc_cli` / `mc`."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .client import Client
from .commands import REGISTRY
from .config import Config
from .errors import CLIError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mc", description="Mission Control agent CLI")
    p.add_argument("--version", action="version", version=f"mc {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    for spec in REGISTRY.values():
        child = sub.add_parser(spec.name, help=spec.help)
        spec.add_args(child)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = Config.from_env()
    # Wenn ein Status-Command die task-id als positional arg uebergibt
    # (`mc ack <id>`, `mc done <id>`, etc.), env-Quelle ueberschreiben.
    # Damit funktionieren beide Aufruf-Stile — env (poll.sh injection) UND
    # explicit (manuelle Aufrufe / Agents die die ID lieber als arg geben).
    task_id_override = getattr(args, "task_id", None)
    if task_id_override:
        cfg = cfg.with_task_id(task_id_override)
    client = Client(cfg)
    spec = REGISTRY[args.command]
    try:
        return spec.handler(args, client, cfg)
    except CLIError as e:
        msg = f"mc {args.command}: {e}"
        print(msg)           # stdout — agents see this in their terminal output
        print(msg, file=sys.stderr)  # stderr — keeps shell-script compat
        return e.exit_code
    except KeyboardInterrupt:
        msg = "mc: interrupted"
        print(msg)
        print(msg, file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
