"""Tests fuer den omp-Chat-Adapter (Harness ``omp``, Agent der omp-Agent).

Die Fixture-Zeilen sind gekuerzte, REDIGIERTE Kopien echter Transkript-Zeilen
aus ``~/.mc/agents/omp-agent/omp-sessions`` (aufgezeichnet am 19.08.2026):
Struktur unveraendert, Inhalte neutralisiert — keine personenbezogenen Daten,
keine Tokens, keine Pfade mit echten Namen. Das Repo ist oeffentlich.

Die Pane-Fixtures sind woertliche ``tmux capture-pane``-Ausgaben aus
``mc-agent-<slug>`` (Fenster 0, omp-TUI) — Arbeitszustand einmal mit dem
Standardtext („Working…") und einmal mit einem werkzeugspezifischen Text, weil
genau dieser Text wechselt und deshalb NICHT der Anker sein darf.
"""
import json
from pathlib import Path

import pytest

from app.services import omp_chat
from app.services.omp_chat import (
    OmpLineParser,
    build_tool_title,
    find_active_session,
    parse_pane_state,
    peek_entry_id,
    resolve_transcript_dir,
    session_scan_root,
    transcript_allowed,
    transcript_suggests_turn_ended,
)


# ── Fixture-Zeilen ──────────────────────────────────────────────────────────

SESSION_LINE = '{"type":"session","version":3,"id":"01a019ff-66a6-7000-a079-9ebd1d17f539","timestamp":"2026-08-19T12:29:23.494Z","cwd":"/workspace"}'

TITLE_LINE = '{"type":"title","v":1,"title":"","updatedAt":"2026-08-19T12:29:23.494Z","pad":"          "}'

TITLE_CHANGE_LINE = '{"type":"title_change","id":"58081dfc","parentId":"0a21a02a","timestamp":"2026-08-19T13:58:31.553Z","title":"Beispieltitel","source":"auto"}'

MODEL_CHANGE_LINE = '{"type":"model_change","id":"83a16702","parentId":null,"timestamp":"2026-08-19T12:29:23.603Z","model":"mc-openai/qwen38-27b-unsloth-nvfp4"}'

SERVICE_TIER_LINE = '{"type":"service_tier_change","id":"97551f21","parentId":"47ba161f","timestamp":"2026-08-09T15:04:30.157Z","serviceTier":null}'

THINKING_LEVEL_LINE = '{"type":"thinking_level_change","id":"49534cbb","parentId":"83a16702","timestamp":"2026-08-19T12:29:23.603Z","thinkingLevel":"high","configured":null}'

USER_LINE = '{"type":"message","id":"98935dc3","parentId":"49534cbb","timestamp":"2026-08-19T12:30:24.601Z","message":{"role":"user","content":[{"type":"text","text":"hey"}],"attribution":"user","timestamp":1787142624579}}'

ASSISTANT_LINE = '{"type":"message","id":"a051fce3","parentId":"98935dc3","timestamp":"2026-08-19T12:30:49.620Z","message":{"role":"assistant","content":[{"type":"thinking","thinking":"kurz nachgedacht","thinkingSignature":"reasoning"},{"type":"text","text":"Hallo."}],"api":"openai-completions","provider":"mc-openai","model":"qwen38-27b-unsloth-nvfp4","usage":{"input":23908,"output":59,"cacheRead":7,"cacheWrite":3,"totalTokens":23967,"cost":{"input":0,"output":0,"total":0}},"stopReason":"stop","timestamp":1787142624640,"duration":24956.6,"contextSnapshot":{"promptTokens":23908,"nonMessageTokens":18696}}}'

# Ein Zug mit ZWEI Werkzeugaufrufen (streamIndex 0/1) — real so beobachtet.
MULTI_TOOL_LINE = '{"type":"message","id":"7f378196","parentId":"64318e03","timestamp":"2026-08-19T12:32:22.704Z","message":{"role":"assistant","content":[{"type":"thinking","thinking":"zwei Sachen pruefen","thinkingSignature":"reasoning"},{"type":"toolCall","id":"tool-aaa","name":"read","arguments":{"path":"_tasks/beispiel","i":"Ordner pruefen"},"streamIndex":0},{"type":"toolCall","id":"tool-bbb","name":"bash","arguments":{"command":"ls -t | head -3","i":"Neueste Dateien"},"streamIndex":1}],"api":"openai-completions","provider":"mc-openai","model":"qwen38-27b-unsloth-nvfp4","usage":{"input":27592,"output":588,"cacheRead":0,"cacheWrite":0,"totalTokens":28180},"stopReason":"toolUse","timestamp":1787142693637}}'

# Die beiden Ergebnisse trafen in UMGEKEHRTER Reihenfolge ein — genau dafuer
# ist die Verknuepfung ueber toolCallId da.
TOOL_RESULT_B_LINE = '{"type":"message","id":"3b014323","parentId":"ad496d92","timestamp":"2026-08-19T12:32:22.802Z","message":{"role":"toolResult","toolCallId":"tool-bbb","toolName":"bash","content":[{"type":"text","text":"beispiel.html\\nnotizen.md"}],"details":{"timeoutSeconds":300,"wallTimeMs":82.6},"isError":false,"timestamp":1787142742801}}'

