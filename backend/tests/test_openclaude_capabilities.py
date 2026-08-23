"""openclaude — die Capabilities-Schicht (Modell-Katalog, Effort, Slash-Kommandos).

openclaude ist ein Claude-Code-Fork: Transkript-Format, Eingabe-Kanal und
Zustands-Sonde tragen unveraendert (Phase-0-Discovery 19.08.2026). Was NICHT
traegt, ist die Capabilities-Schicht — und genau die wird hier festgenagelt.

Alle Fixtures in dieser Datei sind ECHTE ``capture-pane``-Ausgaben von
``mc-agent-openclaude-agent`` (openclaude 0.7.0, Modell ``qwen38-27b-unsloth-nvfp4``
gegen den Spark), aufgenommen am 19.08.2026 in WEGWERF-tmux-Fenstern. Keine
persoenlichen Daten — generische Picker-Chrome und Modellnamen.

Die vier teuer erhobenen Unterschiede zu Claude Code:

1. **Scroll-Marker** ``↑``/``↓`` stehen in derselben Spalte wie der Cursor
   ``❯``. Das alte ``_MODEL_ROW_RE`` verwarf solche Zeilen STILL.
2. **Die Liste ist laenger als der Picker** (fester 10-Zeilen-Ausschnitt,
   unabhaengig von der Terminal-Hoehe — bei 200x60 live gegengeprueft).
   Die Fusszeile ``and N more…`` nennt die Zahl der NICHT sichtbaren Zeilen,
   also gilt ``total = sichtbar + N``. Die Liste laeuft beim Blaettern um.
3. **Effort haengt am MODELL, nicht am Harness.** Der ``/model``-Picker sagt
   es unter der Liste fuer die markierte Zeile — beide Auspraegungen live
   gesehen: ``○ Effort not supported for qwen38-27b-unsloth-nvfp4`` und
   ``◐ Medium effort (default) ← → to adjust`` (gpt-5.2-codex).
4. **Eigene Stufenliste.** ``/effort zzz`` antwortet mit dem eigenen
   Validator: ``Invalid argument: zzz. Valid options are: low, medium, high,
   max, xhigh, auto`` — also OHNE ``ultracode``, das Claude Code kennt.
"""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

import app.redis_client as redis_client_mod
from app.services import agent_chat_input
from app.services import harness_catalog as hc

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def no_real_fleet_access(monkeypatch):
    """Wachhund gegen den Real-Host-Leak — die Klasse Fehler, die diese Datei
    schon dreimal getroffen hat.

    ``mc-agent-openclaude-agent`` ist ein ECHTER Container auf Marks Rechner. Wer
    hier einen Stub vergisst, laesst die Suite ``docker exec -u agent
    mc-agent-openclaude-agent openclaude --version`` in die laufende Flotte
    absetzen — und merkt es nicht, weil ``resolve_cli_version`` jede Ausnahme
    schluckt und ``None`` zurueckgibt. Genau darum wird hier gesammelt und
    erst beim Abbau geprueft, statt zu werfen.

    Tests, die ``subprocess.run`` selbst ersetzen (Versions-Parser),
    ueberschreiben diesen Stub — monkeypatch raeumt in umgekehrter
    Reihenfolge ab, die sind also unberuehrt."""
    leaked: list[list[str]] = []

    def _record(argv, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and argv[0] == "docker":
            leaked.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _record)
    yield
    assert not leaked, f"Test griff in die echte Flotte: {leaked}"


# ══════════════════════════════════════════════════════════════════════════
# Echte Pane-Fixtures (mc-agent-openclaude-agent, openclaude 0.7.0, 19.08.2026)
# ══════════════════════════════════════════════════════════════════════════

# Der Picker beim OEFFNEN: der Cursor steht auf dem aktiven Modell (``✔``),
# darum nennt die Effort-Zeile darunter genau dieses Modell.
OPENCLAUDE_PICKER_OPEN = """\
  Select model
  Switch between Claude models. Applies to this session and future OpenClaude sessions. For other/previous model names, specify with --model.

  ↑ 7.  gpt-5.2-codex               GPT-5.2 Codex with high reasoning
    8.  gpt-5.1-codex-max           GPT-5.1 Codex Max for deep reasoning
    9.  gpt-5.1-codex-mini          GPT-5.1 Codex Mini - faster, cheaper
    10. gpt-5.5-mini                GPT-5.5 Mini - faster, cheaper
    11. gpt-5.4-mini                GPT-5.4 Mini - faster, cheaper
    12. Sonnet                      Sonnet 4.6 · Best for everyday tasks
    13. Sonnet (1M context)         Sonnet 4.6 for long sessions
    14. Opus 4.1                    Opus 4.1 · Legacy
    15. Haiku                       Haiku 3.5 for simple tasks
  ❯ 16. qwen38-27b-unsloth-nvfp4 ✔  Custom model
     and 6 more…

  ○ Effort not supported for qwen38-27b-unsloth-nvfp4

  Enter to confirm · Esc to exit
"""

