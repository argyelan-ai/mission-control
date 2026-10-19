"""omp-Adapter fuer die Sessions-Chat-Ansicht — Transkript-Aufloesung, Zeilen-
Parser und Pane-Sonde fuer den Harness ``omp`` (Agent der omp-Agent).

Gegenstueck zu ``transcript_chat.py`` (Claude Code) und ueber
``transcript_adapters.adapter_for`` angebunden. Der ChatEvent-Vertrag
(``frontend-v2/src/lib/chatTypes.ts``) bleibt unveraendert — dieser Adapter
uebersetzt nur das omp-Format in dieselben normalisierten Ereignisse.

Phase-0-Discovery (live erhoben am 19.08.2026 gegen der omp-Agent, omp im Container
``mc-agent-<slug>``, Modell ``mc-openai/qwen38-27b-unsloth-nvfp4``):

ABLAGE — omp schreibt JSONL live, eine Datei je Session:
    <OMP_HOME>/profiles/<profil>/agent/sessions/<kodiertes-cwd>/<ts>_<uuid>.jsonl
  Im Container ist das ``/home/agent/.omp/profiles/mc-agent/agent/sessions``;
  dieses Verzeichnis ist per Bind-Mount auf dem Host als
  ``~/.mc/agents/<slug>/omp-sessions`` sichtbar (``docker inspect
  mc-agent-<slug>``), und genau von dort liest das Backend. Wichtiger
  Unterschied zu Claude Code: die Sessions liegen NICHT flach im Verzeichnis,
  sondern eine Ebene tiefer in einem pro-cwd-Unterordner (z. B.
  ``--workspace--``) — ``find_active_session`` sucht deshalb eine Ebene tief.

FORMAT — eigenes, VERSIONIERTES Format (``{"type":"session","version":3}``).
  Zeilentypen (Erhebung ueber alle 18 vorhandenen Transkripte, 1050 Zeilen,
  0 unparsebar): ``title``, ``session``, ``model_change``,
  ``thinking_level_change``, ``service_tier_change``, ``title_change``,
  ``message``, ``custom``, ``custom_message``.

  * Die STABILE Eintrags-ID ist das Top-Level-Feld ``id`` (8 Hex-Zeichen),
    nicht ``message.id`` (das gibt es nicht) — sie ist der Dedup-Schluessel.
    ``parentId`` verkettet die Eintraege zu einer Liste.
  * ``message.role`` kennt VIER Werte: ``user``, ``assistant``,
    ``toolResult`` und ``fileMention``. ``toolResult`` ist eine eigene Rolle
    (bei Claude Code ist es ein Block INNERHALB eines user-Eintrags).
  * ``message.content`` ist in ALLEN 657 beobachteten Zeilen eine Liste von
    Bloecken — die String-Form, die bei Claude Code echte getippte Turns
    schreibt, existiert bei omp nicht. Trotzdem defensiv behandelt (Formate
    aendern sich ohne Ankuendigung).
  * Assistant-Bloecke: ``thinking`` (Feld ``thinking``), ``text``,
    ``toolCall`` (``id``/``name``/``arguments``). Ein Zug kann MEHRERE
    ``toolCall``-Bloecke tragen (live gesehen: zwei parallele Aufrufe mit
    ``streamIndex`` 0/1).
  * ``toolResult`` traegt ``toolCallId`` (Verknuepfung zum ``toolCall.id``),
    ``toolName``, ``isError`` und eine Block-Liste, die auch ``image``-
    Bloecke enthalten kann.
  * Usage steht in ``message.usage`` mit ``input``/``output``/``cacheRead``/
    ``cacheWrite``/``totalTokens`` und ``cost`` (Kosten sind im
    ChatEvent-Schema nicht vorgesehen und werden bewusst weggelassen).
    WICHTIG: ``input`` ist SITZUNGSKUMULATIV, nicht der Fuellstand — jeder
    API-Aufruf ohne Prompt-Cache re-processiert das ganze Gespraech, und
    omp summiert das auf (live 14.09.2026: input=11.795.309 bei einem
    500k-Fenster). Der echten Fuellstand schreibt omp seit spaeterem Build
    separat als ``usedTokens`` nebst ``contextWindow``; aeltere Builds
    haben die beiden Felder nicht.
  * Die Effort-Stufe steht NICHT am Zug, sondern in eigenen
    ``thinking_level_change``-Zeilen (``thinkingLevel``). Der Parser ist
    deshalb ZUSTANDSBEHAFTET (``OmpLineParser``) und traegt die zuletzt
    gesehene Stufe an die folgenden ``usage``-Ereignisse — ein reiner
    Pro-Zeile-Parser koennte das nicht.

WAS OMP NICHT HAT: Subagenten/Sidechains (``sidechain`` ist immer ``False``),
  In-Session-Slash-Kommandos im Transkript (kein ``command``-Ereignis) und
  keine Statuszeilen-Datei wie Claude Code (die Prozentangabe im Pane wird
  bewusst nicht gescraped) — ABER seit omp ``usedTokens``/``contextWindow``
  am Zug schreibt, stempelt der Parser daraus ``usedPct`` mit
  ``source: "cli"``: die Zahl kommt direkt vom CLI, auch ohne Statuszeile.

PANE — omp Fenster 0 ist die ECHTE, interaktive TUI (ADR-049 loest den
  headless ``omp -p``-Betrieb von ADR-045 ab; live bestaetigt: ``ps`` zeigt
  ``omp --hook … --cwd /workspace`` in tmux ``omp-agent:0``). Zwei Formen
  reichen zur Klassifikation:
    Arbeitszeile:  `` ⠴ Working… ⟦esc⟧`` bzw. `` ⠇ Reading hostname file ⟦esc⟧``
                   — der Text WECHSELT, stabil ist die Abbruch-Marke ``⟦esc⟧``.
    Eingabefeld:   ``╭── π  > … ▶───╮`` / ``╰─   …   ─╯`` — die untere
                   Rahmenzeile ist das Eingabefeld selbst (leer = bereit,
                   Text darin = getippter, noch nicht abgeschickter Entwurf).
                   Dieselbe Regel benutzt ``docker/omp-bridge/bridge.py``
                   (``_composer_state``) seit 2026-07-12 in Produktion.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from app.services.transcript_chat import (
    _LIVE_WINDOW_SECONDS,
    _truncate_detail,
    _truncate_title,
    resolve_context_window,
)
from app.services.token_harvester import _host_home
from app.services.chat_diff import build_diff, diffs_from_acp
from app.services.chat_todos import (
    ACP_PLAN_CUSTOM_TYPE,
    todo_event_from_acp_plan,
    todo_event_from_tool,
)

logger = logging.getLogger("mc.omp_chat")

#: Der Harness-Wert in ``agents.harness``, fuer den dieser Adapter zustaendig ist.
HARNESS = "omp"

#: Prozessname der CLI im Container — fuer ``pane_state.process_alive``.
PROCESS_NAME = "omp"

#: Unterverzeichnis unter ``~/.mc/agents/<slug>/``, auf das der Container sein
#: ``…/agent/sessions`` mountet (siehe Modul-Docstring).
_SESSIONS_DIRNAME = "omp-sessions"

#: Was omp nach ``/new`` ins Terminal schreibt (live 01.09.2026: ``✔ New
#: session started``). Ohne das Haekchen verglichen — Symbol und Farbe sind
#: Kosmetik der TUI, der Text ist die Aussage.
FRESH_SESSION_MARKER = "New session started"

#: Wie tief unter dem Sessions-Wurzelverzeichnis eine Session liegen darf.
#: 1 = die pro-cwd-Ordner. Tiefer liegen nur Anhaenge/Blobs, keine Sessions.
_SESSION_GLOBS = ("*.jsonl", "*/*.jsonl")

#: Unterverzeichnis im Sessions-Wurzelverzeichnis, in dem der omp-bridge ACP-
#: Treiber die FLUECHTIGEN Vorschau-Snapshots ablegt (Folge-PR zu #471):
#: ``<sessions-wurzel>/<kodiertes-cwd>/previews/<ts>_<sessionId>.jsonl``. Es
#: liegt TIEFER als ``_SESSION_GLOBS`` sucht — die History-/Rollover-Logik
#: kann diese Dateien also nie mit Transkripten verwechseln — und wird vom
#: Tailer ausschliesslich ueber :func:`preview_channel` gelesen.
_PREVIEWS_DIRNAME = "previews"
_PREVIEW_SUFFIX = ".jsonl"


# ── Session-Aufloesung (I/O) ────────────────────────────────────────────────


def resolve_transcript_dir(agent) -> Path | None:
    """Wurzelverzeichnis der omp-Transkripte dieses Agenten, oder ``None``.

    Fail-closed und explizit auf den Harness gegated: nur ein
    ``cli-bridge``-Agent mit ``harness == "omp"`` und einem Slug bekommt ein
    Verzeichnis. „Nicht Claude" ist ausdruecklich KEIN ausreichendes
    Kriterium (Review-Befund A2 am Claude-Adapter) — Kimi laeuft ebenfalls
    als cli-bridge und hat ein voellig anderes Format.

    Enten-typisiert auf ``agent.slug`` / ``agent.agent_runtime`` /
    ``agent.harness``, damit Tests einen einfachen Stub uebergeben koennen.
    """
    slug = getattr(agent, "slug", None)
    if not slug:
        return None
    runtime = getattr(agent, "agent_runtime", None)
    harness = getattr(agent, "harness", None)

    # Zweiter erlaubter Fall seit „Chat over ACP" (docs/specs/chat-over-acp.md):
    # der Hermes-Chat-Daemon laeuft auf dem HOST, schreibt aber exakt dasselbe
    # omp-Transkriptformat in dieselbe Ordnerform. Damit liest die ganze
    # Leser-/Vorschau-/Rollover-Kette unveraendert weiter — ein zweites Format
    # waere reine Doppelarbeit. Das Tor bleibt eng: host + harness „hermes",
    # nichts anderes.
    if runtime == "host" and harness == "hermes":
        return _host_home() / ".mc" / "agents" / slug / _SESSIONS_DIRNAME

    if runtime != "cli-bridge":
        return None
    if harness != HARNESS:
        return None
    return _host_home() / ".mc" / "agents" / slug / _SESSIONS_DIRNAME


def find_active_session(tdir: Path) -> tuple[Path, dict[str, Any]] | None:
    """Neueste ``*.jsonl``-Session unter ``tdir``.

    Anders als beim Claude-Adapter wird EINE Ebene tief gesucht: omp legt je
    Arbeitsverzeichnis einen eigenen Unterordner an (``--workspace--``,
    ``--workspace-bench-…--``) und schreibt die Session-Datei dort hinein.
    Ein Agent kann Sessions in mehreren solchen Ordnern haben (der omp-Agent hat
    sechs) — die aktive ist schlicht die zuletzt geschriebene ueber alle
    hinweg.

    Rueckgabe wie beim Claude-Adapter: ``(pfad, meta)`` mit
    ``{"sessionId", "mtime", "live"}``. ``None``, wenn es das Verzeichnis
    nicht gibt oder keine Session darin liegt.
    """
    if not tdir.is_dir():
        return None

    # Rangfolge nach dem Zeitstempel des LETZTEN EINTRAGS, nicht nach mtime —
    # eine beruehrte Altdatei wuerde sonst die laufende Sitzung verdecken
    # (siehe transcript_chat.last_entry_timestamp; derselbe Fehler galt hier).
    from app.services.transcript_chat import last_entry_timestamp

    newest_path: Path | None = None
    newest_mtime = -1.0
    newest_rank = -1.0
    for pattern in _SESSION_GLOBS:
        for candidate in tdir.glob(pattern):
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            entry_ts = last_entry_timestamp(candidate)
            rank = mtime if entry_ts is None else entry_ts
            if (rank, mtime) > (newest_rank, newest_mtime):
                newest_rank = rank
                newest_mtime = mtime
                newest_path = candidate

    if newest_path is None:
        return None

    # Under OMP_DRIVER=acp one session has TWO files (see session_id_for):
    # omp's own and the bridge sink's. Only the sink carries what MC adds —
    # question cards, chat errors, mode lines — so it must be THE file of
    # the session, every time. Ranking alone flips between the two (omp's
    # file is created first and often written last), and the chat then
    # tailed omp's file: no card until a reload, status "ended" (P3 live
    # test 13.10.2026).
    newest_path = bridge_sibling(newest_path)

    return newest_path, {
        "sessionId": newest_path.stem,
        "mtime": datetime.fromtimestamp(newest_mtime, tz=timezone.utc).isoformat(),
        "live": (time.time() - newest_mtime) < _LIVE_WINDOW_SECONDS,
    }


def session_scan_root(session_path: Path) -> Path:
    """Von einer Session-Datei zurueck auf die Sessions-WURZEL.

    Genau ueber dieser Wurzel muss der Rollover-Scan laufen: wechselt der
    Agent das Arbeitsverzeichnis, legt omp die neue Session in einem ANDEREN
    Unterordner an, und ein Scan nur im alten Ordner wuerde den Wechsel nie
    bemerken.

    Die Wurzel wird am VERZEICHNISNAMEN erkannt (``_SESSIONS_DIRNAME``) —
    demselben Namen, den ``resolve_transcript_dir`` baut — und nicht durch
    Zaehlen von ``.parent``. Zwei Ebenen hoch stimmt nur fuer das uebliche
    Layout ``<wurzel>/<kodiertes-cwd>/<datei>.jsonl``; fuer eine FLACHE
    Datei direkt unter der Wurzel (``find_active_session`` unterstuetzt das
    ausdruecklich, ``_SESSION_GLOBS``) landete man eine Ebene ZU HOCH — in
    ``~/.mc/agents/<slug>``. Dort liegt ``claude-config/history.jsonl``, und
    der Rollover-Scan haette dieses fremde Transkript als „neuere Session"
    gesehen: ``resolve_aliveness`` meldet ``ended`` fuer eine LEBENDE
    Sitzung, und der Tailer versucht jeden Tick einen Rollover dorthin.

    Ohne ``omp-sessions`` im Pfad (heute nur in Tests) bleibt es beim
    Ordner der Datei selbst — lieber einen cwd-Wechsel verpassen als in
    einen fremden Baum hinauslaufen.
    """
    for parent in session_path.parents:
        if parent.name == _SESSIONS_DIRNAME:
            return parent
    return session_path.parent


def transcript_allowed(agent, path: Path) -> bool:
    """Privacy-Tor, fail-closed.

    omp-Sessions liegen pro Agent in einem eigenen Bind-Mount
    (``~/.mc/agents/<slug>/omp-sessions``) — anders als Boss teilt sich hier
    kein Agent ein Verzeichnis mit den privaten Sitzungen des Operators.
    Trotzdem wird NICHT einfach ``True`` zurueckgegeben: geprueft wird, dass
    der Pfad tatsaechlich unterhalb des Verzeichnisses DIESES Agenten liegt.
    Ein Aufrufer, der einen fremden Pfad mitgibt (Rollover-Pruefung, Test,
    kuenftiger Aufrufer), faellt damit sauber durch, statt ein fremdes
    Transkript freizugeben.
    """
    tdir = resolve_transcript_dir(agent)
    if tdir is None:
        return False
    try:
        resolved = path.resolve()
        root = tdir.resolve()
    except OSError:
        return False
    return resolved != root and root in resolved.parents


def _session_id_from_stem(stem: str) -> str:
    """Die Sitzungs-UUID aus einem Transkript-Dateistamm ``<ts>_<uuid>``.

    Der Zeitstempel-Teil enthaelt keinen Unterstrich, die UUID also der Rest
    nach dem ersten. Geteilt aus ``preview_channel`` (Session-Bindung) und
    ``session_id_for`` (Rollover-Vergleich) — beide brauchen exakt dieselbe
    Extraktion, und zwei leicht abweichende Kopien waeren genau die Falle,
    die ein gemeinsamer Helfer verhindert."""
    return stem.split("_", 1)[1] if "_" in stem else stem


def session_id_for(path: Path) -> str:
    """Die stabile Sitzungs-UUID einer Transkript-Datei — fuer den Tailer-
    Rollover-Vergleich (``transcript_chat.ChatTailerManager.
    _is_genuine_rollover``), der sonst JEDEN Pfadwechsel als neue Sitzung
    liest.

    Unter ``OMP_DRIVER=acp`` schreiben ZWEI unabhaengige Prozesse je eine
    eigene Datei fuer DIESELBE Sitzung in denselben cwd-Ordner: die native
    omp-CLI und der ACP-Bridge-Sink (``docker/omp-bridge/acp_chat_events.
    ChatEventSink`` — dessen eigener Docstring sagt „the same shape omp
    writes", bewusst dasselbe Schema). Beide Dateinamen tragen dieselbe
    Sitzungs-UUID, nur mit unterschiedlich genauem Zeitstempel-Praefix
    (Sekunden vs. Millisekunden+``Z``), und beide wachsen ueber einen langen
    Zug mit vielen Werkzeugaufrufen unabhaengig weiter — „die neueste Datei"
    kann darum im Sekundentakt zwischen ihnen hin- und herspringen, OHNE
    dass sich die Sitzung je aendert (Operator-Befund 04.10.2026: der
    Kontext-Ring im Composer blinkte deshalb waehrend eines laufenden Zugs
    mehrfach weg/zurueck — jeder Sprung loeste einen echten, aber
    FALSCHEN ``session_changed`` auf dem SSE-Strom aus).

    Vergleicht der Aufrufer zwei Pfaden ueber diese Funktion statt ueber den
    Pfad selbst, ist eine Geschwisterdatei mit derselben UUID keine neue
    Sitzung mehr."""
    return _session_id_from_stem(path.stem)


#: How many lines from the top of a session file ``session_cwd`` reads to
#: find the ``session`` header. omp writes it as line 1 or 2 (a ``title``
#: line can come first — seen live 04.10.2026); the margin is for safety.
_SESSION_HEADER_SCAN_LINES = 10


def _session_header(path: Path) -> dict[str, Any] | None:
    """The ``{"type": "session", ...}`` header line of a session file, or
    ``None``. Reads at most ``_SESSION_HEADER_SCAN_LINES`` lines."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= _SESSION_HEADER_SCAN_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("type") == "session":
                    return entry
    except OSError:
        return None
    return None


#: ``is_bridge_file`` answers, by ``(path, inode)``. The tailer asks every
#: tick; the header never changes once written, so one read per file is
#: enough. A file without a header yet (just created) is not cached.
_BRIDGE_FILE_CACHE: dict[tuple[str, int], bool] = {}


def is_bridge_file(path: Path) -> bool:
    """Written by the omp-bridge ACP sink (header ``"bridge": "acp"``,
    ``acp_chat_events.ChatEventSink``) rather than by omp itself."""
    try:
        key = (str(path), path.stat().st_ino)
    except OSError:
        return False
    cached = _BRIDGE_FILE_CACHE.get(key)
    if cached is not None:
        return cached
    header = _session_header(path)
    if header is None:
        return False
    answer = header.get("bridge") == "acp"
    if len(_BRIDGE_FILE_CACHE) > 4096:
        _BRIDGE_FILE_CACHE.clear()
    _BRIDGE_FILE_CACHE[key] = answer
    return answer


def bridge_sibling(path: Path) -> Path:
    """The bridge sink's file of the same session as ``path`` (same folder,
    same embedded session id), or ``path`` itself when there is none or
    ``path`` already is it."""
    if is_bridge_file(path):
        return path
    session_id = session_id_for(path)
    try:
        siblings = sorted(path.parent.glob("*.jsonl"))
    except OSError:
        return path
    for sibling in siblings:
        if sibling != path and session_id_for(sibling) == session_id and is_bridge_file(sibling):
            return sibling
    return path


def prefers_sibling(new_path: Path, current_path: Path) -> bool:
    """For the tailer: switch from ``current_path`` to its same-session
    sibling ``new_path``? Only towards the bridge file, never back — a
    one-way move cannot flip-flop (the reason same-session switches were
    banned, see ``session_id_for``)."""
    return is_bridge_file(new_path) and not is_bridge_file(current_path)


def _header_cwd(path: Path) -> str | None:
    """``cwd`` of the ``{"type": "session", ...}`` header line, or ``None``
    when the file has none or records it empty."""
    header = _session_header(path)
    if header is None:
        return None
    cwd = header.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def session_cwd(path: Path) -> str | None:
    """The working directory an omp session runs in, as omp recorded it in
    the session header (a container path for Docker agents, e.g.
    ``/workspace``).

    Under ``OMP_DRIVER=acp`` two files carry the same session (see
    ``session_id_for``): omp's own file has the real ``cwd``, the ACP bridge
    sink writes the header with an EMPTY ``cwd`` — and the sink file is often
    the one ``find_active_session`` returns. So when this file's header is
    empty, the siblings in the same folder that carry the same session id
    are asked. The folder NAME also encodes the cwd, but not reversibly
    (``-`` stands for both ``/`` and a literal dash), so it is deliberately
    not decoded. ``None`` when no file of this session records a cwd.
    """
    own = _header_cwd(path)
    if own:
        return own
    session_id = session_id_for(path)
    try:
        siblings = sorted(path.parent.glob("*.jsonl"))
    except OSError:
        return None
    for sibling in siblings:
        if sibling == path or session_id_for(sibling) != session_id:
            continue
        cwd = _header_cwd(sibling)
        if cwd:
            return cwd
    return None


def preview_channel(session_path: Path) -> Path | None:
    """Die Vorschau-Kanal-Datei zu einer ACP-Session, oder ``None``.

    Der omp-bridge ACP-Treiber schreibt die fluechtigen Vorschau-Snapshots
    (Folge-PR zu #471) NICHT mehr in die Transkript-JSONL, sondern in eine
    Schwesterdatei unter ``previews/``. Diese Funktion leitet aus dem Pfad
    der getailten Transkript-Session die zugehoerige Vorschau-Datei her —
    das juengste ``*.jsonl`` im ``previews/``-Ordner desselben cwd-Ordners,
    das DIESE Session traegt.

    Session-Bindung (Operator-Befund 16.09.2026): Transkript
    (``<ts>_<sessionId>.jsonl``, ``ChatEventSink``) und Vorschau
    (``<ts>_<sessionId>_<uniq>.jsonl``, ``PreviewEventSink``) tragen
    dieselbe ACP-Session-ID. Ohne Bindung gewann nach einem Container-
    neustart die juengste FREMDE Datei einer beendeten Session — der Verlauf
    zeigte die neue, leere Session („No messages yet"), waehrend LIVE
    PREVIEW den letzten Zug VOR dem Neustart nachspielte. Zwei Quellen, die
    auseinanderlaufen. Sichtbarkeitsregel, die dadurch entsteht: eine
    Vorschau existiert nur zu einer Session, deren Transkript der Tailer
    gerade liest; beim Oeffnen eines ruhenden Agenten gibt es keine.

    Fail-closed: fehlt der Ordner, ist er leer oder traegt keine Datei
    diese Session (native Sitzung, alter Stand), gibt es keine Vorschau
    ueber diesen Kanal — der Pane-Strom bleibt unberuehrt.
    """
    pdir = session_path.parent / _PREVIEWS_DIRNAME
    session_id = _session_id_from_stem(session_path.stem)
    own_file = re.compile(rf"(?:^|_){re.escape(session_id)}(?:_|\.)")
    try:
        newest: Path | None = None
        newest_mtime = -1.0
        for candidate in pdir.glob(f"*{_PREVIEW_SUFFIX}"):
            if own_file.search(candidate.name) is None:
                continue
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            if mtime > newest_mtime:
                newest_mtime = mtime
                newest = candidate
    except OSError:
        return None
    return newest


# ── Parser (rein) ───────────────────────────────────────────────────────────

#: Zeilentypen, die bewusst KEIN Chat-Ereignis erzeugen. Explizit gelistet,
#: damit ein unbekannter Typ vom bekannten-aber-stummen unterscheidbar ist
#: (Debug-Log nur beim wirklich Unbekannten).
_SILENT_TYPES = frozenset(
    {
        "title",
        "title_change",
        "session",
        "model_change",
        "service_tier_change",
    }
)

#: ``custom``-Untertypen, die kein Chat-Ereignis erzeugen.
#: ``tool_execution_start`` spiegelt nur den ``toolCall``-Block, den der
#: Assistant-Eintrag bereits getragen hat — daraus ein zweites Werkzeug-
#: Ereignis zu bauen wuerde jede Karte doppeln.
_SILENT_CUSTOM_TYPES = frozenset({"tool_execution_start", "session_exit", "autoresearch-control"})

#: Werkzeuge, deren Titel aus einem Pfad-Argument gebaut wird.
_PATH_TOOLS = {"read": "Read", "write": "Write", "edit": "Edit"}

#: Ergebnis-Text wird auf dieselbe Laenge gekuerzt wie beim Claude-Adapter
#: (``transcript_chat._RESULT_TRUNCATE_LEN``) — die Kuerzung dort passiert
#: erst beim Merge, hier schon beim Zusammenfuegen der Bloecke, damit ein
#: 500-KB-Bash-Ergebnis nicht erst komplett im Speicher landet.
_RESULT_TRUNCATE_LEN = 4000

#: Ein ``fileMention``-Eintrag traegt den ganzen Dateiinhalt. Im Chat zeigen
#: wir Kopf + Anfang, nicht die ganze Datei.
_READ_HEADER = re.compile(r"^\[.*#[0-9A-Za-z]+\]$")
_LINE_NUMBER = re.compile(r"^\d+:")
_MENTION_TRUNCATE_LEN = 2000


def build_tool_title(name: str, args: dict[str, Any]) -> str:
    """Kurzer, lesbarer Titel fuer einen omp-Werkzeugaufruf.

    omp-Werkzeuge heissen klein (``read``, ``bash``, ``edit``, …) und tragen
    ihre Argumente unter anderen Schluesseln als Claude Code (``path`` statt
    ``file_path``, ``command``, ``pattern``). Zusaetzlich gibt es ``i`` — die
    Kurzabsicht, die manche Treiber (z.B. `hermes acp`, docker/omp-bridge/
    acp_chat_events.py `_tool_seed`) als EINZIGES Argument mitschicken, ohne
    ``command``/``path``/``pattern`` ueberhaupt zu setzen. Ein Titel, der nur
    auf dem sprechenden Argument aufbaut, wird dann leer (bzw. beim Bash-Titel
    zum nackten ``"$"``) — jeder Zweig faellt deshalb selbst auf ``i`` zurueck,
    bevor der Aufrufer ganz am Werkzeugnamen landet.
    """
    intent = args.get("i")
    intent = intent.strip() if isinstance(intent, str) else ""

    if name in _PATH_TOOLS:
        base = _basename(args.get("path"))
        if base:
            title = f"{_PATH_TOOLS[name]} {base}".strip()
        else:
            title = f"{_PATH_TOOLS[name]}: {intent}" if intent else ""
    elif name == "bash":
        command = args.get("command")
        if isinstance(command, str) and command.strip():
            title = f"$ {command}"
        else:
            title = f"$ {intent}" if intent else ""
    elif name == "grep":
        pattern = args.get("pattern")
        if isinstance(pattern, str) and pattern.strip():
            title = f'Search "{pattern}"'
        else:
            title = f"Search: {intent}" if intent else ""
    elif name == "browser":
        title = f"Browser {args.get('action', '')} {args.get('url', '')}".strip()
    elif name == "todo":
        title = f"Todo {args.get('op', '')}".strip()
    else:
        title = f"{name}: {intent}" if intent else ""

    return _truncate_title(title.strip() or name)


_MAX_LOCATIONS = 20


def _clean_locations(raw: Any) -> list[str]:
    """File paths a tool call touched (chat package P1): the bridge writes
    plain strings, ACP itself ``{path, line?}`` — both accepted, deduped,
    capped. Anything else is dropped (fail closed)."""
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        path = item.get("path") if isinstance(item, dict) else item
        if not isinstance(path, str) or not path.strip():
            continue
        path = path.strip()[:500]
        if path not in out:
            out.append(path)
        if len(out) >= _MAX_LOCATIONS:
            break
    return out


def _clean_session_cost(raw: Any) -> dict[str, Any] | None:
    """``{amount, currency}`` only for a real, positive, finite amount —
    a local model reports none, and "0.00" would be a figure nobody
    measured."""
    if not isinstance(raw, dict):
        return None
    amount = raw.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    if amount != amount or amount in (float("inf"),) or amount <= 0:
        return None
    currency = raw.get("currency")
    if not isinstance(currency, str) or not currency.strip():
        currency = "USD"
    return {"amount": float(amount), "currency": currency.strip().upper()[:8]}


def _basename(path: Any) -> str:
    if not path:
        return ""
    return str(path).rstrip("/").rsplit("/", 1)[-1]


def _blocks_to_text(content: Any) -> str:
    """Flacht eine omp-Blockliste zu Text ab.

    ``image``-Bloecke tragen nur eine Blob-Referenz (``blob:sha256:…``) und
    keinen darstellbaren Inhalt — sie werden als Marke vermerkt, damit der
    Operator sieht, DASS ein Bild kam, statt dass es spurlos verschwindet.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif btype == "image":
            parts.append(f"[Bild: {block.get('mimeType') or 'image'}]")
    return "\n".join(parts)


def _strip_read_format(content: str) -> str:
    """omp legt den Inhalt einer ``@datei`` in seinem Leseformat ab:
    ``[pfad#HASH]`` als Kopfzeile, dann jede Zeile mit ``N:`` davor. Das ist
    omps Zeilenzaehler, nicht der Auftrag — im Chat stand er trotzdem
    (``1:# Operating Card — Alpha``, Marks Screenshot 04.09.2026)."""
    lines = content.split("\n")
    if lines and _READ_HEADER.match(lines[0]):
        lines = lines[1:]
    return "\n".join(_LINE_NUMBER.sub("", line) for line in lines)


_TASK_FILE_RE = re.compile(r"^task-.*\.md$")
_INBOX_FILE_RE = re.compile(r"^\d{6,}__.*\.msg$")
_NEW_TASK_RE = re.compile(r"^# New Task:\s*(?P<title>.+?)\s*$", re.MULTILINE)


def _mention_source(name: str | None, content: str) -> dict[str, Any] | None:
    """Herkunft einer ``@datei``-Erwaehnung, maschinenlesbar fuer das Chat-Motiv.

    Zeilen „von aussen" sollen zugeklappt auf einen Blick erkennbar sein, mit
    eigenem Motiv je Herkunft. Das Frontend soll dafuer nicht an Dateinamen
    herumraten, also entscheidet der Parser:

    * ``task-….md`` → ``task``. Der Titel steht in der Auftragsdatei erst
      hinter der Operating Card — also weit hinter den
      ``_MENTION_TRUNCATE_LEN`` Zeichen, die im Chat mitgezeigt werden. Er
      wird deshalb hier aus dem UNGEKUERZTEN Inhalt gelesen.
    * ``.msg-nudge.msg`` → ``nudge`` (Anstoss „schau in den Posteingang").
    * ``0000000N__….msg`` → ``inbox`` (ueber MC zugestellte Nachricht).

    Ohne einzelnen Absender (mehrere Dateien) wird keine Herkunft behauptet.
    """
    if not name:
        return None
    if _TASK_FILE_RE.match(name):
        m = _NEW_TASK_RE.search(content)
        return {"kind": "task", "title": m.group("title") if m else None}
    if "nudge" in name:
        return {"kind": "nudge", "title": None}
    if _INBOX_FILE_RE.match(name):
        return {"kind": "inbox", "title": None}
    return None


class OmpLineParser:
    """Zustandsbehafteter Zeilen-Parser: eine Rohzeile -> 0..n ChatEvents.

    Der Zustand ist bewusst minimal — die zuletzt gesehene Effort-Stufe
    (``thinking_level_change``), weil omp sie in einer EIGENEN Zeile
    protokolliert statt am Zug, und ein kleiner Zwischenspeicher fuer
    ``write``-Werkzeugaufrufe (s. ``_write_args``). Jeder Lesevorgang
    (History-Seite, Tailer-Lauf) bekommt eine eigene Instanz; ein
    Session-Wechsel setzt sie zurueck.

    Wirft nie: eine kaputte oder unbekannte Zeile ergibt eine leere Liste
    (Transkript-Formate aendern sich ohne Ankuendigung, ein Parser darf
    daran nicht sterben).
    """

    def __init__(self) -> None:
        self._effort: str | None = None
        # toolCallId -> {"path", "content"} for a seeded tool call whose
        # arguments carry a WHOLE new file body (omp's native `write` tool,
        # or the ACP bridge forwarding the same tool generically under
        # kind/name "edit" — see `_parse_tool_result`). The matching
        # toolResult never repeats the body (omp: {"resolvedPath": ...};
        # ACP: no "diff" content item at all), so without this the card had
        # nothing to build a diff from and fell back to raw JSON (task 475,
        # live 12.10.2026 omp 18.10.1 over ACP). Popped on first use.
        self._write_args: dict[str, dict[str, str]] = {}
        # The ACP bridge's sink header names the task a session belongs to
        # (``{"type":"session", ..., "taskId": ...}``). In such a file every
        # user line is a prompt MC wrote — files written before the bridge
        # stamped ``attribution: "mc"`` (19.10.2026) are recognised by this.
        self._task_session = False

    def reset(self) -> None:
        self._effort = None
        self._write_args = {}
        self._task_session = False

    def seed_from(self, session_path: Path) -> None:
        """Liest die zuletzt geschriebene Effort-Stufe aus einer bestehenden
        Datei. Siehe ``new_parser`` fuer das Warum. Wirft nie."""
        try:
            with open(session_path, "rb") as f:
                for raw in f:
                    if b'"thinking_level_change"' not in raw:
                        continue
                    self(raw.decode("utf-8", errors="replace"))
        except OSError:
            logger.debug("omp_chat: Effort-Vorgabe nicht lesbar", exc_info=True)

    # Signatur identisch zu ``transcript_chat.parse_transcript_line``, damit
    # beide Adapter dieselben Aufrufstellen bedienen.
    def __call__(
        self, line: str, observed_windows: dict[str, int] | None = None
    ) -> list[dict[str, Any]]:
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            logger.debug("omp_chat: kaputte JSON-Zeile, uebersprungen")
            return []
        if not isinstance(d, dict):
            logger.debug("omp_chat: Zeile ist kein JSON-Objekt, uebersprungen")
            return []

        entry_type = d.get("type")
        try:
            if entry_type == "session":
                self._task_session = bool(d.get("taskId")) and d.get("bridge") == "acp"
                return []
            if entry_type == "message":
                return self._parse_message(d, observed_windows)
            if entry_type == "thinking_level_change":
                level = d.get("thinkingLevel")
                self._effort = level if isinstance(level, str) else None
                return []
            if entry_type == "custom_message":
                return self._parse_custom_message(d)
            if entry_type == "custom":
                if d.get("customType") == ACP_PLAN_CUSTOM_TYPE:
                    # The omp bridge's forwarded ACP ``plan`` update
                    # (docker/omp-bridge/acp_chat_events.py ``_plan``).
                    data = d.get("data") if isinstance(d.get("data"), dict) else {}
                    todo = todo_event_from_acp_plan(
                        data.get("entries"), uuid=d.get("id"), ts=d.get("timestamp")
                    )
                    return [todo] if todo is not None else []
                if d.get("customType") == ACP_ELICITATION_RESULT_CUSTOM_TYPE:
                    return _elicitation_result_event(d)
                if d.get("customType") == ACP_MODE_CUSTOM_TYPE:
                    return _mode_event(d)
                if d.get("customType") not in _SILENT_CUSTOM_TYPES:
                    logger.debug("omp_chat: unbekannter custom-Typ %s", d.get("customType"))
                return []
            if entry_type in _SILENT_TYPES:
                return []
        except Exception:
            logger.debug("omp_chat: %s-Eintrag nicht parsebar", entry_type, exc_info=True)
            return []

        logger.debug("omp_chat: unbekannter Zeilentyp %s, uebersprungen", entry_type)
        return []

    # -- message ------------------------------------------------------------

    def _parse_message(
        self, d: dict[str, Any], observed_windows: dict[str, int] | None
    ) -> list[dict[str, Any]]:
        entry_id = d.get("id")
        ts = d.get("timestamp")
        message = d.get("message")
        if not entry_id or not ts or not isinstance(message, dict):
            return []

        role = message.get("role")
        if role == "user":
            return self._parse_user(entry_id, ts, message, task_session=self._task_session)
        if role == "assistant":
            return self._parse_assistant(entry_id, ts, message, observed_windows)
        if role == "toolResult":
            return self._parse_tool_result(message)
        if role == "fileMention":
            return self._parse_file_mention(entry_id, ts, message)
        logger.debug("omp_chat: unbekannte Rolle %s, uebersprungen", role)
        return []

    @staticmethod
    def _parse_user(
        entry_id: str, ts: str, message: dict[str, Any], *, task_session: bool = False,
    ) -> list[dict[str, Any]]:
        text = _blocks_to_text(message.get("content"))
        if not text:
            return []
        origin = message.get("origin") if isinstance(message.get("origin"), dict) else None
        if message.get("attribution") == "mc" or origin is not None or task_session:
            # A prompt Mission Control wrote into an ACP task session (the
            # dispatch with its Operating Card, or a continue-nudge). Over
            # the native TUI the same arrives as an ``@task-….md`` mention
            # and renders as a task event — over ACP it was a plain user
            # turn and the chat showed 8000 characters of briefing as the
            # operator's own bubble (incident 19.10.2026, bench task).
            kind = (origin or {}).get("kind")
            if kind not in ("task", "nudge"):
                kind = "task" if _NEW_TASK_RE.search(text) or task_session else "nudge"
            title = None
            if kind == "task":
                m = _NEW_TASK_RE.search(text)
                title = m.group("title") if m else None
            return [
                {
                    "kind": "message",
                    "uuid": entry_id,
                    "ts": ts,
                    "role": "teammate",
                    "teammate": None,
                    "source": {"kind": kind, "title": title},
                    "text": text[:_MENTION_TRUNCATE_LEN],
                    "model": None,
                    "sidechain": False,
                }
            ]
        return [
            {
                "kind": "message",
                "uuid": entry_id,
                "ts": ts,
                "role": "user",
                "text": text,
                "model": None,
                "sidechain": False,
            }
        ]

    @staticmethod
    def _parse_file_mention(
        entry_id: str, ts: str, message: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """``@datei``-Erwaehnung -> eine Nachricht mit der Rolle ``teammate``.

        So spielt die omp-Bruecke (``bridge.py:inject_file``) jeden Auftrag,
        jede gequeuete Nachricht und jeden Nudge ein. Es ist Eingabe ins
        Gespraech — aber NICHT vom Operator: geschrieben hat sie Mission
        Control selbst. Auf der Nutzer-Seite (so war es bis hierher) sah es
        aus, als haette Mark 2000 Zeichen Briefing getippt; live in der omp-Agents
        Transkripten sind das 33 Zeilen, darunter zwei Operating-Cards.

        Das ist dieselbe Fehlerklasse, die ``0fd8542c`` fuer Claude Code
        behoben hat, und sie bekommt dieselbe Behandlung: die dritte Rolle
        ``teammate``, die das Frontend als ruhige, sichtbar fremde Zeile
        rendert (``ChatMessage.tsx``).

        Absender ist der Dateiname — der steht so in den Daten und sagt dem
        Operator, WOHER es kam (``task-….md`` = Auftrag, ``.msg-nudge.msg``
        = Anstoss, ``0000000N__….msg`` = zugestellte Nachricht). Bei
        mehreren Dateien gibt es keinen einzelnen Absender, dann wird auch
        keiner behauptet.

        Der Dateiinhalt wird gekuerzt mitgezeigt, damit im Chat sichtbar
        ist, WAS der Agent bekommen hat, statt nur eines nackten Pfades.
        """
        files = message.get("files")
        if not isinstance(files, list) or not files:
            return []
        parts: list[str] = []
        paths: list[str] = []
        raw: list[str] = []
        for entry in files:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path") or ""
            content = entry.get("content")
            full = _strip_read_format(str(content)) if content else ""
            body = full[:_MENTION_TRUNCATE_LEN]
            parts.append(f"@{path}\n{body}".rstrip())
            paths.append(str(path))
            raw.append(full)
        text = "\n\n".join(p for p in parts if p)
        if not text:
            return []
        sender = PurePosixPath(paths[0]).name if len(paths) == 1 and paths[0] else None
        return [
            {
                "kind": "message",
                "uuid": entry_id,
                "ts": ts,
                "role": "teammate",
                "teammate": sender or None,
                "source": _mention_source(sender, raw[0] if len(raw) == 1 else ""),
                "text": text,
                "model": None,
                "sidechain": False,
            }
        ]

    def _parse_assistant(
        self,
        entry_id: str,
        ts: str,
        message: dict[str, Any],
        observed_windows: dict[str, int] | None,
    ) -> list[dict[str, Any]]:
        content = message.get("content")
        if isinstance(content, str):
            # Bei omp bisher nie beobachtet (657/657 Zeilen sind Listen) —
            # defensiv trotzdem behandelt, weil genau diese Annahme beim
            # Claude-Adapter live gebrochen ist und dort still jede getippte
            # Nachricht verschluckt hat.
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            return []

        model = message.get("model")
        events: list[dict[str, Any]] = []

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")

            if btype == "text":
                text = block.get("text")
                if text is None:
                    continue
                events.append(
                    {
                        "kind": "message",
                        "uuid": entry_id,
                        "ts": ts,
                        "role": "assistant",
                        "text": text,
                        "model": model,
                        "sidechain": False,
                    }
                )
            elif btype == "thinking":
                text = block.get("thinking")
                if text is None:
                    continue
                events.append(
                    {
                        "kind": "thinking",
                        "uuid": entry_id,
                        "ts": ts,
                        "text": text,
                        "sidechain": False,
                    }
                )
            elif btype == "toolCall":
                name = block.get("name")
                if not name:
                    continue
                args = block.get("arguments")
                if not isinstance(args, dict):
                    args = {}
                tool_use_id = block.get("id")
                tool_ev: dict[str, Any] = {
                    "kind": "tool",
                    "uuid": entry_id,
                    "ts": ts,
                    "name": name,
                    "title": build_tool_title(name, args),
                    "detail": _truncate_detail(args),
                    "toolUseId": tool_use_id,
                    "result": None,
                    "status": "done",
                    "stats": None,
                    "sidechain": False,
                }
                # Chat package P1: the file paths ACP named for this call
                # (the bridge copies them onto the block) — a running card
                # already shows which file it touches. Absent = no key.
                locations = _clean_locations(block.get("locations"))
                if locations:
                    tool_ev["locations"] = locations
                events.append(tool_ev)
                # Remember a WHOLE-file write (content + path) by call id —
                # see `_write_args`'s docstring. Not gated on `name == "write"`:
                # the ACP bridge forwards the same call generically as "edit".
                path, content = args.get("path"), args.get("content")
                if (
                    isinstance(tool_use_id, str)
                    and tool_use_id
                    and isinstance(path, str)
                    and path.strip()
                    and isinstance(content, str)
                ):
                    self._write_args[tool_use_id] = {"path": path, "content": content}
                # A tool that sends the FULL list (todo_write/update_plan —
                # what a model drives over ACP/Hermes may call it) becomes
                # the todo snapshot too. omp's native ``todo`` tool sends
                # deltas (``op``) and is deliberately not mapped (chat_todos).
                todo = todo_event_from_tool(name, args, uuid=entry_id, ts=ts)
                if todo is not None:
                    events.append(todo)

        usage = message.get("usage")
        if isinstance(usage, dict):
            components = {
                "input": usage.get("input") or 0,
                "cacheRead": usage.get("cacheRead") or 0,
                # omp nennt es cacheWrite, das Schema cacheCreation — dasselbe
                # Ding, der Vertrag gewinnt.
                "cacheCreation": usage.get("cacheWrite") or 0,
                "output": usage.get("output") or 0,
            }
            # omp's own live context accounting, when the build writes it:
            # `usedTokens` against `contextWindow` is the CURRENT FILL, the
            # same figure the pane's context meter shows. `input` above is
            # session-CUMULATIVE (no prompt cache → every call re-processes
            # the whole conversation; live 14.09.2026: input=11,795,309
            # against a 500k window) — feeding it to the frontend's
            # inputTokens/window fallback clamped the chat ring at 100%
            # forever (task 156f57c7). When the truth fields exist they win
            # outright: usedPct becomes a CLI figure (source "cli"), the
            # window replaces the model-name guess, and inputTokens carries
            # the fill itself so the panel's Belegt/Frei rows and the ring
            # can't disagree. The raw cumulative buckets stay out of
            # `components` — a breakdown of "where the window went" is not
            # derivable from them, and a wrong breakdown reads as a
            # measurement.
            omp_window = usage.get("contextWindow")
            used_tokens = usage.get("usedTokens")
            has_fill_truth = (
                isinstance(omp_window, (int, float)) and omp_window > 0 and
                isinstance(used_tokens, (int, float)) and used_tokens >= 0
            )
            # Cache hit rate (cacheRead / (cacheRead + input)) is a RATIO,
            # not a window breakdown — unlike `components` above it survives
            # `has_fill_truth` on purpose. An ACP agent's `_usage_block`
            # (acp_chat_events.py) always stamps usedTokens/contextWindow
            # from usage_update ALONGSIDE this turn's real input/cacheRead,
            # so has_fill_truth is normally True there; nulling the ratio
            # too (as `components` does) would mean an ACP agent's context
            # panel never shows a hit rate at all (#464 mid — this is the
            # fix). cache_hit_denom==0 -> None, never a fake 0%.
            cache_hit_denom = components["input"] + components["cacheRead"]
            cache_hit_rate = (
                round(components["cacheRead"] / cache_hit_denom, 4)
                if cache_hit_denom > 0
                else None
            )
            usage_ev: dict[str, Any] = {
                    "kind": "usage",
                    "uuid": entry_id,
                    "ts": ts,
                    "inputTokens": (
                        int(used_tokens)
                        if has_fill_truth
                        else (
                            components["input"]
                            + components["cacheRead"]
                            + components["cacheCreation"]
                        )
                    ),
                    "outputTokens": components["output"],
                    "model": model,
                    # Aus der zuletzt gesehenen thinking_level_change-Zeile —
                    # omp schreibt die Stufe nicht an den Zug (s. Docstring).
                    "effort": self._effort,
                    "contextWindow": (
                        int(omp_window)
                        if has_fill_truth
                        else resolve_context_window(model, observed_windows)
                    ),
                    # Kein Breakdown aus kumulativen Buckets ableitbar (s.o.)
                    # — ehrlich gar keine statt einer falschen.
                    "components": components if not has_fill_truth else None,
                    "cacheHitRate": cache_hit_rate,
                    **(
                        {
                            "usedPct": round(used_tokens / omp_window * 100, 1),
                            "source": "cli",
                        }
                        if has_fill_truth
                        else {}
                    ),
                }
            # Chat package P1: the SESSION's running cost, stamped by the ACP
            # bridge from ``usage_update.cost`` (only when the harness
            # reported a real one). omp's native per-message ``cost`` is a
            # different number and stays out (see module docstring).
            session_cost = _clean_session_cost(usage.get("sessionCost"))
            if session_cost is not None:
                usage_ev["sessionCost"] = session_cost
            events.append(usage_ev)

        return events

    def _parse_tool_result(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """``role: toolResult`` -> internes ``_tool_result``-Ereignis.

        Verknuepft ueber ``toolCallId`` == ``toolCall.id`` — genau die
        Verknuepfung, die einen Zug mit MEHREREN parallelen Werkzeugaufrufen
        korrekt aufloest (live beobachtet: zwei Aufrufe, deren Ergebnisse in
        umgekehrter Reihenfolge eintrafen). Erreicht das Frontend nie
        alleine; der Aufrufer merged es auf die passende Werkzeug-Karte.
        """
        tool_call_id = message.get("toolCallId")
        if not tool_call_id:
            return []
        result: dict[str, Any] = {
            "kind": "_tool_result",
            "tool_use_id": tool_call_id,
            "content": _blocks_to_text(message.get("content"))[:_RESULT_TRUNCATE_LEN],
            "is_error": bool(message.get("isError")),
        }
        is_error = bool(message.get("isError"))
        # A seeded whole-file write is only ever consumed once — pop it now,
        # error or not, so a stray later result with the same id (should
        # never happen) never reuses it (test:
        # write_args_do_not_leak_into_an_unrelated_later_result).
        write_args = self._write_args.pop(tool_call_id, None)
        # Chat package P1: one diff per edited file — from the ACP bridge's
        # ``details.diffs`` or omp's own edit details (native TUI path).
        diffs: list[dict[str, Any]] = []
        details = message.get("details")
        if isinstance(details, dict) and not is_error:
            diffs = diffs_from_acp(details.get("diffs") if "diffs" in details else details)
            locations = _clean_locations(details.get("locations"))
            if locations:
                result["locations"] = locations
        # P2: the bridge stored the tool's pictures next to the transcript
        # (media/<sha256>.<ext>) and left only their references here.
        # Internal: ``_merge_tool_result`` turns them into card ``images``.
        if isinstance(details, dict) and isinstance(details.get("images"), list):
            refs = [
                {"sha256": i.get("sha256"), "mimeType": i.get("mimeType")}
                for i in details["images"]
                if isinstance(i, dict)
            ]
            if refs:
                result["image_refs"] = refs
        if not diffs and not is_error and write_args is not None:
            # omp's `write` tool result repeats only "wrote N bytes" / the
            # resolved path (native: {"resolvedPath": ...}; ACP-forwarded:
            # no diff content item at all) — the toolCall's own content is
            # the only place the new body survives. Rendered as "create"
            # (all additions, +N badge, "New file" label): the card used to
            # show raw JSON for every one of these, create or overwrite —
            # "New file" is the task's explicit choice over that, not a
            # claim that a prior body never existed (unlike an ACP edit's
            # verified ``oldText: null``).
            diff = build_diff(write_args["path"], None, write_args["content"], kind="create")
            if diff is not None:
                diffs = [diff]
        if diffs:
            result["diffs"] = diffs
        return [result]

    @staticmethod
    def _parse_custom_message(d: dict[str, Any]) -> list[dict[str, Any]]:
        """Systemhinweis, den omp selbst ins Gespraech einspeist.

        Real beobachtet: ``customType: "async-result"`` — das Ergebnis eines
        im Hintergrund gestarteten Bash-Jobs, das dem Modell als
        ``<system-notice>`` nachgereicht wird. Es hat den naechsten Zug
        ausgeloest, also gehoert es sichtbar in den Verlauf; ohne es
        erschiene die Antwort des Agenten grundlos.

        Rolle ``teammate``, nicht ``user``: es ist zwar EINGABE — der Agent
        hat es nicht gesagt, er hat es bekommen — aber getippt hat es
        niemand. omp speist es selbst ein, und als Nachricht des Operators
        angezeigt ist es schlicht falsch zugeordnet (dieselbe Fehlerklasse
        wie bei ``_parse_file_mention``). Absender ist der ``customType``,
        der einzige Hinweis auf die Herkunft, den die Zeile mitbringt.

        ``display: false`` heisst „omp zeigt das selbst nicht an" — dann
        zeigen wir es auch nicht. Ein FEHLENDES ``display`` ist ausdruecklich
        etwas anderes: das Format ist versioniert und aendert sich ohne
        Ankuendigung; alle drei Live-Beispiele tragen das Feld, aber das ist
        eine Beobachtung, keine Zusage. Ein weggeworfener Systemhinweis
        liesse genau die Antwort grundlos dastehen, die dieser Docstring
        ausschliesst — deshalb nur der explizite ``False``-Vergleich.
        """
        if d.get("display") is False:
            return []
        entry_id = d.get("id")
        ts = d.get("timestamp")
        content = d.get("content")
        if not entry_id or not ts or not isinstance(content, str) or not content:
            return []
        custom_type = d.get("customType")
        # Review #471 N3: ACP preview flushes (the bridge's streaming
        # snapshots) are VOLATILE — they ride the replace-me preview slot
        # (same channel the pane tailer broadcasts on), never the permanent
        # timeline. Stacking them as teammate messages doubled the answer
        # in the history and grew the transcript quadratically.
        if custom_type == "acp-preview":
            return [
                {
                    "kind": "preview",
                    "uuid": None,
                    "ts": ts,
                    "text": content[:_RESULT_TRUNCATE_LEN],
                    "source": "acp",
                }
            ]
        if custom_type == "acp-tool-output":
            # Chat package P1: a running tool's output so far (the bridge's
            # volatile snapshot). Never a timeline row: it rides its own
            # replace-me slot keyed by the tool call and dies once the
            # tool's result lands. It belongs on the preview channel; this
            # branch keeps it from ever rendering as a system message should
            # one reach a transcript.
            data = d.get("data") if isinstance(d.get("data"), dict) else {}
            tool_use_id = data.get("toolCallId")
            if not isinstance(tool_use_id, str) or not tool_use_id:
                return []
            return [
                {
                    "kind": "tool_output",
                    "uuid": None,
                    "ts": ts,
                    "toolUseId": tool_use_id,
                    "text": content[:_RESULT_TRUNCATE_LEN],
                }
            ]
        if custom_type == ACP_ELICITATION_CUSTOM_TYPE:
            # Chat-Paket P3: eine Rueckfrage des Agenten (ACP form
            # elicitation). Eigene Art, damit das Frontend eine Karte mit
            # Knoepfen/Feldern baut. Ohne brauchbares Schema faellt die Zeile
            # unten durch und erscheint als schlichter Systemhinweis — die
            # Frage geht nie verloren.
            event = _elicitation_event(d, entry_id, ts, content)
            if event is not None:
                return [event]
        if custom_type == "chat_notice":
            # Something MC decided for the operator (acp_model.py: the model
            # the session was pinned to is not offered anymore). Not red —
            # the turn goes on — but visible, with the values the frontend
            # fills into its localized sentence.
            data = d.get("data") if isinstance(d.get("data"), dict) else {}
            notice = {
                k: data.get(k) for k in ("code", "previous", "current")
                if isinstance(data.get(k), str) and data.get(k)
            }
            return [
                {
                    "kind": "message",
                    "uuid": entry_id,
                    "ts": ts,
                    "role": "teammate",
                    "teammate": custom_type,
                    "source": {"kind": "notice", "title": notice.get("code")},
                    "notice": notice or None,
                    "text": content[:_RESULT_TRUNCATE_LEN],
                    "model": None,
                    "sidechain": False,
                }
            ]
        if custom_type == "chat_error":
            # Chat over ACP: der Chat-Daemon hat keinen anderen Weg, einen
            # Fehler sichtbar zu machen — er schreibt ihn als eigene Zeile ins
            # Transkript (Boss' Bedingung: „Fehler muessen als sichtbares
            # Ereignis im Chat landen"). Eigene Quelle ``error`` + Code, damit
            # das Frontend eine rote Karte daraus baut, statt den Fehler unter
            # den uebrigen Systemhinweisen verschwinden zu lassen.
            data = d.get("data") if isinstance(d.get("data"), dict) else {}
            return [
                {
                    "kind": "message",
                    "uuid": entry_id,
                    "ts": ts,
                    "role": "teammate",
                    "teammate": custom_type,
                    "source": {"kind": "error", "title": custom_type},
                    "error": {"code": data.get("code"), "detail": data.get("detail")},
                    "text": content[:_RESULT_TRUNCATE_LEN],
                    "model": None,
                    "sidechain": False,
                }
            ]
        return [
            {
                "kind": "message",
                "uuid": entry_id,
                "ts": ts,
                "role": "teammate",
                "teammate": custom_type if isinstance(custom_type, str) and custom_type else None,
                "source": {
                    "kind": "system",
                    "title": custom_type if isinstance(custom_type, str) and custom_type else None,
                },
                "text": content[:_RESULT_TRUNCATE_LEN],
                "model": None,
                "sidechain": False,
            }
        ]


#: Chat-Paket P3 (docker/omp-bridge/acp_chat_events.py): Rueckfrage-Karte,
#: ihr Ausgang, und der Sitzungsmodus (Plan-Modus).
ACP_ELICITATION_CUSTOM_TYPE = "acp-elicitation"
ACP_ELICITATION_RESULT_CUSTOM_TYPE = "acp-elicitation-result"
ACP_MODE_CUSTOM_TYPE = "acp-mode"
_ELICITATION_STATUSES = frozenset({"answered", "declined", "cancelled", "expired"})


def _elicitation_event(
    d: dict[str, Any], entry_id: Any, ts: Any, content: str
) -> dict[str, Any] | None:
    """``acp-elicitation`` -> ``{"kind": "elicitation", ...}`` oder ``None``,
    wenn Kennung oder Schema fehlen (dann zeigt der Aufrufer die Frage als
    schlichten Systemhinweis). Das Schema hat der Chat-Daemon bereits auf die
    gerenderten Felder beschnitten (``acp_chat.clean_schema``)."""
    data = d.get("data") if isinstance(d.get("data"), dict) else {}
    elicitation_id = data.get("elicitationId")
    schema = data.get("schema")
    if (
        not isinstance(elicitation_id, str)
        or not elicitation_id
        or not isinstance(schema, dict)
        or not isinstance(schema.get("properties"), dict)
        or not schema["properties"]
    ):
        return None
    message = data.get("message") if isinstance(data.get("message"), str) else content
    return {
        "kind": "elicitation",
        "uuid": entry_id,
        "ts": ts,
        "elicitationId": elicitation_id,
        "message": message[:_RESULT_TRUNCATE_LEN],
        "schema": schema,
        # omp's plan approval (bridge: acp_chat.is_plan_approval) — the card
        # offers "Cancel (stops agent)" instead of "Skip".
        "planApproval": data.get("planApproval") is True,
    }


def _elicitation_result_event(d: dict[str, Any]) -> list[dict[str, Any]]:
    """``acp-elicitation-result`` -> ``{"kind": "elicitation_result", ...}``.
    Kein Zeitleisten-Eintrag: das Frontend legt es neben die Karte mit
    derselben ``elicitationId`` (beantwortet / abgelaufen)."""
    data = d.get("data") if isinstance(d.get("data"), dict) else {}
    elicitation_id = data.get("elicitationId")
    status = data.get("status")
    if not isinstance(elicitation_id, str) or status not in _ELICITATION_STATUSES:
        return []
    if not d.get("id") or not d.get("timestamp"):
        return []
    answer = data.get("content") if isinstance(data.get("content"), dict) else None
    return [
        {
            "kind": "elicitation_result",
            "uuid": d["id"],
            "ts": d["timestamp"],
            "elicitationId": elicitation_id,
            "status": status,
            "content": answer,
        }
    ]


def _mode_event(d: dict[str, Any]) -> list[dict[str, Any]]:
    """``acp-mode`` -> ``{"kind": "mode", "modeId"}`` — der Modus-Chip im
    Composer folgt dem neuesten (leise, keine Karte)."""
    data = d.get("data") if isinstance(d.get("data"), dict) else {}
    mode_id = data.get("modeId")
    if not isinstance(mode_id, str) or not mode_id or not d.get("id") or not d.get("timestamp"):
        return []
    return [{"kind": "mode", "uuid": d["id"], "ts": d["timestamp"], "modeId": mode_id[:64]}]


def new_parser(session_path: Path | None = None) -> OmpLineParser:
    """Frischer Parser, optional mit dem Zustand der bereits geschriebenen
    Zeilen vorgeladen.

    Der Live-Tailer setzt seinen Lese-Offset ans DATEIENDE (sonst wuerde beim
    Verbinden die ganze Historie noch einmal als „live" durchlaufen). Damit
    sieht er die ``thinking_level_change``-Zeile nie, die am Session-ANFANG
    steht — und jedes Live-``usage``-Ereignis truege ``effort: null``, waehrend
    die History-Seite fuer dieselbe Session ``"high"`` liefert. Im Composer
    kippt der Effort-Chip dann mitten im Gespraech von „high" auf „auto"
    (live gesehen 19.08.2026). Deshalb wird die zuletzt protokollierte Stufe
    hier einmal aus der Datei nachgelesen.

    Fail-silent: eine fehlende oder unlesbare Datei laesst den Parser
    schlicht ohne Vorgabe starten.
    """
    parser = OmpLineParser()
    if session_path is not None:
        parser.seed_from(session_path)
    return parser


def peek_entry_id(line: str) -> str | None:
    """Bester Versuch, die stabile Eintrags-ID (``id``) zu lesen — der
    Dedup-Schluessel des Live-Pfades. Wirft nie."""
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    entry_id = d.get("id")
    return entry_id if isinstance(entry_id, str) else None


def stamp_usage(ev: dict[str, Any], session_path: Path) -> None:
    """Kein Statuszeilen-Kanal bei omp — bewusst wirkungslos.

    Claude Code spiegelt seine eigene Kontextfenster-Buchhaltung in eine
    Datei (``statusline-state/<session>.json``); omp tut das nicht. Statt
    eine Zahl zu erfinden, bleibt ``usedPct``/``source`` schlicht leer und
    das UI faellt auf seine eigene Schaetzung zurueck.
    """
    return None


# ── Zug-Ende aus dem Transkript ─────────────────────────────────────────────

#: Nur diese ``stopReason``-Werte beenden einen Zug. ``toolUse`` und
#: ``length`` heissen „der Agent macht weiter" (Werkzeuglauf bzw.
#: Auto-Kompaktierung) — dieselbe Zuordnung, die ``bridge.py`` seit ADR-049
#: gegen omp v16.2.13 verifiziert benutzt.
_TERMINAL_STOP_REASONS = frozenset({"stop", "error", "aborted"})

_TURN_END_TAIL_BYTES = 262_144
_TURN_END_SCAN_LINES = 50


def transcript_suggests_turn_ended(path: Path) -> bool:
    """``True``, wenn die letzte inhaltliche Zeile einen beendeten Zug zeigt.

    Gegenstueck zu ``ChatTailerManager._transcript_suggests_turn_ended`` fuer
    Claude Code, aber praeziser: omp schreibt ``message.stopReason`` an jeden
    Assistant-Zug, also muss nichts aus der Blockliste erraten werden.

    Zweck: eine frische mtime allein heisst nicht „arbeitet" — sie stammt
    direkt nach dem Zug von der Abschlussantwort selbst. Ohne diese Probe
    dreht die Statuszeile nach Zug-Ende noch sekundenlang weiter.

    Synchron und billig (ein Tail-Read); der Aufrufer wrappt in
    ``asyncio.to_thread``. Fail-silent -> ``False`` (dann gilt konservativ
    das reine mtime-Verhalten).
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - _TURN_END_TAIL_BYTES))
            tail = f.read()
        lines = [ln for ln in tail.split(b"\n") if ln.strip()]
        if not lines:
            return False
        for raw in list(reversed(lines))[:_TURN_END_SCAN_LINES]:
            try:
                entry = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                # Erste Zeile kann ein abgeschnittener Rest der Chunk-Grenze
                # sein — dann konservativ aufhoeren statt raten.
                return False
            if not isinstance(entry, dict) or entry.get("type") != "message":
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                return False
            role = message.get("role")
            if role != "assistant":
                # user / toolResult / fileMention: der Zug laeuft noch.
                return False
            return message.get("stopReason") in _TERMINAL_STOP_REASONS
        return False
    except Exception:
        return False


# ── Pane-Sonde (rein) ───────────────────────────────────────────────────────

#: Die Abbruch-Marke der omp-Arbeitszeile. Der Text davor WECHSELT
#: („Working…", „Reading hostname file" — beides live am 19.08.2026
#: aufgezeichnet), diese Marke nicht. Sie ist damit das omp-Gegenstueck zu
#: Claude Codes ``esc to interrupt``.
_WORKING_MARKER = "⟦esc⟧"

#: Untere Rahmenzeile des Eingabefeldes. Identisch zu
#: ``bridge.py:_COMPOSER_BOTTOM_PREFIX`` — die dortige Regel laeuft seit
#: 2026-07-12 in Produktion und entscheidet dort ueber Absende-Erfolg.
_COMPOSER_BOTTOM_PREFIX = "╰─"
#: Rechtes Ende derselben Zeile — real bis omp 18.1.x: ``╰─ <entwurf> … ─╯``
#: (18.10.1 zeichnet keinen rechten Rand mehr, die Zeile endet nach dem Text).
_COMPOSER_BOTTOM_SUFFIX = "─╯"
#: Platzhalter-Hinweis im LEEREN Eingabefeld (omp 18.x, live geprueft an
#: 18.10.1, in 18.1.10 noch nicht vorhanden): rechtsbuendig
#: ``<Taste> to change thinking effort`` (frische Sitzung) bzw.
#: ``<Taste> to see N running agents`` — Quelle omp
#: ``packages/tui/src/prompt/composer-hints.ts``. ``capture-pane -p`` verliert
#: die gedimmte Darstellung, ohne diese Regel liest er sich wie ein Entwurf
#: und eine frisch gestartete TUI stuende fuer immer auf ``working``.
#: Rechtsbuendig = mindestens zwei Leerzeichen vor der Taste; die Tasten-
#: Beschriftung darf selbst ein Leerzeichen tragen ("Left Left"). Dieselbe
#: Regel steht in ``docker/omp-bridge/bridge.py`` (``_COMPOSER_HINT_RE``).
_COMPOSER_HINT_RE = re.compile(
    r"^\s{2,}\S+(?: \S+)? to (?:change thinking effort|see \d+ running agents?)\s*$"
)


def _composer_draft(composer_line: str) -> str:
    """Schaelt den Entwurfstext aus der unteren Rahmenzeile.

    Nur PRAEFIX und SUFFIX werden entfernt, nicht eine Zeichenmenge: ein
    ``strip("╰╯─│╭╮ ")`` frisst dieselben Glyphen auch mitten aus dem
    Entwurf heraus. Ein Entwurf, der nur aus solchen Zeichen besteht,
    schrumpfte damit auf ``""`` — der Agent haette eine eingereihte, nicht
    abgeschickte Nachricht und wuerde als ``idle`` gemeldet.
    """
    line = composer_line.strip()
    line = line.removeprefix(_COMPOSER_BOTTOM_PREFIX)
    if _COMPOSER_HINT_RE.match(line):
        return ""  # nur der Platzhalter des leeren Feldes, kein Entwurf
    line = line.removesuffix(_COMPOSER_BOTTOM_SUFFIX)
    return line.strip()


_PANE_TAIL_LINES = 40


# ── Denk-Stufe aus der Statuszeile ─────────────────────────────────────────
#
# omp hat kein ``/thinking``-Kommando in der TUI (live geprueft am omp-Pane eines Agenten,
# omp v16.4.6, 02.09.2026: der Text ging als normale Nachricht ans Modell).
# Die Stufe wird per Shift+Tab im Ring gedreht und steht in der Statuszeile
# ueber dem Eingabefeld:
#   ``╭── π  > ⬢ MC model · ◒ high > 📁 /workspace > ◫ 5.9%/500K ⟲ ▶…``
# Beobachtete Anzeigen: ``⟳ auto``, ``○ min``, ``◔ low``, ``◑ med``,
# ``◒ high``, ``◕ xhigh``; ``off`` hat KEINEN Suffix (``⬢ MC model >``).
# Das Glyph vor dem Wort wird bewusst nicht ausgewertet — nur das Wort.
_STATUS_LINE_MODEL_MARKER = "⬢"
_STATUS_LINE_THINKING_RE = re.compile(r"⬢\s*[^>·\n]*?(?:\s·\s\S+\s+(\w+))?\s*>")
_STATUS_LINE_LEVEL_ALIASES = {"min": "minimal", "med": "medium"}


def status_line_thinking_level(pane_text: str | None) -> str | None:
    """Die aktuelle Denk-Stufe (omps Effort-Pendant) aus der Statuszeile —
    ``"off"`` ohne Suffix, ``None`` wenn gar keine Statuszeile im Pane steht
    (TUI bootet noch). Liest die LETZTE Statuszeile: der Pane kann ueber der
    aktuellen noch alte aus dem Scrollback zeigen."""
    if not pane_text:
        return None
    level: str | None = None
    for line in pane_text.splitlines():
        if _STATUS_LINE_MODEL_MARKER not in line or not line.lstrip().startswith("╭"):
            continue
        m = _STATUS_LINE_THINKING_RE.search(line)
        if m is None:
            continue
        word = m.group(1)
        level = "off" if word is None else _STATUS_LINE_LEVEL_ALIASES.get(word, word)
    return level


def parse_pane_state(pane_text: str, transcript_active: bool) -> dict[str, Any]:
    """Klassifiziert einen aufgezeichneten omp-Pane.

    Reihenfolge (erste Regel gewinnt), auf den letzten
    ``_PANE_TAIL_LINES`` Zeilen:

    1. Arbeitszeile (``⟦esc⟧``) irgendwo im Ausschnitt -> ``working``.
    2. Untere Rahmenzeile des Eingabefeldes gefunden -> die TUI ist da und
       nimmt Eingaben an. Mit Entwurfstext darin ODER waehrend das
       Transkript waechst -> ``working`` (der Zug laeuft noch bzw. eine
       eingereihte Nachricht wartet), sonst ``idle``.
    3. Sonst -> ``unknown``.

    ``permission_prompt`` liefert dieser Adapter NIE, und das ist Absicht:
    die Flotte faehrt omp mit ``--approval-mode yolo`` (live in ``ps``
    nachgesehen), es gibt also keine Genehmigungsdialoge, und fuer
    irgendeine andere Auswahl-Oberflaeche existiert keine einzige
    aufgezeichnete Beobachtung. Eine erfundene Prompt-Karte waere schlimmer
    als keine — ``prompt`` bleibt ``None``.

    ``unknown`` ist ein vollwertiger Zustand: eine bootende TUI, ein
    respawntes tmux-Fenster oder eine abgestuerzte CLI sehen alle so aus,
    und „Status unklar" ist ehrlicher als eine geratene Anzeige.
    """
    lines = pane_text.splitlines()[-_PANE_TAIL_LINES:]

    for line in lines:
        if _WORKING_MARKER in line:
            return {"status": "working", "prompt": None}

    composer_line: str | None = None
    for line in lines:
        if line.strip().startswith(_COMPOSER_BOTTOM_PREFIX):
            composer_line = line

    if composer_line is None:
        return {"status": "unknown", "prompt": None}

    draft = _composer_draft(composer_line)
    if draft or transcript_active:
        return {"status": "working", "prompt": None}
    return {"status": "idle", "prompt": None}