TOOL_RESULT_A_LINE = '{"type":"message","id":"ad496d92","parentId":"2a8d8ebf","timestamp":"2026-08-19T12:32:22.717Z","message":{"role":"toolResult","toolCallId":"tool-aaa","toolName":"read","content":[{"type":"text","text":".\\n  - output/"}],"details":{"meta":{"source":{"type":"path","value":"/workspace/_tasks/beispiel"}}},"isError":false,"timestamp":1787142742717}}'

TOOL_RESULT_ERROR_LINE = '{"type":"message","id":"dd010723","parentId":"291c668b","timestamp":"2026-07-23T17:30:16.302Z","message":{"role":"toolResult","toolCallId":"tool-ccc","toolName":"bash","content":[{"type":"text","text":"Command exited with code 2"}],"details":{"exitCode":2},"isError":true,"timestamp":1784827816301}}'

TOOL_RESULT_IMAGE_LINE = '{"type":"message","id":"9c5fd7f4","parentId":"ebc21d4c","timestamp":"2026-08-09T16:12:04.066Z","message":{"role":"toolResult","toolCallId":"tool-ddd","toolName":"read","content":[{"type":"text","text":"Read image file [image/webp]"},{"type":"image","data":"blob:sha256:0000","mimeType":"image/webp"}],"details":{},"isError":false,"timestamp":1786291924064}}'

FILE_MENTION_LINE = '{"type":"message","id":"bd07f655","parentId":"0241fc9e","timestamp":"2026-07-23T17:29:01.349Z","message":{"role":"fileMention","files":[{"path":"/home/agent/.msg-nudge.msg","content":"[/home/agent/.msg-nudge.msg#2306]\\n1: Neue Nachrichten — lies sie jetzt mit: mc inbox","lineCount":1}],"timestamp":1784827741325}}'

CUSTOM_TOOL_START_LINE = '{"type":"custom","customType":"tool_execution_start","data":{"toolCallId":"tool-aaa","toolName":"read","startedAt":"2026-08-19T12:31:33.423Z","args":{"path":"."},"intent":"Ordner pruefen"},"id":"0a072d79","parentId":"c95f0cad","timestamp":"2026-08-19T12:31:33.424Z"}'

CUSTOM_SESSION_EXIT_LINE = '{"type":"custom","customType":"session_exit","data":{"reason":"exit","kind":"process_exit","recordedAt":"2026-07-23T17:30:02.570Z"},"id":"a382260c","parentId":"167ff326","timestamp":"2026-07-23T17:30:02.570Z"}'

CUSTOM_MESSAGE_LINE = '{"type":"custom_message","customType":"async-result","content":"<system-notice>\\nHintergrund-Job bg_1 ist fertig.\\n</system-notice>","display":true,"details":{"jobs":[{"jobId":"bg_1","type":"bash"}]},"attribution":"agent","id":"b0e6c25b","parentId":"97948230","timestamp":"2026-07-23T08:49:44.991Z"}'

CUSTOM_MESSAGE_HIDDEN_LINE = CUSTOM_MESSAGE_LINE.replace('"display":true', '"display":false')

# Dieselbe Zeile OHNE das Feld ``display`` — „fehlt" ist nicht „ausgeblendet".
CUSTOM_MESSAGE_NO_DISPLAY_LINE = CUSTOM_MESSAGE_LINE.replace('"display":true,', "")

# Der Auftrag, den MC ueber ``bridge.py:inject_file`` einspielt: eine
# Operating Card von 2069 Zeichen. Live in der omp-Agents Transkripten (33 solche
# Zeilen) — sie war NIE etwas, das der Operator getippt hat.
TASK_MENTION_LINE = json.dumps(
    {
        "type": "message",
        "id": "78ba0374",
        "parentId": "0241fc9e",
        "timestamp": "2026-07-16T07:08:51.469Z",
        "message": {
            "role": "fileMention",
            "files": [
                {
                    "path": "/home/agent/.omp/tasks/task-0000.md",
                    "content": "[/home/agent/.omp/tasks/task-0000.md#BFF7]\n1:# Operating Card",
                    "lineCount": 2,
                }
            ],
            "timestamp": 1784827741325,
        },
    }
)

# Zwei Dateien in EINER Erwaehnung — dann gibt es keinen einzelnen Absender.
TWO_FILE_MENTION_LINE = json.dumps(
    {
        "type": "message",
        "id": "aabbccdd",
        "timestamp": "2026-07-16T07:08:51.469Z",
        "message": {
            "role": "fileMention",
            "files": [
                {"path": "/home/agent/a.md", "content": "eins"},
                {"path": "/home/agent/b.md", "content": "zwei"},
            ],
        },
    }
)

# Ein Typ, den es heute nicht gibt — Transkript-Formate aendern sich ohne
# Ankuendigung, der Parser muss ihn still ueberspringen statt zu sterben.
UNKNOWN_TYPE_LINE = '{"type":"telepathy_change","id":"zz","timestamp":"2026-08-19T12:00:00.000Z","vibes":"gut"}'

BROKEN_LINE = '{"type":"message","id":"kaputt",'

NOT_AN_OBJECT_LINE = '["das ist eine Liste"]'


def parse(line, effort_seed=None, observed=None):
    """Eine einzelne Zeile durch einen frischen Parser."""
    p = OmpLineParser()
    if effort_seed is not None:
        p(THINKING_LEVEL_LINE.replace('"high"', f'"{effort_seed}"'))
    return p(line, observed)