# Nach dem Hochblaettern: Zeilen 1–10, der ``↓``-Marker sitzt auf der letzten
# sichtbaren Zeile. (Die Beschreibungsspalte rutscht dabei zusammen —
# ``GPT-5.5 Minim- faster`` ist echt so aufgenommen. Uninteressant: die
# Beschreibung wird verworfen, das Label steht in einer eigenen Spalte.)
OPENCLAUDE_PICKER_TOP = """\
  Select model
  Switch between Claude models. Applies to this session and future OpenClaude sessions. For other/previous model names, specify with --model.

  ❯ 1.  Default (recommended)  Use the default model (currently qwen38-27b-unsloth-nvfp4)
    2.  gpt-5.5                GPT-5.5 with high reasoning
    3.  gpt-5.4                GPT-5.4 with high reasoning
    4.  gpt-5.3-codex          GPT-5.3 Codex with high reasoning
    5.  gpt-5.3-codex-spark    GPT-5.3 Codex Spark for fast tool loops
    6.  codexspark             GPT-5.3 Codex Spark alias for fast tool loops
    7.  gpt-5.2-codex          GPT-5.2 Codex with high reasoning
    8.  gpt-5.1-codex-max      GPT-5.1 Codex Max for deep reasoning
    9.  gpt-5.1-codex-mini     GPT-5.1 Codex Mini - faster, cheaper
  ↓ 10. gpt-5.5-mini           GPT-5.5 Minim- faster, cheaper
     and 6 more…

  ○ Effort not supported for Default (recommended)

  Enter to confirm · Esc to exit
"""

# Cursor auf ``gpt-5.2-codex`` — ein Modell, das Effort KANN. Die zweite,
# entscheidende Auspraegung derselben Zeile.
OPENCLAUDE_PICKER_EFFORT_SUPPORTED = """\
  Select model

  ↑ 5.  gpt-5.3-codex-spark  GPT-5.3 Codex Spark for fast tool loops
  ❯ 7.  gpt-5.2-codex        GPT-5.2 Codex with high reasoning
  ↓ 14. Opus 4.1             Opus 4.1 · Legacy
     and 6 more…

  ◐ Medium effort (default) ← → to adjust

  Enter to confirm · Esc to exit
"""

# Claude Codes eigene Effort-Zeile (Fixture aus test_harness_catalog.py) —
# derselbe Parser muss beide Schreibweisen lesen (``←/→`` vs. ``← →``).
CLAUDE_EFFORT_LINE = "   ● High effort (default) ←/→ to adjust\n"


def _agent(slug="openclaude-agent", harness="openclaude", runtime="cli-bridge"):
    return SimpleNamespace(slug=slug, agent_runtime=runtime, harness=harness)


# ══════════════════════════════════════════════════════════════════════════
# 1 — parse_model_picker: Scroll-Marker, openclaude-Zeilenformat, Aliasse
# ══════════════════════════════════════════════════════════════════════════


async def test_scroll_marker_rows_are_not_silently_dropped():
    """Der teuerste der vier Unterschiede: ``↑``/``↓`` stehen in der
    Cursor-Spalte. Das alte Muster ``^\\s*(?:❯\\s*)?\\d+\\.`` warf solche
    Zeilen weg, ohne sich zu beschweren — bei openclaude ist das JEDE erste
    und letzte sichtbare Zeile."""
    options = hc.parse_model_picker(OPENCLAUDE_PICKER_OPEN, harness="openclaude")
    commands = [o["command"] for o in options]

    assert "gpt-5.2-codex" in commands  # trug den ``↑``-Marker

    top = hc.parse_model_picker(OPENCLAUDE_PICKER_TOP, harness="openclaude")
    assert "gpt-5.5-mini" in [o["command"] for o in top]  # trug den ``↓``-Marker


async def test_openclaude_picker_rows_parse_to_verified_commands():
    """Jedes Kommando-Token hier wurde live gegengeprueft — in einem
    Wegwerf-Fenster mit EIGENEM ``CLAUDE_CONFIG_DIR``, damit kein Schreiber
    die echte ``settings.json`` des Agenten anfassen konnte:

    - ``default``  -> ``Set model to … (default)``, der ``model``-Schluessel
      verschwindet aus der Datei (genau die Bedeutung der Zeile).
    - ``opus``     -> das ``✔`` landet auf Zeile 14 ``Opus 4.1``.
    - ``haiku``    -> das ``✔`` landet auf Zeile 15 ``Haiku``.
    - ``sonnet``   -> gueltiges Modell (persistiert), siehe eigenen Test.
    - blanke Modell-IDs (``gpt-5.2-codex``, ``qwen38-27b-unsloth-nvfp4``)
      sind ihr eigenes Token — ``qwen38-27b-unsloth-nvfp4`` steht wortgleich
      als ``model`` in der settings.json des Agenten.

    Gegenprobe, dass openclaude ueberhaupt validiert: ``/model
    zzz-not-a-model`` -> ``Model 'zzz-not-a-model' not found``, Datei
    unveraendert. Ein falsch geratenes Token waere also kein Schoenheits-,
    sondern ein Ausfall-Fehler."""
    options = hc.parse_model_picker(OPENCLAUDE_PICKER_TOP, harness="openclaude")

    assert options[0] == {"command": "default", "label": "Default"}
    assert {"command": "gpt-5.3-codex-spark", "label": "gpt-5.3-codex-spark"} in options
    assert {"command": "codexspark", "label": "codexspark"} in options

    open_rows = hc.parse_model_picker(OPENCLAUDE_PICKER_OPEN, harness="openclaude")
    by_command = {o["command"]: o["label"] for o in open_rows}
    assert by_command["opus"] == "Opus 4.1"
    assert by_command["haiku"] == "Haiku"
    assert by_command["sonnet"] == "Sonnet"
    # Das aktive Modell: ``✔`` darf weder ins Label noch ins Kommando lecken.
    assert by_command["qwen38-27b-unsloth-nvfp4"] == "qwen38-27b-unsloth-nvfp4"


