#!/usr/bin/env python3
"""context_detect.py — harness-aware context%-Scraper (Python-Zwilling von
docker/mc-agent-base/lib/context-detect.sh).

CTX-01 Nachzug Teil 2 (2026-08-10): der Bash-Fix in poll.sh deckt nur die
containerisierten/Host-Agenten ab, die docker/shared/poll.sh fahren (Claude,
Kimi, openclaude/Shakespeare). Hermes (scripts/hermes-bridge.py), Grok
(scripts/grok-bridge.py) und omp/Sparky (docker/omp-bridge/bridge.py) haben
JEDER einen eigenen Python-Heartbeat-Loop, der bisher ueberhaupt keinen
Kontext-Prozentwert scrapt (nur `{"status": ...}` bzw. leerer Body) — nicht
falsches Muster, sondern GAR KEIN Scraping.

Dieses Modul spiegelt EXAKT dieselben Regex-Muster wie die Bash-Lib (siehe
dort fuer die vollstaendige Format-Dokumentation und die Live-Belege). Die
Gleichwertigkeit ist ueber backend/tests/test_context_detect_equivalence.py
bewiesen: dieselben Fixtures laufen durch BEIDE Implementierungen und muessen
identische Ergebnisse liefern — sonst laufen sie mit der Zeit auseinander.

Diese Datei ist wie die lib/*.sh-Dateien ein HAND-GEPFLEGTES Duplikat:
scripts/context_detect.py (fuer hermes-bridge.py + grok-bridge.py, die direkt
aus dem Checkout laufen) und docker/omp-bridge/context_detect.py (fuer die
Docker-Image-Build von omp-bridge, die nur einzelne Dateien kopiert, nicht
den ganzen scripts/-Ordner) MUESSEN byte-identisch bleiben — geprueft von
backend/tests/test_adapter_tck.py::test_lib_copies_byte_identical Analogon
in test_context_detect_equivalence.py.

scrape_context_pct(text, harness=None) -> Optional[int]
    Gibt den Kontext-Prozentwert 0-100 zurueck, oder None wenn kein Muster
    matcht ODER der Wert ausserhalb 0-100 liegt. NIE 0 raten wenn nur die
    Erkennung scheitert (z.B. `ctx --` = frisch gestartete Session ohne
    Wert) — None heisst fuer den Aufrufer "context_pct-Feld weglassen",
    genau wie bei der Bash-Variante.
"""
from __future__ import annotations

import re
from typing import Optional

_CLAUDE_RE = re.compile(r"ctx[: ]*([0-9]+)")
_KIMI_RE = re.compile(r"context: ([0-9]+)%")
_OPENCLAUDE_RE = re.compile(r"([0-9]+)(?:\.[0-9]+)?%/")
_HERMES_BAR_RE = re.compile(r"\]\s*([0-9]+)(?:\.[0-9]+)?%")
_FRACTION_RE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?[KkMm]?)/([0-9]+(?:\.[0-9]+)?[KkMm]?)"
)


def _resolve_suffix(value: str) -> float:
    """`21.3K` -> 21300.0, `262.1M` -> 262100000.0, `45` -> 45.0."""
    suffix = value[-1]
    if suffix in "Kk":
        return float(value[:-1]) * 1_000
    if suffix in "Mm":
        return float(value[:-1]) * 1_000_000
    return float(value)


def _ctx_claude(text: str) -> Optional[str]:
    """`ctx: NN` / `ctx NN` — letztes Vorkommen (mirrors bash `tail -1`)."""
    matches = _CLAUDE_RE.findall(text)
    return matches[-1] if matches else None


def _ctx_kimi(text: str) -> Optional[str]:
    """`context: NN%` (kimi-code Statuszeile)."""
    matches = _KIMI_RE.findall(text)
    return matches[-1] if matches else None


def _ctx_openclaude(text: str) -> Optional[str]:
    """Prozent direkt vor `/` (omp/Sparky: `◫ 8.3%/262K`) — erstes Vorkommen."""
    m = _OPENCLAUDE_RE.search(text)
    return m.group(1) if m else None


def _ctx_hermes_bar(text: str) -> Optional[str]:
    """Prozent direkt nach `]` (Hermes-Balken: `[█░░░░░░░░░] 8%`).

    `[░░░░░░░░░░] --` (kein Wert) matcht bewusst nicht — die Regex
    verlangt Ziffern vor dem `%`.
    """
    m = _HERMES_BAR_RE.search(text)
    return m.group(1) if m else None


def _ctx_fraction(text: str) -> Optional[str]:
    """Bruchform `USED/TOTAL` mit optionalem K/M-Suffix, Prozent BERECHNET."""
    m = _FRACTION_RE.search(text)
    if not m:
        return None
    used = _resolve_suffix(m.group(1))
    total = _resolve_suffix(m.group(2))
    if total <= 0:
        return None
    pct = used / total * 100
    return str(int(pct + 0.5))


def scrape_context_pct(text: Optional[str], harness: Optional[str] = None) -> Optional[int]:
    """Siehe Datei-Kopf. `harness` ist einer von "claude"/"kimi"/"openclaude"
    oder None (unbekannt/nicht gebacken) — dann wird sofort der volle
    Fallback ueber alle Muster probiert."""
    if not text:
        return None

    pct: Optional[str] = None
    if harness == "claude":
        pct = _ctx_claude(text)
    elif harness == "kimi":
        pct = _ctx_kimi(text)
    elif harness == "openclaude":
        pct = _ctx_openclaude(text)

    if pct is None:
        pct = _ctx_claude(text)
    if pct is None:
        pct = _ctx_kimi(text)
    if pct is None:
        pct = _ctx_openclaude(text)
    if pct is None:
        pct = _ctx_hermes_bar(text)
    if pct is None:
        pct = _ctx_fraction(text)

    if pct is None:
        return None
    try:
        val = int(pct)
    except ValueError:
        return None
    if val < 0 or val > 100:
        return None
    return val