# ── Parser: Metadaten-Zeilen erzeugen nichts ────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        SESSION_LINE,
        TITLE_LINE,
        TITLE_CHANGE_LINE,
        MODEL_CHANGE_LINE,
        SERVICE_TIER_LINE,
        THINKING_LEVEL_LINE,
        CUSTOM_TOOL_START_LINE,
        CUSTOM_SESSION_EXIT_LINE,
        CUSTOM_MESSAGE_HIDDEN_LINE,
    ],
)
def test_metadata_lines_emit_nothing(line):
    assert parse(line) == []


def test_tool_execution_start_does_not_duplicate_the_tool_card():
    """``custom/tool_execution_start`` spiegelt nur den toolCall-Block, den der
    Assistant-Eintrag schon getragen hat. Zwei Karten fuer einen Aufruf waeren
    ein sichtbarer Fehler."""
    p = OmpLineParser()
    events = p(MULTI_TOOL_LINE) + p(CUSTOM_TOOL_START_LINE)
    assert [e["toolUseId"] for e in events if e["kind"] == "tool"] == ["tool-aaa", "tool-bbb"]


# ── Parser: Robustheit ──────────────────────────────────────────────────────


@pytest.mark.parametrize("line", [BROKEN_LINE, NOT_AN_OBJECT_LINE, "", "   ", "nicht json"])
def test_broken_lines_never_raise(line):
    assert parse(line) == []


def test_unknown_entry_type_is_skipped_not_fatal():
    assert parse(UNKNOWN_TYPE_LINE) == []


def test_unknown_message_role_is_skipped():
    line = USER_LINE.replace('"role":"user"', '"role":"telepath"')
    assert parse(line) == []


def test_message_without_id_or_timestamp_is_skipped():
    assert parse(USER_LINE.replace('"id":"98935dc3",', "")) == []
    assert parse(USER_LINE.replace('"timestamp":"2026-08-19T12:30:24.601Z",', "", 1)) == []


# ── Parser: Nutzer- und Assistenten-Zuege ───────────────────────────────────


def test_user_turn():
    (ev,) = parse(USER_LINE)
    assert ev == {
        "kind": "message",
        "uuid": "98935dc3",
        "ts": "2026-08-19T12:30:24.601Z",
        "role": "user",
        "text": "hey",
        "model": None,
        "sidechain": False,
    }


def test_user_content_as_plain_string_is_tolerated():
    """Bei omp bisher nie gesehen (657/657 Zeilen sind Listen) — aber genau
    diese Annahme ist beim Claude-Adapter live gebrochen und hat still jede
    getippte Nachricht verschluckt."""
    line = USER_LINE.replace('[{"type":"text","text":"hey"}]', '"hey"')
    (ev,) = parse(line)
    assert ev["text"] == "hey"


def test_assistant_turn_yields_thinking_message_and_usage():
    events = parse(ASSISTANT_LINE, effort_seed="high")
    assert [e["kind"] for e in events] == ["thinking", "message", "usage"]
    thinking, message, usage = events
    assert thinking["text"] == "kurz nachgedacht"
    assert message["role"] == "assistant"
    assert message["model"] == "qwen38-27b-unsloth-nvfp4"
    assert usage["outputTokens"] == 59
    # inputTokens ist die SUMME der Eingabe-Seite (Vertrag mit dem Frontend),
    # components haelt sie getrennt.
    assert usage["inputTokens"] == 23908 + 7 + 3
    assert usage["components"] == {
        "input": 23908,
        "cacheRead": 7,
        # omp nennt es cacheWrite, das Schema cacheCreation.
        "cacheCreation": 3,
        "output": 59,
    }


def test_all_events_of_one_entry_share_the_entry_id():
    events = parse(ASSISTANT_LINE)
    assert {e["uuid"] for e in events} == {"a051fce3"}


def test_effort_comes_from_the_preceding_thinking_level_line():
    """omp schreibt die Stufe in eine EIGENE Zeile, nicht an den Zug — ein
    zustandsloser Pro-Zeile-Parser koennte sie nie liefern."""
    p = OmpLineParser()
    assert p(ASSISTANT_LINE)[-1]["effort"] is None
    p(THINKING_LEVEL_LINE)
    assert p(ASSISTANT_LINE)[-1]["effort"] == "high"
    p(THINKING_LEVEL_LINE.replace('"high"', '"low"'))
    assert p(ASSISTANT_LINE)[-1]["effort"] == "low"


def test_reset_clears_the_effort_state():
    p = OmpLineParser()
    p(THINKING_LEVEL_LINE)
    p.reset()
    assert p(ASSISTANT_LINE)[-1]["effort"] is None


def test_context_window_is_resolved_from_the_observed_map():
    events = parse(ASSISTANT_LINE, observed={"qwen38-27b-unsloth-nvfp4": 1_000_000})
    assert events[-1]["contextWindow"] == 1_000_000


def test_unknown_model_gets_no_invented_context_window():
    assert parse(ASSISTANT_LINE)[-1]["contextWindow"] is None


# ── Parser: Werkzeuge ───────────────────────────────────────────────────────