async def test_unresolvable_label_is_dropped_not_guessed():
    """``Sonnet (1M context)`` ist die eine Zeile, deren Token NICHT
    ermittelt werden konnte: ``sonnet (1m context)`` und ``sonnet-1m``
    antworten beide ``not found``; ``sonnet[1m]`` wird zwar angenommen,
    erscheint danach aber als ``16. sonnet[1m] ✔  Custom model`` und eben
    NICHT als die gemeinte Zeile 13 — es ist damit unbewiesen, ob es
    dasselbe meint.

    Lieber eine Zeile weniger im Dropdown als ein Klick, der dem Agenten
    ein Modell setzt, das keiner wollte. Verschwiegen wird sie trotzdem
    nicht: ``_discover_via_throwaway_window`` protokolliert sie."""
    commands = [o["command"] for o in hc.parse_model_picker(OPENCLAUDE_PICKER_OPEN, harness="openclaude")]

    assert "Sonnet (1M context)" not in commands
    assert "sonnet[1m]" not in commands
    assert not any(c.lower().startswith("sonnet (") for c in commands)


async def test_claude_picker_still_parses_unchanged():
    """Regressionsnetz: der Claude-Code-Pfad darf sich durch die
    Marker-Erweiterung nicht veraendern."""
    from tests.test_harness_catalog import REAL_MODEL_PICKER_PANE

    assert hc.parse_model_picker(REAL_MODEL_PICKER_PANE) == [
        {"command": "default", "label": "Default"},
        {"command": "sonnet", "label": "Sonnet"},
        {"command": "opus", "label": "Opus"},
        {"command": "haiku", "label": "Haiku"},
        {"command": "Qwen/Qwen3.6-35B-A3B-FP8", "label": "Qwen/Qwen3.6-35B-A3B-FP8"},
    ]


# ══════════════════════════════════════════════════════════════════════════
# 2 — Blaettern: der Katalog ist laenger als der Ausschnitt
# ══════════════════════════════════════════════════════════════════════════


async def test_picker_page_reports_hidden_row_count():
    """``and N more…`` = Zahl der NICHT sichtbaren Zeilen. Damit kennt der
    Sammler seine Sollzahl, statt zu raten, wann er fertig ist: 10 sichtbar
    + 6 versteckt = 16 (live nachgerechnet: hochblaettern zeigte 1–10 mit
    ebenfalls ``and 6 more…``)."""
    rows, hidden, seen = hc._parse_picker_page(OPENCLAUDE_PICKER_OPEN, "openclaude")

    assert hidden == 6
    assert sorted(seen) == list(range(7, 17))  # 10 sichtbare Zeilen, 7..16
    # ``seen`` zaehlt AUCH die uebersprungene Zeile 13 ("Sonnet (1M context)").
    # Genau deshalb: mit ``len(rows)`` waere die Sollzahl 9+6=15 statt 16 —
    # der Sammler haette zu frueh aufgehoert und Zeilen still verschluckt.
    assert 13 in seen and 13 not in rows
    assert len(seen) + hidden == 16


async def test_picker_page_without_footer_reports_no_hidden_rows():
    """Ein Picker, der ganz in den Ausschnitt passt (Claude Code), hat keine
    Fusszeile — dann gibt es nichts zu blaettern."""
    from tests.test_harness_catalog import REAL_MODEL_PICKER_PANE

    rows, hidden, seen = hc._parse_picker_page(REAL_MODEL_PICKER_PANE, "claude")
    assert hidden is None
    assert len(rows) == len(seen) == 5


async def test_discovery_pages_until_the_whole_catalog_is_collected(monkeypatch):
    """Der Sammler blaettert, bis er so viele Zeilen hat, wie die Fusszeile
    verspricht — eine einzelne ``capture-pane`` liefert nur den Ausschnitt.
    Die Liste laeuft dabei um (live: von Zeile 7 fuehrten 9x Down auf 16,
    weitere 9x auf 9, weitere 9x auf 2), darum genuegt Blaettern in EINE
    Richtung."""
    captures = iter([OPENCLAUDE_PICKER_OPEN, OPENCLAUDE_PICKER_TOP])
    keys: list[str] = []

    async def _capture(slug, window):
        return next(captures, OPENCLAUDE_PICKER_TOP)

    async def _tmux(slug, args):
        keys.append(" ".join(args))

    async def _ready(slug, window):
        return True

    monkeypatch.setattr(hc, "_capture", _capture)
    monkeypatch.setattr(hc, "_tmux", _tmux)
    monkeypatch.setattr(hc, "_wait_for_ready", _ready)
    monkeypatch.setattr(hc, "_DISCOVERY_POLL_INTERVAL_SECONDS", 0)

    result = await hc._discover_via_throwaway_window(_agent())
    commands = [o["command"] for o in result["models"]]

    # Zeilen aus BEIDEN Ausschnitten, jede genau einmal.
    assert "gpt-5.5" in commands          # nur im oberen Ausschnitt
    assert "qwen38-27b-unsloth-nvfp4" in commands  # nur im unteren
    assert len(commands) == len(set(commands))
    assert any("Down" in k for k in keys), "ohne Blaettern bleibt der Katalog unvollstaendig"
    assert any("Escape" in k for k in keys), "der Picker muss abgebrochen werden"


# ══════════════════════════════════════════════════════════════════════════
# 2b — Der Picker darf NIE ein Modell setzen
#
# Der teuerste Befund dieser Runde, vom Operator selbst gefunden und live
# bewiesen: die Erkennung schickte eine FESTE Zahl Enter je Harness (2 fuer
# Claude Code, 1 fuer openclaude) in der Annahme, das erste bestaetige nur
# die Autovervollstaendigung. Falsch — nach einem Enter steht der Picker
# offen, und seine Fusszeile lautet woertlich:
#
#     Enter to set as default · s to use this session only · Esc to cancel
#
# Das zweite Enter setzte also das markierte Modell ALS STANDARD. Sichtbar
# blieb es nur deshalb nicht, weil der Cursor auf dem AKTIVEN Modell steht —
# die "Wahl" traf meist dasselbe wieder. Im Bestand: 41 Sitzungsdateien bei
# 8 Agenten mit "Set model to … and saved as your default".
#
# Die Tests hier nageln das Verhalten fest, das auch ein weiteres
# CLI-Update ueberlebt: sehen, dann tippen — nie andersherum.
# ══════════════════════════════════════════════════════════════════════════


