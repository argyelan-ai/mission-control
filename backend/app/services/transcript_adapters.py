"""Adapter-Register der Sessions-Chat-Ansicht — welcher Harness wird wie
gelesen, geparst und beobachtet.

Der Chat-Kern (SSE-Strom, Tailer, History-Seite, Frontend-Reducer) ist
generisch; pro CLI braucht er vier Bausteine (Session-Aufloesung, Parser,
Eingabe, Zustands-Sonde). Bis hierher war der Claude-Code-Adapter in
``transcript_chat.py`` die einzige Umsetzung und damit implizit fest
verdrahtet — an drei Stellen sogar so, dass ein Nicht-Claude-Agent still
falsche Antworten bekam:

  * ``transcript_chat.resolve_transcript_dir`` gab JEDEM ``cli-bridge``-Agenten
    das Claude-Verzeichnis. Sparky (omp) landete damit auf seinen ALTEN
    Claude-Transkripten aus der Zeit vor der omp-Umstellung — der Chat zeigte
    eine Sitzung, die es nicht mehr gibt.
  * ``pane_state.process_alive`` sucht ``pgrep -x claude``. Bei omp findet das
    nichts, rc=1 heisst „nachweislich weg" -> die Sitzung galt als ``ended``,
    obwohl die TUI lief.
  * ``pane_state.parse_pane_state`` kennt nur Claude-Glyphen; die omp-TUI fiel
    auf ``unknown``, und ``agent_chat_input`` musste sein Bereitschafts-Tor
    fuer fremde Harnesses ganz abschalten.

Dieses Modul macht die Auswahl explizit. ``adapter_for(agent)`` liefert immer
einen Adapter — der Claude-Adapter ist der Vorgabewert, damit jede bestehende
Aufrufstelle unveraendert weiterlaeuft. Ein neuer Harness kommt hinzu, indem
er ein Modul mit denselben Funktionsnamen liefert und hier eingetragen wird;
der Kern bleibt unberuehrt.

Die Importe der Adapter-Module passieren ABSICHTLICH erst in ``adapter_for``
und nicht auf Modulebene: ``transcript_chat`` importiert dieses Modul selbst
(fuer den Tailer), ein Modulebenen-Import wuerde also einen Ringschluss
erzeugen.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

#: Harness-Wert, der zum Claude-Code-Adapter fuehrt. Alles Unbekannte
#: (``None``, Alt-Datensaetze ohne Harness) faellt ebenfalls hierher — das war
#: das Verhalten vor diesem Register und bleibt es.
CLAUDE = "claude"
OMP = "omp"


@dataclass(frozen=True)
class TranscriptAdapter:
    """Die harness-spezifische Haelfte der Chat-Ansicht.

    Jedes Feld hat exakt die Signatur der Claude-Umsetzung in
    ``transcript_chat``/``pane_state`` — der Kern ruft sie auf, ohne zu
    wissen, welche CLI dahintersteht.
    """

    #: Harness-Schluessel, unter dem dieser Adapter registriert ist.
    name: str

    #: Agent -> Verzeichnis mit den Transkripten (``None`` = keins).
    resolve_transcript_dir: Callable[[Any], Path | None]

    #: Verzeichnis -> ``(pfad, meta)`` der aktiven Session (``None`` = keine).
    find_active_session: Callable[[Path], tuple[Path, dict[str, Any]] | None]

    #: Pfad EINER Session -> das Verzeichnis, ueber dem
    #: ``find_active_session`` fuer den Rollover-Scan laufen muss. Bei Claude
    #: Code ist das schlicht der Ordner der Datei; bei omp liegt die Session
    #: eine Ebene tief in einem pro-cwd-Ordner, der Scan muss also darueber
    #: laufen — sonst faende ein Rollover in ein ANDERES Arbeitsverzeichnis
    #: nie statt. Bewusst aus dem PFAD abgeleitet und nicht aus dem Agenten:
    #: so bleibt die Aufloesung an die tatsaechlich getailte Datei gebunden.
    session_scan_root: Callable[[Path], Path]

    #: Privacy-Tor, fail-closed: darf DIESER Agent DIESEN Pfad zeigen?
    transcript_allowed: Callable[[Any, Path], bool]

    #: Fabrik fuer einen Zeilen-Parser. Eine Fabrik statt einer Funktion,
    #: weil ein Adapter Zustand ueber die Zeilen einer Session hinweg
    #: brauchen kann (omp protokolliert die Effort-Stufe in einer EIGENEN
    #: Zeile, nicht am Zug). Jeder Lesevorgang holt sich eine frische
    #: Instanz; ein Session-Wechsel holt eine neue.
    #:
    #: Das optionale Argument ist der Pfad der Session, die gleich gelesen
    #: wird. Ein zustandsbehafteter Parser laedt daraus seinen Anfangszustand
    #: — noetig fuer den Live-Tailer, der am DATEIENDE einsteigt und die
    #: Zustands-Zeilen vom Session-Anfang sonst nie saehe.
    new_parser: Callable[..., Callable[[str, dict[str, int] | None], list[dict[str, Any]]]]

    #: Rohzeile -> stabile Eintrags-ID fuer die Dedup-Menge des Live-Pfades.
    peek_entry_id: Callable[[str], str | None]

    #: Verfeinert ein ``usage``-Ereignis mit der CLI-eigenen Kontext-
    #: Buchhaltung, wenn die CLI so etwas schreibt. Sonst wirkungslos.
    stamp_usage: Callable[[dict[str, Any], Path], None]

    #: Transkript-Pfad -> „der letzte Zug ist abgeschlossen".
    transcript_suggests_turn_ended: Callable[[Path], bool]

    #: Pane-Text + „Transkript waechst gerade" -> Zustands-Dikt.
    parse_pane_state: Callable[[str, bool], dict[str, Any]]

    #: Prozessname im Container fuer ``pane_state.process_alive``.
    process_name: str


def _claude_adapter() -> TranscriptAdapter:
    from app.services import pane_state, transcript_chat

    return TranscriptAdapter(
        name=CLAUDE,
        resolve_transcript_dir=transcript_chat.resolve_transcript_dir,
        find_active_session=transcript_chat.find_active_session,
        session_scan_root=lambda session_path: session_path.parent,
        transcript_allowed=transcript_chat.transcript_allowed,
        # Claude Codes Parser ist zustandslos — die Fabrik gibt schlicht ihn
        # selbst zurueck; der Pfad interessiert ihn nicht.
        new_parser=lambda session_path=None: transcript_chat.parse_transcript_line,
        peek_entry_id=transcript_chat._peek_uuid,
        stamp_usage=lambda ev, session_path: transcript_chat._stamp_usage_source(
            ev, transcript_chat._claude_config_root(session_path), session_path.stem
        ),
        transcript_suggests_turn_ended=(
            transcript_chat.ChatTailerManager._transcript_suggests_turn_ended
        ),
        parse_pane_state=pane_state.parse_pane_state,
        process_name="claude",
    )


def _omp_adapter() -> TranscriptAdapter:
    from app.services import omp_chat

    return TranscriptAdapter(
        name=OMP,
        resolve_transcript_dir=omp_chat.resolve_transcript_dir,
        find_active_session=omp_chat.find_active_session,
        session_scan_root=omp_chat.session_scan_root,
        transcript_allowed=omp_chat.transcript_allowed,
        new_parser=omp_chat.new_parser,
        peek_entry_id=omp_chat.peek_entry_id,
        stamp_usage=omp_chat.stamp_usage,
        transcript_suggests_turn_ended=omp_chat.transcript_suggests_turn_ended,
        parse_pane_state=omp_chat.parse_pane_state,
        process_name=omp_chat.PROCESS_NAME,
    )


_BUILDERS: dict[str, Callable[[], TranscriptAdapter]] = {
    CLAUDE: _claude_adapter,
    OMP: _omp_adapter,
}


def adapter_for(agent: Any | None) -> TranscriptAdapter:
    """Der Adapter fuer diesen Agenten — nie ``None``.

    Ein unbekannter oder fehlender Harness bekommt den Claude-Adapter. Das
    ist bewusst KEIN Privacy-Loch: der Claude-Adapter entscheidet danach
    selbst (``resolve_transcript_dir`` / ``transcript_allowed``), ob dieser
    Agent ueberhaupt ein Transkript hat. Ein fremder Harness ohne eigenen
    Adapter landet damit im selben „nichts zu zeigen"-Zustand wie vorher —
    Kimi ist heute genau dieser Fall.
    """
    harness = getattr(agent, "harness", None)
    builder = _BUILDERS.get(harness or CLAUDE, _claude_adapter)
    return builder()
