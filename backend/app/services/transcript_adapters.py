"""Adapter-Register der Sessions-Chat-Ansicht — welcher Harness wird wie
gelesen, geparst und beobachtet.

Der Chat-Kern (SSE-Strom, Tailer, History-Seite, Frontend-Reducer) ist
generisch; pro CLI braucht er vier Bausteine (Session-Aufloesung, Parser,
Eingabe, Zustands-Sonde). Bis hierher war der Claude-Code-Adapter in
``transcript_chat.py`` die einzige Umsetzung und damit implizit fest
verdrahtet — an drei Stellen sogar so, dass ein Nicht-Claude-Agent still
falsche Antworten bekam:

  * ``transcript_chat.resolve_transcript_dir`` gab JEDEM ``cli-bridge``-Agenten
    das Claude-Verzeichnis. der omp-Agent (omp) landete damit auf seinen ALTEN
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
OPENCLAUDE = "openclaude"
OMP = "omp"
#: Host-Harness, dessen ACP-Chat-Daemon dasselbe omp-Transkriptformat
#: schreibt (docs/specs/chat-over-acp.md) — er teilt sich daher den
#: omp-Adapter, statt ein zweites Format zu lernen.
HERMES = "hermes"


def _no_subagent_runs(_session_path: Path) -> list[dict[str, Any]]:
    """Der Standard fuer jeden Harness ohne eigenes Subagenten-Layout."""
    return []


def _no_session_cwd(_session_path: Path) -> str | None:
    """The default for a harness that records no working directory."""
    return None


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

    #: Sitzungspfad -> die Subagenten-Laeufe dieser Sitzung (Steckbriefe).
    #:
    #: MIT Standard, und der Standard ist die leere Liste: Subagenten-Dateien
    #: schreibt nur Claude Code und sein Fork openclaude. omp hat nachweislich
    #: keine Sidechains (``omp_chat``: ``sidechain=False`` an jeder Stelle),
    #: kimi hat gar keinen eigenen Adapter. Ein Pflichtfeld zwaenge beide zu
    #: einer Attrappe — der leere Standard sagt dasselbe ehrlicher. Muss als
    #: letztes Feld stehen: Python verlangt Felder mit Standard hinter allen
    #: ohne.
    subagent_runs: Callable[[Path], list[dict[str, Any]]] = _no_subagent_runs

    #: Terminal-Zeile, die eine FRISCHE Sitzung ohne Datei anzeigt (omp:
    #: ``New session started``). omp legt bei ``/new`` keine Datei an, die
    #: alte bleibt die neueste auf der Platte — nur das Terminal weiss vom
    #: Wechsel. Der Tailer zaehlt den Marker im Pane; ein ZUWACHS setzt
    #: ``fresh_session.mark``. ``None`` = die CLI schreibt den Wechsel selbst
    #: ins Transkript (Claude Code: ``/clear`` eroeffnet eine neue Datei).
    fresh_session_pane_marker: str | None = None

    #: Pfad der AKTIVEN Session -> die Vorschau-Kanal-Datei dieses Harness
    #: (``None`` = der Harness hat keinen eigenen Vorschau-Kanal).
    #:
    #: Der omp-bridge ACP-Treiber schreibt seine fluechtigen Vorschau-
    #: Snapshots (Folge-PR zu #471) in eine Schwesterdatei der Session —
    #: NICHT in die Transkript-JSONL. Der Tailer liest sie ueber dieses
    #: Feld und broadcastet jede Zeile als volatile ``preview``-Ereignis.
    #: Claude Code schreibt Previews nicht auf die Platte (dort lebt der
    #: Kanal im Pane-Strom), darum der ``None``-Standard — echtes ``None``,
    #: keine Attrappen-Lambda: der Tailer gated per ``is not None`` (Review
    #: #473 N3), eine Lambda waere immer „nicht None" und der `to_thread`-Hop
    #: liefe bei jedem Takt fuer Adapter ohne eigenen Kanal ins Leere.
    preview_channel: Callable[[Path], Path | None] | None = None

    #: Session file -> the working directory the CLI recorded for it (its own
    #: path, a container path for Docker agents), or ``None`` when the
    #: harness records none.
    #:
    #: The chat's diff panel needs it: what "just happened in the chat"
    #: happens where the SESSION works, not in the last task's workspace —
    #: operator finding 04.10.2026, the panel showed a months-old commit from
    #: a finished task while the chat had just committed elsewhere. Default
    #: ``None`` so a harness without this knowledge simply keeps the older
    #: fallbacks (running task, most recently used repo).
    session_cwd: Callable[[Path], str | None] = _no_session_cwd

    #: Session-Datei -> die stabile ID der LOGISCHEN Sitzung, die sie
    #: traegt — fuer den Rollover-Vergleich im Tailer (``transcript_chat.
    #: ChatTailerManager``'s ``_is_genuine_rollover``), der sonst JEDEN
    #: Pfadwechsel als neue Sitzung behandelt.
    #:
    #: Vorgabe: der volle Datei-Stamm (deckt sich mit Claude Code, wo der
    #: Stamm selbst die Sitzungs-UUID ist — ``_claude_adapter`` setzt dieses
    #: Feld deshalb nie explizit, der Vorgabewert ist dort bereits exakt die
    #: alte Pfadgleichheits-Pruefung).
    #:
    #: omp (``_omp_adapter``) ueberschreibt das: unter ``OMP_DRIVER=acp``
    #: schreiben ZWEI unabhaengige Prozesse — die native omp-CLI und der
    #: ACP-Bridge-Sink (``docker/omp-bridge/acp_chat_events.ChatEventSink``,
    #: dessen eigener Docstring sagt „the same shape omp writes" — bewusst
    #: dasselbe Schema) — je eine eigene Datei fuer DIESELBE Sitzung in
    #: denselben Ordner; beide tragen dieselbe Sitzungs-UUID im Dateinamen,
    #: nur mit unterschiedlich genauem Zeitstempel-Praefix. Beide wachsen
    #: ueber den ganzen Zug hinweg unabhaengig weiter, sodass „die neueste
    #: Datei" im Sekundentakt zwischen ihnen hin- und herspringen kann, OHNE
    #: dass sich die Sitzung je aendert — Operator-Befund 04.10.2026: der
    #: Kontext-Ring im Composer blinkte waehrend eines laufenden Zugs mehrfach
    #: weg und kam zurueck, weil jeder Sprung einen echten (aber falschen)
    #: ``session_changed`` auf dem SSE-Strom ausloeste (der den Verlauf im
    #: Frontend-Reducer absichtlich leert — richtig bei einem ECHTEN Rollover
    #: wie ``/clear``, falsch hier). Mit diesem Feld vergleicht der Tailer die
    #: eingebettete UUID statt des Pfades: dieselbe UUID in einer
    #: Geschwisterdatei ist keine neue Sitzung.
    session_id_for: Callable[[Path], str] = lambda path: path.stem

    #: Glob (relative to a head run folder, forward slashes) that finds this
    #: harness's TOP-LEVEL transcript file(s) among `scripts/head/mc-head`'s
    #: run layout (docs/specs/head-launcher.md §6.1) — ``None`` for a harness
    #: without a head reader yet.
    #:
    #: Deliberately ONE level shallower than the token harvester's own glob
    #: (``token_harvester._HEAD_TRANSCRIPT_GLOBS``, which also wants
    #: subagent files for usage accounting): Claude Code's subagent
    #: transcripts live two levels under ``claude-config/projects/<dir>/``
    #: (``<session>/subagents/agent-*.jsonl``), so ``projects/*/*.jsonl``
    #: already excludes them without special-casing the name. The head chat
    #: view shows exactly the one conversation the operator started — never
    #: a subagent's.
    head_transcript_glob: str | None = None


def _claude_adapter(
    name: str = CLAUDE, process_name: str = "claude"
) -> TranscriptAdapter:
    """Der Claude-Code-Adapter — und zugleich der von openclaude.

    openclaude ist ein Fork von Claude Code: Transkript-Format, Pane-Marker
    und Bereitschafts-Erkennung sind nachweislich dieselben (am echten Pane
    gegengeprueft, nur gelesen). Verschieden ist einzig, wie der Prozess im
    Container HEISST — ``pgrep -x`` vergleicht exakt, und eine Sitzung, die
    unter ``openclaude`` laeuft, waere unter ``claude`` gesucht "beweisbar
    beendet". Darum eine Fabrik mit zwei Registrierungen statt einer Kopie:
    faellt am Format je etwas auseinander, ist hier die Stelle dafuer.
    """
    from app.services import pane_state, transcript_chat

    return TranscriptAdapter(
        name=name,
        resolve_transcript_dir=transcript_chat.resolve_transcript_dir,
        find_active_session=transcript_chat.find_active_session,
        session_scan_root=lambda session_path: session_path.parent,
        transcript_allowed=transcript_chat.transcript_allowed,
        session_cwd=transcript_chat.session_cwd,
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
        process_name=process_name,
        subagent_runs=transcript_chat.subagent_runs,
        head_transcript_glob="claude-config/projects/*/*.jsonl",
    )


def _omp_adapter() -> TranscriptAdapter:
    from app.services import omp_chat

    return TranscriptAdapter(
        name=OMP,
        resolve_transcript_dir=omp_chat.resolve_transcript_dir,
        find_active_session=omp_chat.find_active_session,
        fresh_session_pane_marker=omp_chat.FRESH_SESSION_MARKER,
        preview_channel=omp_chat.preview_channel,
        session_scan_root=omp_chat.session_scan_root,
        transcript_allowed=omp_chat.transcript_allowed,
        session_cwd=omp_chat.session_cwd,
        new_parser=omp_chat.new_parser,
        peek_entry_id=omp_chat.peek_entry_id,
        stamp_usage=omp_chat.stamp_usage,
        transcript_suggests_turn_ended=omp_chat.transcript_suggests_turn_ended,
        parse_pane_state=omp_chat.parse_pane_state,
        process_name=omp_chat.PROCESS_NAME,
        session_id_for=omp_chat.session_id_for,
        head_transcript_glob="omp-sessions/*.jsonl",
    )


_BUILDERS: dict[str, Callable[[], TranscriptAdapter]] = {
    CLAUDE: _claude_adapter,
    OPENCLAUDE: lambda: _claude_adapter(OPENCLAUDE, process_name=OPENCLAUDE),
    OMP: _omp_adapter,
    # Hermes liest mit dem omp-Adapter: sein Chat-Daemon schreibt dieselben
    # Transkripte in dieselbe Ordnerform (``omp_chat.resolve_transcript_dir``
    # laesst host+hermes ausdruecklich zu). Ohne diesen Eintrag waere jener
    # Zweig toter Code — der History-Endpunkt fragt immer ueber
    # ``adapter_for``, und ein unbekannter Harness bekommt den Claude-Adapter.
    HERMES: _omp_adapter,
}

#: Harnesses mit eigener Pane-Sonde — und damit die, bei denen ein
#: Bereitschafts-Tor vor dem Senden ueberhaupt eine Aussage treffen kann.
#:
#: Abgeleitet aus der Registrierung, nicht von Hand gepflegt: wer einen
#: Adapter ergaenzt, hat damit eine Sonde, und das Tor gilt sofort. Eine
#: zweite, handgefuehrte Liste war genau der Fehler, den diese Zusammen-
#: fuehrung aufgeraeumt hat — sie waere beim naechsten Harness still
#: auseinandergelaufen, und ein Agent haette Nachrichten ungeprueft in eine
#: womoeglich noch bootende TUI bekommen, ohne dass ein Test rot wird.
PANE_PROBED_HARNESSES = frozenset(_BUILDERS)


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


def adapter_for_harness(name: str | None) -> TranscriptAdapter | None:
    """The adapter for a head's ``spec["harness"]`` — or ``None``.

    ``adapter_for`` is duck-typed on an object's ``.harness`` attribute and
    falls back to the Claude adapter for anything unknown: right for an
    agent (an unknown/missing harness still has a Claude Code process
    behind it), wrong for a head. A head's harness is an exact string from
    ``spec.json`` (docs/specs/head-launcher.md §6.1) — passing that string
    itself to ``adapter_for`` would read its OWN ``.harness`` *attribute*
    (strings have none), silently resolve to the Claude builder, and read a
    head that never ran Claude Code through the Claude parser. That is
    exactly the trap `anhang.md` section B calls out (0 events, no error).

    So this function takes the plain string directly and is strict in the
    other direction instead: a harness with no registered adapter, or whose
    adapter has no ``head_transcript_glob`` (no head reader built for it
    yet, e.g. a future harness that is chat-adapter-only), gets ``None`` —
    never a silent Claude fallback. Callers (``services/heads/transcript.py``)
    turn that into ``reason="no_reader"``.
    """
    if not isinstance(name, str) or not name:
        return None
    builder = _BUILDERS.get(name)
    if builder is None:
        return None
    adapter = builder()
    return adapter if adapter.head_transcript_glob else None