def _picker_probe(monkeypatch, opens_after_enters: int):
    """Baut die CLI nach, nicht nur ihre Ausgabe: der Picker geht nach
    ``opens_after_enters`` Enter auf, und **jedes weitere Enter setzt dann
    das markierte Modell als Standard** — genau das, was die Fusszeile des
    echten Pickers ankuendigt ("Enter to set as default").

    Entscheidend ist, dass die CLI auf Tasten reagiert, OHNE dass jemand
    hinsieht. Eine erste Fassung dieses Helfers merkte sich nur, was bei
    einer Aufnahme auf dem Schirm stand — und liess die alte Fassung mit
    fester Enter-Zahl glatt durchgehen, weil zwischen ihren beiden Enter
    schlicht nie aufgenommen wurde. ``state["model_set"]`` ist darum die
    eigentliche Zusicherung dieser Tests."""
    timeline: list[str] = []
    state = {"enters": 0, "model_set": False}

    async def _tmux(slug, args):
        joined = " ".join(args)
        timeline.append(f"keys:{joined}")
        if joined.endswith("Enter"):
            if state["enters"] >= opens_after_enters:
                # Der Picker stand bereits offen: dieses Enter waehlt aus und
                # persistiert die Wahl.
                state["model_set"] = True
                timeline.append("cli:MODELL-ALS-STANDARD-GESETZT")
            state["enters"] += 1

    async def _capture(slug, window):
        opened = state["enters"] >= opens_after_enters
        timeline.append("capture:picker" if opened else "capture:prompt")
        return OPENCLAUDE_PICKER_OPEN if opened else "❯ /model"

    async def _ready(slug, window):
        return True

    monkeypatch.setattr(hc, "_tmux", _tmux)
    monkeypatch.setattr(hc, "_capture", _capture)
    monkeypatch.setattr(hc, "_wait_for_ready", _ready)
    monkeypatch.setattr(hc, "_DISCOVERY_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(hc, "_PICKER_OPEN_CONFIRM_SECONDS", 0)
    return timeline, state


@pytest.mark.parametrize("harness", ["openclaude", "claude"])
async def test_no_enter_reaches_an_open_picker(monkeypatch, harness):
    """DIE Zusicherung dieser Runde: ein Erkennungslauf darf NIE ein Modell
    setzen. Die nachgebaute CLI meldet es selbst, sobald ein Enter einen
    bereits offenen Picker trifft.

    BEIDE Harnesses, mit demselben Verhalten des Pickers (er geht nach EINEM
    Enter auf) — denn genau das war die falsche Annahme: fuer Claude Code
    stand fest verdrahtet "zwei Enter", und das zweite traf den offenen
    Picker. Ein Test nur mit openclaude (dort stand "eins") haette den
    Fehler nie gesehen."""
    timeline, state = _picker_probe(monkeypatch, opens_after_enters=1)

    await hc._discover_via_throwaway_window(_agent(harness=harness))

    assert state["model_set"] is False, (
        "ein Enter traf den offenen Picker und setzte das Modell als Standard: "
        f"{timeline}"
    )
    assert state["enters"] == 1
    assert "capture:picker" in timeline, "der Picker muss im Lauf ueberhaupt aufgehen"


async def test_a_second_enter_only_when_the_picker_stayed_shut(monkeypatch):
    """Die Gegenrichtung: bleibt der Picker nach dem ersten Enter zu (Claude
    Code faengt es mit seiner Autovervollstaendigung ab), wird sehr wohl noch
    eines geschickt. Sonst waere der Fix eine Erkennung, die bei Claude Code
    nie etwas findet."""
    timeline, state = _picker_probe(monkeypatch, opens_after_enters=2)

    result = await hc._discover_via_throwaway_window(_agent(harness="claude"))

    assert state["enters"] == 2
    assert state["model_set"] is False, timeline
    assert result["models"], "nach dem zweiten Enter muss der Katalog stehen"


async def test_picker_that_never_opens_stops_typing(monkeypatch):
    """Notbremse: oeffnet sich gar nichts, wird nicht endlos in eine Eingabe
    getippt, die uns offenbar nicht versteht — hoechstens
    ``_PICKER_OPEN_MAX_ENTERS``, dann Abbruch mit Escape."""
    timeline, state = _picker_probe(monkeypatch, opens_after_enters=99)

    result = await hc._discover_via_throwaway_window(_agent())

    assert state["enters"] == hc._PICKER_OPEN_MAX_ENTERS
    assert state["model_set"] is False
    assert result["models"] == []
    assert any(e.endswith("Escape") for e in timeline), "der Versuch muss abgeraeumt werden"


async def test_discovery_window_runs_in_its_own_working_directory(monkeypatch):
    """Zweite Haelfte desselben Befunds: die Probe legt ihre Sitzungsdatei im
    Projektordner ihres Arbeitsverzeichnisses ab. Startet sie in
    ``/home/agent``, landet sie genau dort, wo
    ``transcript_chat.resolve_transcript_dir`` das Gespraech des Agenten
    liest — und verdeckt es als neueste Datei."""
    windows: list[list[str]] = []

    async def _tmux(slug, args):
        if args and args[0] == "new-window":
            windows.append(list(args))

    async def _ready(slug, window):
        return False

    monkeypatch.setattr(hc, "_tmux", _tmux)
    monkeypatch.setattr(hc, "_wait_for_ready", _ready)

    await hc._discover_via_throwaway_window(_agent())

    assert windows, "ohne Fenster keine Erkennung"
    args = windows[0]
    assert "-c" in args, "ohne -c startet das Fenster im Heimatverzeichnis des Agenten"
    assert args[args.index("-c") + 1] == "/workspace"
    assert args[args.index("-c") + 1] != "/home/agent"


async def test_discovery_starts_the_openclaude_binary_not_claude(monkeypatch):
    """Ein ``claude``-Aufruf im openclaude-Container startet die falsche (oder
    gar keine) CLI — das Fenster wuerde nie bereit und der Katalog bliebe
    fuer immer leer."""
    started: list[str] = []

    async def _tmux(slug, args):
        if args and args[0] == "new-window":
            started.append(args[-1])

    async def _ready(slug, window):
        return False

    monkeypatch.setattr(hc, "_tmux", _tmux)
    monkeypatch.setattr(hc, "_wait_for_ready", _ready)

    await hc._discover_via_throwaway_window(_agent())
    assert started == ["openclaude --dangerously-skip-permissions"]

    started.clear()
    await hc._discover_via_throwaway_window(_agent(harness="claude"))
    assert started == ["claude --dangerously-skip-permissions"]


# ══════════════════════════════════════════════════════════════════════════
# 3 — Effort haengt am MODELL
# ══════════════════════════════════════════════════════════════════════════


async def test_parse_effort_line_reads_the_unsupported_case():
    support = hc.parse_effort_line(OPENCLAUDE_PICKER_OPEN)

    assert support["supported"] is False
    assert support["model"] == "qwen38-27b-unsloth-nvfp4"
    assert support["level"] is None


async def test_parse_effort_line_reads_the_supported_case():
    support = hc.parse_effort_line(OPENCLAUDE_PICKER_EFFORT_SUPPORTED)

    assert support["supported"] is True
    assert support["level"] == "medium"


async def test_parse_effort_line_reads_claude_arrow_spelling():
    """Claude Code schreibt ``←/→``, openclaude ``← →`` — derselbe Parser."""
    assert hc.parse_effort_line(CLAUDE_EFFORT_LINE) == {
        "supported": True, "model": None, "level": "high",
    }


async def test_parse_effort_line_unknown_when_nothing_says_so():
    """``unknown`` ist ein erstklassiger Zustand (Adapter-Kontrakt): lieber
    nichts behaupten als raten."""
    assert hc.parse_effort_line("irgendein Pane ohne Picker") == {
        "supported": None, "model": None, "level": None,
    }


async def test_discovery_harvests_effort_support_from_the_same_probe(monkeypatch):
    """Kein zweiter Wegwerf-Lauf fuer den Effort: der Picker steht beim
    OEFFNEN auf dem aktiven Modell des Agenten, also beantwortet genau die
    erste Aufnahme die Frage 'kann DIESES Modell Effort?'."""
    async def _capture(slug, window):
        return OPENCLAUDE_PICKER_OPEN

    monkeypatch.setattr(hc, "_capture", _capture)
    monkeypatch.setattr(hc, "_tmux", lambda slug, args: _noop())
    monkeypatch.setattr(hc, "_wait_for_ready", _true())
    monkeypatch.setattr(hc, "_DISCOVERY_POLL_INTERVAL_SECONDS", 0)

    result = await hc._discover_via_throwaway_window(_agent())

    assert result["effort"]["supported"] is False
    assert result["effort"]["model"] == "qwen38-27b-unsloth-nvfp4"


def _noop():
    async def _inner():
        return None
    return _inner()


def _true():
    async def _inner(slug, window):
        return True
    return _inner


# ══════════════════════════════════════════════════════════════════════════
# 4 — harness_for / resolve_cli_version
# ══════════════════════════════════════════════════════════════════════════


async def test_harness_for_accepts_openclaude():
    assert hc.harness_for(_agent()) == "openclaude"
    assert hc.harness_for(_agent(harness="claude")) == "claude"


async def test_harness_for_still_refuses_foreign_clis():
    """kimi/omp bleiben draussen: ein ``/model``-Probe in eine fremde TUI ist
    kein defekter Claude, sondern ein anderes Geraet."""
    for harness in ("kimi", "omp", None):
        assert hc.harness_for(_agent(harness=harness)) is None


async def test_resolve_cli_version_uses_the_openclaude_binary(monkeypatch):
    """``openclaude --version`` -> ``0.7.0 (OpenClaude)`` (live geprueft).
    Wichtig, weil das Transkript-Feld ``version`` bei openclaude
    ``"unknown"`` sagt und als Cache-Schluessel damit unbrauchbar ist."""
    seen = {}

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, returncode=0, stdout="0.7.0 (OpenClaude)\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    assert await hc.resolve_cli_version(_agent()) == "0.7.0"
    assert seen["argv"][-2:] == ["openclaude", "--version"]


async def test_resolve_cli_version_none_for_foreign_harness(monkeypatch):
    """Ohne bekanntes Binary wird gar nichts ausgefuehrt — nicht ``claude``
    auf gut Glueck in einen kimi-Container."""
    def _boom(argv, **kwargs):
        raise AssertionError(f"kein Aufruf erwartet: {argv}")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert await hc.resolve_cli_version(_agent(harness="kimi")) is None


# ══════════════════════════════════════════════════════════════════════════
# 5 — Cache: getrennt je Harness
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
async def redis_env(fake_redis, monkeypatch):
    monkeypatch.setattr(redis_client_mod, "_redis", fake_redis)
    return fake_redis


async def test_catalog_cache_does_not_leak_between_harnesses(monkeypatch, redis_env):
    """Beide Harnesses koennen dieselbe Versionsnummer tragen — der
    Harness-Name gehoert darum in den Schluessel (und tut es bereits)."""
    async def _version(a):
        return "0.7.0"

    monkeypatch.setattr(hc, "resolve_cli_version", _version)
    await redis_env.set(
        hc.RedisKeys.model_catalog("claude", "0.7.0", "openclaude-agent"),
        json.dumps([{"command": "opus", "label": "Opus"}]),
    )

    calls = {"n": 0}

    async def _discover(a):
        calls["n"] += 1
        return {"models": [{"command": "haiku", "label": "Haiku"}], "effort": {"supported": None, "model": None, "level": None}}

    monkeypatch.setattr(hc, "_discover_via_throwaway_window", _discover)

    assert await hc.discover_model_catalog(_agent()) == [{"command": "haiku", "label": "Haiku"}]
    assert calls["n"] == 1


async def test_effort_answer_is_not_cached_across_models(monkeypatch, redis_env):
    """BEFUND 2: der Cache-Schluessel trug 24h lang KEIN Modell, die Antwort
    haengt aber am Modell. Nach ``/model gpt-5.2-codex`` meldete der Chip
    darum weiter ``canSwitchEffort=false`` fuer ein Modell, das Stufen kennt
    — und umgekehrt blieb der Regler nach dem Wechsel auf ein Modell OHNE
    Effort aktiv und schrieb eine Stufe, die dieses Modell ignoriert."""
    async def _version(a):
        return "0.7.0"

    monkeypatch.setattr(hc, "resolve_cli_version", _version)

    runs: list[str | None] = []
    answers = {
        "qwen38-27b-unsloth-nvfp4": {"supported": False, "model": "qwen38-27b-unsloth-nvfp4", "level": None},
        "gpt-5.2-codex": {"supported": True, "model": None, "level": "medium"},
    }
    current = {"model": "qwen38-27b-unsloth-nvfp4"}

    async def _discover(agent):
        runs.append(current["model"])
        return {"models": [{"command": "x", "label": "X"}], "effort": answers[current["model"]]}

    monkeypatch.setattr(hc, "_discover_via_throwaway_window", _discover)
    agent = _agent()

    first = await hc.discover_effort_support(agent, current["model"])
    assert first["supported"] is False

    # Zweiter Aufruf mit DEMSELBEN Modell: der Cache greift, kein zweites
    # Wegwerf-Fenster.
    assert (await hc.discover_effort_support(agent, current["model"]))["supported"] is False
    assert runs == ["qwen38-27b-unsloth-nvfp4"]

    # Modellwechsel — die alte Antwort darf jetzt NICHT mehr gelten.
    current["model"] = "gpt-5.2-codex"
    after = await hc.discover_effort_support(agent, current["model"])

    assert after["supported"] is True, "die Antwort des alten Modells hielt sich"
    assert runs == ["qwen38-27b-unsloth-nvfp4", "gpt-5.2-codex"]


async def test_cache_key_carries_the_model(redis_env):
    """Die Sperre bleibt bewusst OHNE Modell (sie schuetzt das eine
    Wegwerf-Fenster je Container), der Eintrag traegt es — und kein
    Modellname kann je den Sperr-Schluessel treffen."""
    a = hc.RedisKeys.model_catalog("openclaude", "0.7.0", "openclaude-agent", "gpt-5.2-codex")
    b = hc.RedisKeys.model_catalog("openclaude", "0.7.0", "openclaude-agent", "qwen38-27b")
    lock = hc.RedisKeys.model_catalog_discovery_lock("openclaude", "0.7.0", "openclaude-agent")

    assert a != b
    assert hc.RedisKeys.model_catalog("openclaude", "0.7.0", "openclaude-agent", "discovery-lock") != lock


async def test_legacy_cached_list_is_still_readable(monkeypatch, redis_env):
    """Vor dieser Runde lag eine blanke Liste im Cache. Nach dem Deploy darf
    ein solcher Eintrag nicht als Fehler enden — er hat nur keine
    Effort-Aussage."""
    async def _version(a):
        return "2.1.234"

    monkeypatch.setattr(hc, "resolve_cli_version", _version)
    await redis_env.set(
        hc.RedisKeys.model_catalog("claude", "2.1.234", "rex"),
        json.dumps([{"command": "opus", "label": "Opus"}]),
    )

    agent = _agent(slug="rex", harness="claude")
    assert await hc.discover_model_catalog(agent) == [{"command": "opus", "label": "Opus"}]
    assert (await hc.discover_effort_support(agent))["supported"] is None


# ══════════════════════════════════════════════════════════════════════════
# 6 — agent_chat_input: Stufen, Gate, Grund, Slash-Kommandos
# ══════════════════════════════════════════════════════════════════════════


async def test_openclaude_effort_levels_exclude_ultracode():
    """Live erhoben mit dem persistenzfreien Trick aus dem Adapter-Skill:
    ``/effort zzz`` -> ``Invalid argument: zzz. Valid options are: low,
    medium, high, max, xhigh, auto``. Claude Codes ``ultracode`` fehlt —
    wer die Claude-Liste weiterreicht, bietet einen Regler mit einer Stufe
    an, die diese CLI zurueckweist."""
    levels = agent_chat_input.effort_levels_for("openclaude")

    assert "ultracode" not in levels
    assert set(levels) == {"low", "medium", "high", "xhigh", "max"}
    assert "auto" not in levels  # loescht den Override, ist keine Stufe
    assert "ultracode" in agent_chat_input.effort_levels_for("claude")


def _stub_effort_support(monkeypatch, tmp_path, **support):
    """Stubbt ALLE drei Wege in die echte Flotte, die ``effort_capabilities``
    sonst nimmt.

    Ohne den dritten (``resolve_cli_version``) fuehrte diese Testdatei auf
    Marks Rechner ``docker exec -u agent mc-agent-openclaude-agent openclaude
    --version`` aus: der Container existiert dort, die Suite griff also in
    die laufende Flotte. ``_check_effort_levels_version_drift`` laeuft VOR
    dem Effort-Stub und geht an ihm vorbei — genau der "Real-Host-Leak",
    gegen den ``test_agent_chat_router.py`` bereits stubbt."""
    async def _support(agent, model=None):
        return {"supported": None, "model": None, "level": None, **support}

    async def _no_version(agent):
        return None

    monkeypatch.setattr(agent_chat_input, "discover_effort_support", _support)
    monkeypatch.setattr(agent_chat_input, "resolve_cli_version", _no_version)
    monkeypatch.setattr(agent_chat_input, "_host_home", lambda: tmp_path)


async def test_effort_capabilities_openclaude_model_without_effort_explains_why(monkeypatch, tmp_path):
    """Der Kern der Aufgabe: NICHT nur ausblenden. Der Agent bekommt keinen
    Regler (sein Modell kennt keine Stufen), aber einen GRUND, den das UI
    vorlesen kann. Die Stufenleiter kommt trotzdem mit — sie beschreibt den
    Harness, nicht das Schaltrecht (bestehende Semantik des read-only-Chips)."""
    _stub_effort_support(
        monkeypatch, tmp_path, supported=False, model="qwen38-27b-unsloth-nvfp4"
    )

    caps = await agent_chat_input.effort_capabilities(_agent())

    assert caps["canSwitchEffort"] is False
    assert caps["effortReason"] == "model_no_effort"
    assert caps["effortLevels"] == ["low", "medium", "high", "xhigh", "max"]
    # Das Modell, ueber das die CLI die Aussage gemacht hat, faehrt mit —
    # sonst setzt der Erklaertext im Frontend das GERADE angezeigte Modell
    # ein und behauptet ueber es etwas, das nur fuers alte gemessen wurde.
    assert caps["effortModel"] == "qwen38-27b-unsloth-nvfp4"


async def test_model_without_effort_reports_no_level_at_all(monkeypatch, tmp_path):
    """BEFUND 4: die persistierte Stufe darf hier NICHT mitkommen. Das
    Frontend fuellt daraus die Saeule des Chips — ein Agent mit
    ``effortLevel: "xhigh"`` in der settings.json zeigte auf einem Modell
    ohne Effort eine zu 100 % gefuellte Saeule und ``data-level="xhigh"``.
    Der Chip behauptete damit eine Stufe, die das Modell ignoriert; genau
    die luegende Anzeige, gegen die das Tor gebaut wurde."""
    _stub_effort_support(monkeypatch, tmp_path, supported=False, model="qwen38-27b")
    cfg = tmp_path / ".mc" / "agents" / "openclaude-agent" / "claude-config"
    cfg.mkdir(parents=True)
    (cfg / "settings.json").write_text(json.dumps({"effortLevel": "xhigh"}))

    caps = await agent_chat_input.effort_capabilities(_agent())

    assert caps["effort"] is None, "eine wirkungslose Stufe darf die Saeule nicht fuellen"
    assert caps["effortReason"] == "model_no_effort"


async def test_effort_capabilities_openclaude_model_with_effort_can_switch(monkeypatch, tmp_path):
    _stub_effort_support(
        monkeypatch, tmp_path, supported=True, model="gpt-5.2-codex", level="medium"
    )

    caps = await agent_chat_input.effort_capabilities(_agent())

    assert caps["canSwitchEffort"] is True
    assert caps["effortReason"] is None


async def test_effort_capabilities_reads_persisted_level_for_openclaude(monkeypatch, tmp_path):
    """openclaude nutzt denselben Config-Mount
    (``~/.mc/agents/<slug>/claude-config`` -> ``/home/agent/.claude``) — live
    gegengeprueft, indem ``/effort low`` im Container die Datei schrieb
    (danach zurueckgesetzt)."""
    _stub_effort_support(monkeypatch, tmp_path, supported=True, model="gpt-5.2-codex")

    cfg = tmp_path / ".mc" / "agents" / "openclaude-agent" / "claude-config"
    cfg.mkdir(parents=True)
    (cfg / "settings.json").write_text(json.dumps({"effortLevel": "xhigh"}))
    assert (await agent_chat_input.effort_capabilities(_agent()))["effort"] == "xhigh"

    # ultracode kennt openclaude NICHT — ein solcher Wert in der Datei darf
    # nicht als gueltige Stufe durchgereicht werden.
    (cfg / "settings.json").write_text(json.dumps({"effortLevel": "ultracode"}))
    assert (await agent_chat_input.effort_capabilities(_agent()))["effort"] is None


async def test_set_effort_accepts_openclaude_and_its_levels(monkeypatch):
    """Das harte Gate liess frueher NUR ``claude`` durch. Live bewiesen, dass
    openclaude die Argumentform kennt: ``/effort low`` ->
    ``Set effort level to low: Quick, straightforward implementation with
    minimal overhead`` und ``effortLevel: "low"`` in der settings.json
    (danach zurueckgesetzt). Der Bestaetigungs-Marker
    (``effort level to <stufe>``) traegt also unveraendert."""
    sent = _stub_set_effort(monkeypatch, supported=True, model="gpt-5.2-codex")

    await agent_chat_input.set_effort(_agent(), "low")
    assert any("/effort low" in a for a in sent[0])

    # ultracode ist Claude-Vokabular — openclaude wuerde es zurueckweisen,
    # also gar nicht erst tippen.
    with pytest.raises(ValueError):
        await agent_chat_input.set_effort(_agent(), "ultracode")


def _stub_set_effort(monkeypatch, **support):
    """Alle I/O-Wege von ``set_effort`` abgefangen — inklusive des
    Modell-Tors, das sonst ein echtes Wegwerf-Fenster im Container von
    der openclaude-Agent oeffnen wuerde."""
    sent: list[list[str]] = []

    async def _run(argv):
        sent.append(argv)

    async def _busy(agent):
        return False

    async def _verified(agent, level):
        return True

    async def _support(agent, model=None):
        return {"supported": None, "model": None, "level": None, **support}

    def _model(slug):
        return support.get("model")

    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _run)
    monkeypatch.setattr(agent_chat_input, "_pane_is_busy", _busy)
    monkeypatch.setattr(agent_chat_input, "_verify_effort_applied", _verified)
    monkeypatch.setattr(agent_chat_input, "discover_effort_support", _support)
    monkeypatch.setattr(agent_chat_input, "_persisted_model", _model)
    return sent


