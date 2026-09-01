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
  * Die Effort-Stufe steht NICHT am Zug, sondern in eigenen
    ``thinking_level_change``-Zeilen (``thinkingLevel``). Der Parser ist
    deshalb ZUSTANDSBEHAFTET (``OmpLineParser``) und traegt die zuletzt
    gesehene Stufe an die folgenden ``usage``-Ereignisse — ein reiner
    Pro-Zeile-Parser koennte das nicht.

WAS OMP NICHT HAT: Subagenten/Sidechains (``sidechain`` ist immer ``False``),
  In-Session-Slash-Kommandos im Transkript (kein ``command``-Ereignis) und
  eine Statuszeilen-Datei mit der echten Kontextfenster-Auslastung (kein
  ``source: "cli"``; die Prozentangabe im Pane wird bewusst nicht gescraped).

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
    if getattr(agent, "agent_runtime", None) != "cli-bridge":
        return None
    if getattr(agent, "harness", None) != HARNESS:
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
_MENTION_TRUNCATE_LEN = 2000


def build_tool_title(name: str, args: dict[str, Any]) -> str:
    """Kurzer, lesbarer Titel fuer einen omp-Werkzeugaufruf.

    omp-Werkzeuge heissen klein (``read``, ``bash``, ``edit``, …) und tragen
    ihre Argumente unter anderen Schluesseln als Claude Code (``path`` statt
    ``file_path``, ``command``, ``pattern``). Zusaetzlich gibt es ``i`` — omps
    eigene Kurzabsicht („Checking workspace layout"), die als Titel dient,
    wenn das Werkzeug sonst kein sprechendes Argument hat.
    """
    if name in _PATH_TOOLS:
        title = f"{_PATH_TOOLS[name]} {_basename(args.get('path'))}".strip()
    elif name == "bash":
        title = f"$ {args.get('command', '')}"
    elif name == "grep":
        title = f'Search "{args.get("pattern", "")}"'
    elif name == "browser":
        title = f"Browser {args.get('action', '')} {args.get('url', '')}".strip()
    elif name == "todo":
        title = f"Todo {args.get('op', '')}".strip()
    else:
        intent = args.get("i")
        title = f"{name}: {intent}" if isinstance(intent, str) and intent else name

    return _truncate_title(title.strip() or name)


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


class OmpLineParser:
    """Zustandsbehafteter Zeilen-Parser: eine Rohzeile -> 0..n ChatEvents.

    Der Zustand ist bewusst minimal — nur die zuletzt gesehene Effort-Stufe
    (``thinking_level_change``), weil omp sie in einer EIGENEN Zeile
    protokolliert statt am Zug. Jeder Lesevorgang (History-Seite, Tailer-
    Lauf) bekommt eine eigene Instanz; ein Session-Wechsel setzt sie zurueck.

    Wirft nie: eine kaputte oder unbekannte Zeile ergibt eine leere Liste
    (Transkript-Formate aendern sich ohne Ankuendigung, ein Parser darf
    daran nicht sterben).
    """

    def __init__(self) -> None:
        self._effort: str | None = None

    def reset(self) -> None:
        self._effort = None

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
            if entry_type == "message":
                return self._parse_message(d, observed_windows)
            if entry_type == "thinking_level_change":
                level = d.get("thinkingLevel")
                self._effort = level if isinstance(level, str) else None
                return []
            if entry_type == "custom_message":
                return self._parse_custom_message(d)
            if entry_type == "custom":
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
            return self._parse_user(entry_id, ts, message)
        if role == "assistant":
            return self._parse_assistant(entry_id, ts, message, observed_windows)
        if role == "toolResult":
            return self._parse_tool_result(message)
        if role == "fileMention":
            return self._parse_file_mention(entry_id, ts, message)
        logger.debug("omp_chat: unbekannte Rolle %s, uebersprungen", role)
        return []

    @staticmethod
    def _parse_user(entry_id: str, ts: str, message: dict[str, Any]) -> list[dict[str, Any]]:
        text = _blocks_to_text(message.get("content"))
        if not text:
            return []
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
        for entry in files:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path") or ""
            content = entry.get("content")
            body = str(content)[:_MENTION_TRUNCATE_LEN] if content else ""
            parts.append(f"@{path}\n{body}".rstrip())
            paths.append(str(path))
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
                events.append(
                    {
                        "kind": "tool",
                        "uuid": entry_id,
                        "ts": ts,
                        "name": name,
                        "title": build_tool_title(name, args),
                        "detail": _truncate_detail(args),
                        "toolUseId": block.get("id"),
                        "result": None,
                        "status": "done",
                        "stats": None,
                        "sidechain": False,
                    }
                )

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
            events.append(
                {
                    "kind": "usage",
                    "uuid": entry_id,
                    "ts": ts,
                    "inputTokens": (
                        components["input"]
                        + components["cacheRead"]
                        + components["cacheCreation"]
                    ),
                    "outputTokens": components["output"],
                    "model": model,
                    # Aus der zuletzt gesehenen thinking_level_change-Zeile —
                    # omp schreibt die Stufe nicht an den Zug (s. Docstring).
                    "effort": self._effort,
                    "contextWindow": resolve_context_window(model, observed_windows),
                    "components": components,
                }
            )

        return events

    @staticmethod
    def _parse_tool_result(message: dict[str, Any]) -> list[dict[str, Any]]:
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
        return [
            {
                "kind": "_tool_result",
                "tool_use_id": tool_call_id,
                "content": _blocks_to_text(message.get("content"))[:_RESULT_TRUNCATE_LEN],
                "is_error": bool(message.get("isError")),
            }
        ]

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
        return [
            {
                "kind": "message",
                "uuid": entry_id,
                "ts": ts,
                "role": "teammate",
                "teammate": custom_type if isinstance(custom_type, str) and custom_type else None,
                "text": content[:_RESULT_TRUNCATE_LEN],
                "model": None,
                "sidechain": False,
            }
        ]


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
#: Rechtes Ende derselben Zeile — real: ``╰─ <entwurf> … ─╯``.
_COMPOSER_BOTTOM_SUFFIX = "─╯"


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
    line = line.removesuffix(_COMPOSER_BOTTOM_SUFFIX)
    return line.strip()


_PANE_TAIL_LINES = 40


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