def test_multi_tool_turn_keeps_both_calls_apart():
    events = parse(MULTI_TOOL_LINE)
    tools = [e for e in events if e["kind"] == "tool"]
    assert [t["toolUseId"] for t in tools] == ["tool-aaa", "tool-bbb"]
    assert [t["name"] for t in tools] == ["read", "bash"]
    assert tools[0]["title"] == "Read beispiel"
    assert tools[1]["title"] == "$ ls -t | head -3"
    assert all(t["status"] == "done" and t["result"] is None for t in tools)


def test_tool_result_is_internal_and_keyed_by_tool_call_id():
    (ev,) = parse(TOOL_RESULT_A_LINE)
    assert ev["kind"] == "_tool_result"
    assert ev["tool_use_id"] == "tool-aaa"
    assert ev["content"] == ".\n  - output/"
    assert ev["is_error"] is False


def test_tool_result_error_flag():
    (ev,) = parse(TOOL_RESULT_ERROR_LINE)
    assert ev["is_error"] is True


def test_tool_result_image_block_is_marked_not_dropped():
    (ev,) = parse(TOOL_RESULT_IMAGE_LINE)
    assert "Read image file" in ev["content"]
    assert "[Bild: image/webp]" in ev["content"]


def test_tool_result_without_call_id_is_dropped():
    line = TOOL_RESULT_A_LINE.replace('"toolCallId":"tool-aaa",', "")
    assert parse(line) == []


def test_tool_call_arguments_are_truncated_into_detail():
    long_cmd = "x" * 3000
    line = MULTI_TOOL_LINE.replace("ls -t | head -3", long_cmd)
    tools = [e for e in parse(line) if e["kind"] == "tool"]
    assert len(tools[1]["detail"]["command"]) == 2001  # 2000 + Auslassungszeichen


@pytest.mark.parametrize(
    "name,args,expected",
    [
        ("read", {"path": "/workspace/a/b.py"}, "Read b.py"),
        ("write", {"path": "/workspace/neu.txt"}, "Write neu.txt"),
        ("edit", {"path": "index.html"}, "Edit index.html"),
        ("bash", {"command": "ls -la"}, "$ ls -la"),
        ("grep", {"pattern": "TODO"}, 'Search "TODO"'),
        ("todo", {"op": "add"}, "Todo add"),
        # Kein sprechendes Argument -> omps eigene Kurzabsicht als Titel.
        ("eval", {"i": "Zahl pruefen", "code": "1+1"}, "eval: Zahl pruefen"),
        # Auch die fehlt -> der nackte Werkzeugname, nie ein leerer Titel.
        ("irc", {}, "irc"),
    ],
)
def test_tool_titles(name, args, expected):
    assert build_tool_title(name, args) == expected


def test_tool_title_is_truncated():
    assert len(build_tool_title("bash", {"command": "y" * 500})) == 80


# ── Parser: eingespeiste Eingaben ───────────────────────────────────────────


def test_file_mention_is_not_a_message_of_the_operator():
    """So spielt ``bridge.py`` jeden Auftrag und jeden Nudge ein — MC redet
    hier mit dem Agenten, nicht Mark. Auf der Nutzer-Seite saehe es aus, als
    haette er 2000 Zeichen Briefing selbst getippt (Befund 19.08.2026, exakt
    die Fehlerklasse aus 0fd8542c). Also eigene Rolle, mit der Datei als
    Absender."""
    (ev,) = parse(FILE_MENTION_LINE)
    assert ev["kind"] == "message"
    assert ev["role"] == "teammate"
    assert ev["teammate"] == ".msg-nudge.msg"
    assert ev["text"].startswith("@/home/agent/.msg-nudge.msg\n")
    assert "mc inbox" in ev["text"]


def test_task_briefing_is_not_a_message_of_the_operator():
    """Der Fall, der live am haesslichsten aussah: die Operating Card."""
    (ev,) = parse(TASK_MENTION_LINE)
    assert ev["role"] == "teammate"
    assert ev["teammate"] == "task-0000.md"
    assert "Operating Card" in ev["text"]


def test_file_mention_with_several_files_claims_no_sender():
    """Kein einzelner Absender -> keiner wird behauptet."""
    (ev,) = parse(TWO_FILE_MENTION_LINE)
    assert ev["role"] == "teammate"
    assert ev["teammate"] is None
    assert "@/home/agent/a.md" in ev["text"] and "@/home/agent/b.md" in ev["text"]


def test_file_mention_without_files_is_dropped():
    line = json.dumps(
        {
            "type": "message",
            "id": "leer1234",
            "timestamp": "2026-07-23T17:29:01.349Z",
            "message": {"role": "fileMention", "files": []},
        }
    )
    assert json.loads(line)["message"]["files"] == []  # gueltiges JSON, leere Liste
    assert parse(line) == []


def test_custom_message_shown_by_omp_is_not_a_message_of_the_operator():
    """``<system-notice>`` kommt von omp selbst, nicht vom Operator."""
    (ev,) = parse(CUSTOM_MESSAGE_LINE)
    assert ev["kind"] == "message"
    assert ev["role"] == "teammate"
    assert ev["teammate"] == "async-result"
    assert "Hintergrund-Job bg_1" in ev["text"]


def test_custom_message_without_a_display_field_is_still_shown():
    """``display`` FEHLT ist nicht ``display: false``. Das Format ist
    versioniert und aendert sich ohne Ankuendigung — ein weggeworfener
    Systemhinweis liesse die folgende Antwort grundlos dastehen."""
    (ev,) = parse(CUSTOM_MESSAGE_NO_DISPLAY_LINE)
    assert ev["kind"] == "message"
    assert "Hintergrund-Job bg_1" in ev["text"]