async def test_set_effort_refuses_a_model_without_effort_levels(monkeypatch):
    """BEFUND 3: der Docstring nannte sich "Spiegel des Capabilities-Gates",
    das MODELL-Tor fehlte dort aber. Ein direkter
    ``POST /chat/effort {"level":"high"}`` kam bei einem Agenten auf einem
    Modell ohne Effort-Stufen durch beide bisherigen Pruefungen und tippte
    ``/effort high`` in den Live-Pane: die Stufe landet in der settings.json,
    das Modell ignoriert sie."""
    sent = _stub_set_effort(monkeypatch, supported=False, model="qwen38-27b-unsloth-nvfp4")

    with pytest.raises(agent_chat_input.InputNotSupportedError):
        await agent_chat_input.set_effort(_agent(), "high")

    assert sent == [], "es darf KEIN Tastendruck in den Live-Pane gegangen sein"


async def test_set_effort_still_works_when_the_probe_is_unsure(monkeypatch):
    """``supported=None`` (kalter Cache, Sperre, Probe gescheitert) bleibt
    schaltbar — identisch zum Capabilities-Gate. "Noch nicht ermittelt" ist
    kein Grund, einen funktionierenden Wechsel abzulehnen; das war der
    Zustand der ganzen Flotte vor dieser Runde."""
    sent = _stub_set_effort(monkeypatch, supported=None)

    await agent_chat_input.set_effort(_agent(), "low")

    assert any("/effort low" in a for a in sent[0])


