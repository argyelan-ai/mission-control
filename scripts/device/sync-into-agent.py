#!/usr/bin/env python3
"""Schreibt die Steuer-Skripte aus scripts/device/ als Text in
scripts/mc-node-agent.py (Block zwischen den CONTROL_FILES-Markern).

Warum: der Agent ist EINE Datei (siehe dessen Moduldoc) und muss die
Skripte selbst mitbringen. Die Dateien hier sind die lesbare Fassung;
backend/tests/test_node_agent_parsers.py prüft, dass beide identisch sind.
Nach jeder Änderung an einem Skript also:  python3 scripts/device/sync-into-agent.py
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT = HERE.parent / "mc-node-agent.py"
BEGIN = "# >>> CONTROL_FILES"
END = "# <<< CONTROL_FILES"

FILES = [
    # (Dateiname hier, Zielpfad-Ausdruck im Agenten, Rechte)
    ("mc-gpu-mode.sh", 'f"{CONTROL_SCRIPT_DIR}/mc-gpu-mode.sh"', "0o755"),
    ("latency-tune.sh", 'f"{CONTROL_SCRIPT_DIR}/latency-tune.sh"', "0o755"),
    ("mc-set-min-free.sh", 'f"{CONTROL_SCRIPT_DIR}/mc-set-min-free.sh"', "0o755"),
    ("mc-set-mtu.sh", 'f"{CONTROL_SCRIPT_DIR}/mc-set-mtu.sh"', "0o755"),
    ("gb10-clock-cap.service", "str(CLOCK_CAP_UNIT_PATH)", "0o644"),
]


def render_block() -> str:
    out = [
        f"{BEGIN} (erzeugt von scripts/device/sync-into-agent.py — NICHT von Hand ändern)",
        "CONTROL_FILES: dict[str, tuple[str, int, str]] = {",
        "    # Zielpfad -> (Dateiname unter scripts/device/, Rechte, Inhalt)",
    ]
    for name, target, mode in FILES:
        text = (HERE / name).read_text(encoding="utf-8")
        # Roh-String (r\"\"\"): Backslashes bleiben, aber drei Anführungszeichen
        # oder ein Backslash am Ende würden den Python-Text zerreissen.
        assert '"""' not in text and not text.endswith("\\"), name
        out.append(f'    {target}: ("{name}", {mode}, r"""{text}"""),')
    out.append("}")
    out.append(END)
    return "\n".join(out)


def main() -> None:
    src = AGENT.read_text(encoding="utf-8")
    start = src.index(BEGIN)
    end = src.index(END) + len(END)
    AGENT.write_text(src[:start] + render_block() + src[end:], encoding="utf-8")
    print(f"{len(FILES)} Dateien in {AGENT.name} eingebettet")


if __name__ == "__main__":
    main()