# ── Dedup-Schluessel ────────────────────────────────────────────────────────


def test_peek_entry_id_reads_the_top_level_id():
    assert peek_entry_id(USER_LINE) == "98935dc3"


def test_peek_entry_id_never_raises():
    assert peek_entry_id(BROKEN_LINE) is None
    assert peek_entry_id(NOT_AN_OBJECT_LINE) is None
    assert peek_entry_id(SESSION_LINE) == "01a019ff-66a6-7000-a079-9ebd1d17f539"


def test_duplicate_line_is_caught_by_the_entry_id():
    """Der Dedup laeuft auf der stabilen Eintrags-ID. Zweimal dieselbe Zeile
    ergibt zwar zweimal dieselben Ereignisse — aber dieselbe ID, also wirft
    der Aufrufer die zweite Runde weg."""
    p = OmpLineParser()
    first = p(ASSISTANT_LINE)
    second = p(ASSISTANT_LINE)
    assert first == second
    assert peek_entry_id(ASSISTANT_LINE) == first[0]["uuid"]


# ── Session-Aufloesung + Privacy ────────────────────────────────────────────


class _Agent:
    def __init__(self, slug="omp-agent", agent_runtime="cli-bridge", harness="omp"):
        self.slug = slug
        self.agent_runtime = agent_runtime
        self.harness = harness


@pytest.fixture
def omp_home(tmp_path, monkeypatch):
    monkeypatch.setattr(omp_chat, "_host_home", lambda: tmp_path)
    return tmp_path


def test_resolve_transcript_dir(omp_home):
    assert resolve_transcript_dir(_Agent()) == omp_home / ".mc/agents/omp-agent/omp-sessions"


@pytest.mark.parametrize(
    "agent",
    [
        _Agent(harness="claude"),
        _Agent(harness="kimi"),
        _Agent(harness=None),
        _Agent(agent_runtime="host"),
        _Agent(agent_runtime="manual"),
        _Agent(slug=None),
        object(),
    ],
)
def test_resolve_transcript_dir_is_fail_closed(agent, omp_home):
    """„Nicht Claude" ist KEIN ausreichendes Kriterium — Kimi laeuft ebenfalls
    als cli-bridge und hat ein voellig anderes Format."""
    assert resolve_transcript_dir(agent) is None


def _make_session(root: Path, cwd_dir: str, name: str, mtime: float) -> Path:
    d = root / cwd_dir
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(SESSION_LINE + "\n")
    import os

    os.utime(p, (mtime, mtime))
    return p


def test_find_active_session_looks_one_level_deep(tmp_path):
    """omp legt je Arbeitsverzeichnis einen eigenen Unterordner an — flach zu
    suchen (wie beim Claude-Adapter) faende gar nichts."""
    old = _make_session(tmp_path, "--workspace--", "a.jsonl", 1_000_000)
    new = _make_session(tmp_path, "--workspace-bench--", "b.jsonl", 2_000_000)
    found, meta = find_active_session(tmp_path)
    assert found == new
    assert meta["sessionId"] == "b"
    assert meta["live"] is False
    assert old.exists()


def test_find_active_session_also_finds_a_flat_file(tmp_path):
    p = _make_session(tmp_path, ".", "flach.jsonl", 3_000_000)
    assert find_active_session(tmp_path)[0] == p


# ── Rollover-Wurzel ─────────────────────────────────────────────────────────


def test_session_scan_root_of_a_nested_file_is_the_sessions_root(tmp_path):
    root = tmp_path / "agents" / "omp-agent" / "omp-sessions"
    p = _make_session(root, "--workspace--", "a.jsonl", 1_000_000)
    assert session_scan_root(p) == root


def test_session_scan_root_of_a_flat_file_is_the_sessions_root(tmp_path):
    """``find_active_session`` unterstuetzt flache Dateien (eigener Test) —
    dann darf die Wurzel NICHT eine Ebene ueber ``omp-sessions`` landen.
    Dort liegt ``claude-config/history.jsonl`` (real bei omp-agent, kimi und 11
    weiteren Agenten): der Rollover-Scan haette die als „neuere Session"
    gesehen und eine LEBENDE Sitzung als beendet gemeldet."""
    root = tmp_path / "agents" / "omp-agent" / "omp-sessions"
    p = _make_session(root, ".", "flach.jsonl", 1_000_000)
    assert session_scan_root(p) == root


def test_session_scan_root_never_escapes_above_the_file_directory(tmp_path):
    """Pfad ohne ``omp-sessions``-Anteil (heute nur in Tests): dann lieber
    den eigenen Ordner scannen als eine geratene Ebene hoeher."""
    p = _make_session(tmp_path, "irgendwo", "a.jsonl", 1_000_000)
    assert session_scan_root(p) == tmp_path / "irgendwo"


def test_flat_session_next_to_a_foreign_transcript_stays_the_active_one(tmp_path):
    """Die Wirkung des Fehlers, End-to-End nachgestellt: neben der
    Sessions-Wurzel liegt ein fremdes, NEUERES ``history.jsonl``. Ueber der
    falschen Wurzel gescannt gilt das als Rollover."""
    root = tmp_path / "agents" / "omp-agent" / "omp-sessions"
    p = _make_session(root, ".", "flach.jsonl", 1_000_000)
    _make_session(root.parent, "claude-config", "history.jsonl", 9_000_000)
    assert find_active_session(session_scan_root(p))[0] == p