async def test_slash_commands_openclaude_has_its_own_builtins():
    """openclaude bringt eine eigene Builtin-Liste mit (live durchgeblaettert)
    — Claude Codes acht Kommandos sind hier zu wenig UND teils falsch."""
    caps = await agent_chat_input.slash_command_capabilities(_agent())
    names = {c["name"] for c in caps["slashCommands"]}

    for own in ("btw", "buddy", "provider", "rewind", "wiki", "insights"):
        assert own in names, f"/{own} fehlt — openclaude-eigenes Builtin"
    for shared in ("model", "effort", "clear", "compact", "context", "status", "help", "resume"):
        assert shared in names
    # Installierte Skills kommen ueber die Skill-Erkennung, nicht ueber die
    # statische Liste — sonst behaupten wir Skills, die dieser Agent nicht hat.
    for skill in ("pdf", "xlsx", "brand-guidelines", "webapp-testing"):
        assert skill not in names


async def test_model_options_openclaude_cold_cache_offers_nothing(monkeypatch, tmp_path):
    """Der statische Rueckfall (``settings.model_aliases``) ist
    Claude-Vokabular. Bei openclaude waere er geraten — dann lieber ein
    leeres Dropdown als eine Auswahl, die den Agenten kaputtschaltet."""
    async def _empty(agent, model=None):
        return []

    async def _windows():
        return {}

    monkeypatch.setattr(agent_chat_input, "discover_model_catalog", _empty)
    monkeypatch.setattr(agent_chat_input, "get_observed_model_windows", _windows)
    monkeypatch.setattr(agent_chat_input, "_host_home", lambda: tmp_path)

    caps = await agent_chat_input.model_options_capabilities(_agent())
    assert caps["modelOptions"] == []


async def test_send_text_gates_on_readiness_for_openclaude(monkeypatch):
    """Die Bereitschafts-Marker sind bei openclaude identisch — live
    gegengeprueft am ECHTEN Pane eines openclaude-Agenten (nur gelesen, nichts
    getippt): ``parse_pane_state`` liefert dort ``idle``. Damit schuetzt das
    Gate auch hier vor einem Send in eine noch bootende TUI, statt wie bei
    fremden CLIs jede Nachricht mit 409 abzuweisen."""
    gated = {"n": 0}

    async def _gate(agent):
        gated["n"] += 1

    async def _run(argv):
        return None

    async def _touch(slug):
        return None

    monkeypatch.setattr(agent_chat_input, "_wait_for_send_readiness", _gate)
    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _run)
    monkeypatch.setattr(agent_chat_input, "_touch_recycler_marker", _touch)

    await agent_chat_input.send_text(_agent(), "hallo")
    assert gated["n"] == 1

    # omp bekommt das Tor seit der Zusammenfuehrung EBENFALLS — nicht weil
    # seine TUI Claude-Marker zeigt, sondern weil es einen eigenen Adapter
    # mit eigener Sonde hat; das Tor liest mit DESSEN Regeln. Vorher stand
    # hier das Gegenteil, und das stimmte auch: solange omp keine Sonde
    # hatte, waere jede Aussage ``unknown`` und damit eine Ablehnung ohne
    # Erkenntnis gewesen.
    await agent_chat_input.send_text(_agent(harness="omp"), "hallo")
    assert gated["n"] == 2

    # Kimi hat weiterhin KEINEN Adapter — und bekommt darum kein Tor.
    # Das ist die Absicherung gegen eine handgepflegte Liste: waere das Tor
    # nicht aus der Registrierung abgeleitet, wuerde hier still gegen
    # Claude-Regeln geprueft, die Kimis TUI nie erfuellt.
    await agent_chat_input.send_text(_agent(harness="kimi"), "hallo")
    assert gated["n"] == 2