def test_find_active_session_on_missing_dir(tmp_path):
    assert find_active_session(tmp_path / "gibtsnicht") is None


def test_find_active_session_on_empty_dir(tmp_path):
    (tmp_path / "--workspace--").mkdir()
    assert find_active_session(tmp_path) is None


def test_find_active_session_marks_a_fresh_file_live(tmp_path):
    import time

    p = _make_session(tmp_path, "--workspace--", "frisch.jsonl", time.time())
    assert find_active_session(tmp_path)[1]["live"] is True
    assert p.exists()


def test_transcript_allowed_only_inside_the_agents_own_dir(omp_home):
    agent = _Agent()
    root = resolve_transcript_dir(agent)
    own = _make_session(root, "--workspace--", "eigen.jsonl", 1_000_000)
    assert transcript_allowed(agent, own) is True


def test_transcript_allowed_rejects_a_foreign_path(omp_home):
    agent = _Agent()
    other_root = omp_home / ".mc/agents/anderer/omp-sessions"
    foreign = _make_session(other_root, "--workspace--", "fremd.jsonl", 1_000_000)
    assert transcript_allowed(agent, foreign) is False
    assert transcript_allowed(agent, omp_home / ".claude/projects/x/y.jsonl") is False


def test_transcript_allowed_rejects_the_root_itself(omp_home):
    agent = _Agent()
    root = resolve_transcript_dir(agent)
    root.mkdir(parents=True)
    assert transcript_allowed(agent, root) is False


def test_transcript_allowed_fails_closed_for_a_foreign_harness(omp_home):
    """Ohne Verzeichnis kein Zugriff — auch nicht auf einen Pfad, der zufaellig
    passend aussieht."""
    agent = _Agent(harness="kimi")
    path = omp_home / ".mc/agents/omp-agent/omp-sessions/--workspace--/x.jsonl"
    assert transcript_allowed(agent, path) is False


# ── Zug-Ende aus dem Transkript ─────────────────────────────────────────────


def _write_transcript(tmp_path: Path, *lines: str) -> Path:
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return p


def test_turn_ended_on_a_stop_reason(tmp_path):
    p = _write_transcript(tmp_path, SESSION_LINE, USER_LINE, ASSISTANT_LINE)
    assert transcript_suggests_turn_ended(p) is True


def test_turn_not_ended_while_tools_are_still_running(tmp_path):
    """``stopReason: toolUse`` heisst „der Agent macht weiter" — dieselbe
    Zuordnung, die ``bridge.py`` gegen omp v16.2.13 verifiziert benutzt."""
    p = _write_transcript(tmp_path, SESSION_LINE, USER_LINE, MULTI_TOOL_LINE)
    assert transcript_suggests_turn_ended(p) is False


def test_turn_not_ended_right_after_a_user_message(tmp_path):
    p = _write_transcript(tmp_path, SESSION_LINE, ASSISTANT_LINE, USER_LINE)
    assert transcript_suggests_turn_ended(p) is False


def test_turn_not_ended_while_a_tool_result_is_the_last_line(tmp_path):
    p = _write_transcript(tmp_path, MULTI_TOOL_LINE, TOOL_RESULT_A_LINE)
    assert transcript_suggests_turn_ended(p) is False


def test_turn_end_probe_skips_trailing_metadata(tmp_path):
    """Nach der Antwort schreibt omp noch ``title_change`` — das darf die
    Zug-Semantik nicht verdecken."""
    p = _write_transcript(tmp_path, USER_LINE, ASSISTANT_LINE, TITLE_CHANGE_LINE)
    assert transcript_suggests_turn_ended(p) is True


def test_turn_end_probe_on_error_stop_reason(tmp_path):
    line = ASSISTANT_LINE.replace('"stopReason":"stop"', '"stopReason":"error"')
    assert transcript_suggests_turn_ended(_write_transcript(tmp_path, line)) is True


def test_turn_end_probe_fails_silent(tmp_path):
    assert transcript_suggests_turn_ended(tmp_path / "gibtsnicht.jsonl") is False
    assert transcript_suggests_turn_ended(_write_transcript(tmp_path, "")) is False
    assert transcript_suggests_turn_ended(_write_transcript(tmp_path, BROKEN_LINE)) is False


# ── Pane-Sonde ──────────────────────────────────────────────────────────────

# Woertliche capture-pane-Ausgaben aus mc-agent-<slug> (19.08.2026).
PANE_IDLE = """\
 Die Datei enthält den Hostnamen c83837ef127c.

╭── π  > ⬢ MC model · ◒ high > 📁 /workspace > ◫ 2.8%/1M ⟲ ▶───────────────────╮
╰─                                                                            ─╯
"""

PANE_WORKING_GENERIC = """\
 Lies bitte die Datei /etc/hostname.

 ⠋ Working… ⟦esc⟧

╭── π  > ⬢ MC model · ◒ high > 📁 /workspace > ◫ 2.8%/1M ⟲ ▶───────────────────╮
╰─                                                                            ─╯
"""

# Derselbe Zustand, anderer Text — genau deshalb ist der Text NICHT der Anker.
PANE_WORKING_TOOL = PANE_WORKING_GENERIC.replace("Working…", "Reading hostname file")

PANE_DRAFT = PANE_IDLE.replace(
    "╰─                                                                            ─╯",
    "╰─ noch nicht abgeschickt                                                     ─╯",
)

# Bootende TUI / respawntes tmux-Fenster / abgestuerzte CLI sehen alle so aus.
PANE_BOOTING = "agent@c83837ef127c:/workspace$ \n"


@pytest.mark.parametrize("pane", [PANE_WORKING_GENERIC, PANE_WORKING_TOOL])
def test_pane_working_is_anchored_on_the_interrupt_marker(pane):
    assert parse_pane_state(pane, False) == {"status": "working", "prompt": None}


def test_pane_idle():
    assert parse_pane_state(PANE_IDLE, False) == {"status": "idle", "prompt": None}


def test_pane_with_a_growing_transcript_reads_as_working():
    assert parse_pane_state(PANE_IDLE, True)["status"] == "working"


def test_pane_with_draft_text_reads_as_working():
    """Eingereihte Nachricht: der Operator hat getippt, waehrend der Agent
    arbeitete. Das Eingabefeld traegt den Entwurf."""
    assert parse_pane_state(PANE_DRAFT, False)["status"] == "working"


def test_pane_draft_made_of_border_glyphs_still_reads_as_working():
    """Der Entwurf wird aus der Rahmenzeile geschaelt. Wird dabei eine
    ZEICHENMENGE gestrippt statt Praefix/Suffix, verschwindet ein Entwurf,
    der selbst aus solchen Zeichen besteht — ein eingereihter, nicht
    abgeschickter Text gaelte dann als ruhender Agent."""
    for draft in ("│", "╭╮", "── │ ──"):
        pane = PANE_IDLE.replace(
            "╰─                                                                            ─╯",
            f"╰─ {draft}                                                                    ─╯",
        )
        assert parse_pane_state(pane, False)["status"] == "working", draft


@pytest.mark.parametrize("pane", [PANE_BOOTING, "", "   \n  "])
def test_pane_without_a_composer_is_unknown(pane):
    """``unknown`` ist ein vollwertiger Status — „Status unklar" ist ehrlicher
    als eine geratene Anzeige."""
    assert parse_pane_state(pane, False) == {"status": "unknown", "prompt": None}


def test_pane_never_claims_a_permission_prompt():
    """Die Flotte faehrt omp mit ``--approval-mode yolo``; es gibt keinen
    beobachteten Genehmigungsdialog. Eine erfundene Prompt-Karte waere
    schlimmer als keine."""
    for pane in (PANE_IDLE, PANE_WORKING_GENERIC, PANE_DRAFT, PANE_BOOTING):
        for active in (True, False):
            state = parse_pane_state(pane, active)
            assert state["status"] != "permission_prompt"
            assert state["prompt"] is None


def test_pane_working_beats_the_composer():
    """Das Eingabefeld ist WAEHREND eines Zuges sichtbar (live so
    aufgezeichnet) — die Arbeitszeile muss deshalb zuerst gepruefte werden."""
    assert "╰─" in PANE_WORKING_GENERIC
    assert parse_pane_state(PANE_WORKING_GENERIC, False)["status"] == "working"


def test_pane_only_the_tail_is_considered():
    """Eine alte Arbeitszeile weit oben im Scrollback darf keinen laufenden
    Zug vortaeuschen."""
    pane = "⠋ Working… ⟦esc⟧\n" + ("Ausgabe\n" * 60) + PANE_IDLE
    assert parse_pane_state(pane, False)["status"] == "idle"


# ── stamp_usage ─────────────────────────────────────────────────────────────


def test_stamp_usage_is_a_no_op(tmp_path):
    """omp schreibt keine Statuszeilen-Datei. Statt eine Zahl zu erfinden,
    bleibt ``usedPct``/``source`` leer."""
    ev = {"kind": "usage", "inputTokens": 1, "outputTokens": 2}
    omp_chat.stamp_usage(ev, tmp_path / "s.jsonl")
    assert ev == {"kind": "usage", "inputTokens": 1, "outputTokens": 2}


# ── History-Seite ueber den Adapter ─────────────────────────────────────────


def test_read_history_with_the_omp_adapter_surfaces_the_omp_turns(tmp_path):
    """Die Gegenprobe zu ``test_read_history_demands_an_adapter``: mit dem
    RICHTIGEN Adapter liefert dieselbe Datei die Zuege, die drinstehen."""
    from app.services.transcript_adapters import adapter_for
    from app.services.transcript_chat import read_history

    class _Agent:
        harness = "omp"

    f = tmp_path / "sess.jsonl"
    f.write_text("\n".join([SESSION_LINE, USER_LINE, ASSISTANT_LINE]) + "\n")

    result = read_history(f, adapter_for(_Agent()), limit=200)
    texts = [e["text"] for e in result["events"] if e["kind"] == "message"]
    assert texts == ["hey", "Hallo."]


# ── Vertrag: jedes Ereignis passt ins ChatEvent-Schema ──────────────────────

_ALLOWED_KEYS = {
    "message": {"kind", "uuid", "ts", "role", "text", "model", "sidechain"},
    "thinking": {"kind", "uuid", "ts", "text", "sidechain"},
    "tool": {
        "kind", "uuid", "ts", "name", "title", "detail", "toolUseId", "result",
        "status", "stats", "sidechain",
    },
    "usage": {
        "kind", "uuid", "ts", "inputTokens", "outputTokens", "model", "effort",
        "contextWindow", "components",
    },
    "_tool_result": {"kind", "tool_use_id", "content", "is_error"},
}


def test_no_event_invents_a_field_outside_the_frontend_contract():
    """Der ChatEvent-Vertrag (``frontend-v2/src/lib/chatTypes.ts``) ist stabil.
    Was omp mehr hat (``cost``, ``duration``, ``ttft``, ``contextSnapshot``),
    wird weggelassen statt das Schema zu verbiegen."""
    p = OmpLineParser()
    events = []
    for line in (
        THINKING_LEVEL_LINE, USER_LINE, ASSISTANT_LINE, MULTI_TOOL_LINE,
        TOOL_RESULT_A_LINE, TOOL_RESULT_B_LINE, TOOL_RESULT_IMAGE_LINE,
        FILE_MENTION_LINE, CUSTOM_MESSAGE_LINE,
    ):
        events += p(line)
    assert events
    for ev in events:
        expected = set(_ALLOWED_KEYS[ev["kind"]])
        # ``teammate`` gehoert zum Vertrag (``MessageEvent.teammate``), aber
        # NUR zur dritten Rolle — eine Nutzer- oder Assistenz-Nachricht darf
        # das Feld nicht mitschleppen.
        if ev["kind"] == "message" and ev["role"] == "teammate":
            expected.add("teammate")
        assert set(ev) == expected, ev
        assert json.dumps(ev)  # JSON-serialisierbar, sonst bricht SSE


# ── Vorgeladener Parser (Live-Tailer steigt am Dateiende ein) ───────────────


def test_new_parser_seeds_the_effort_level_from_an_existing_file(tmp_path):
    """Der Tailer setzt seinen Offset ans DATEIENDE und saehe die
    ``thinking_level_change``-Zeile vom Session-Anfang nie. Ohne Vorgabe
    truege jedes Live-``usage`` ``effort: null``, waehrend die History-Seite
    fuer dieselbe Session ``"high"`` liefert — der Effort-Chip kippt dann
    mitten im Gespraech auf „auto"."""
    p = _write_transcript(
        tmp_path, SESSION_LINE, THINKING_LEVEL_LINE, USER_LINE, ASSISTANT_LINE
    )
    assert omp_chat.new_parser(p)(ASSISTANT_LINE)[-1]["effort"] == "high"
    assert omp_chat.new_parser()(ASSISTANT_LINE)[-1]["effort"] is None


def test_new_parser_seed_uses_the_last_level_written(tmp_path):
    p = _write_transcript(
        tmp_path,
        THINKING_LEVEL_LINE,
        THINKING_LEVEL_LINE.replace('"high"', '"low"'),
    )
    assert omp_chat.new_parser(p)(ASSISTANT_LINE)[-1]["effort"] == "low"


def test_new_parser_seed_fails_silent(tmp_path):
    parser = omp_chat.new_parser(tmp_path / "gibtsnicht.jsonl")
    assert parser(ASSISTANT_LINE)[-1]["effort"] is None


def test_new_parser_seed_ignores_everything_but_the_level_lines(tmp_path):
    """Die Vorgabe darf KEINE Ereignisse erzeugen — sonst erschiene die halbe
    Historie noch einmal als Live-Strom."""
    p = _write_transcript(tmp_path, THINKING_LEVEL_LINE, USER_LINE, ASSISTANT_LINE)
    parser = omp_chat.new_parser(p)
    assert parser(TITLE_LINE) == []
    assert parser(ASSISTANT_LINE)[-1]["effort"] == "high"


# ── Denk-Stufe aus der Statuszeile (Effort-Pendant, s. agent_chat_input) ────

def _omp_pane(level_suffix: str | None) -> str:
    suffix = f" · {level_suffix}" if level_suffix else ""
    return (
        " - Keine neuen Nachrichten — Inbox leer, nichts zu tun.\n"
        f"╭── π  > ⬢ MC model{suffix} > 📁 /workspace > ◫ 5.9%/500K ⟲ ▶──────◀ ──╮\n"
        "╰─                                                                ─╯\n"
    )


def test_omp_status_line_thinking_level_reads_every_observed_form():
    assert omp_chat.status_line_thinking_level(_omp_pane("◒ high")) == "high"
    assert omp_chat.status_line_thinking_level(_omp_pane("◕ xhigh")) == "xhigh"
    assert omp_chat.status_line_thinking_level(_omp_pane("○ min")) == "minimal"
    assert omp_chat.status_line_thinking_level(_omp_pane("◔ low")) == "low"
    assert omp_chat.status_line_thinking_level(_omp_pane("◑ med")) == "medium"
    assert omp_chat.status_line_thinking_level(_omp_pane("⟳ auto")) == "auto"
    assert omp_chat.status_line_thinking_level(_omp_pane(None)) == "off"
    # Keine Statuszeile (bootende TUI) -> None, nicht "off".
    assert omp_chat.status_line_thinking_level("nothing here\n") is None


def test_status_line_thinking_level_takes_the_last_status_line():
    pane = _omp_pane("◒ high") + _omp_pane("◔ low")
    assert omp_chat.status_line_thinking_level(pane) == "low"
