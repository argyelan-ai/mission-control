"""Harness catalog — the adapter-contract generalization pattern for
"what models/levels does this CLI actually support", replacing hardcoded
alias/window maps with dynamic, observed, cached discovery.

Two independent Redis-backed layers:

1. MODEL CATALOG (``discover_model_catalog``): the ``/model`` picker's own
   rows (command token + label), discovered from a THROWAWAY tmux window —
   NEVER the agent's own live session (see ``agent_chat_input``'s and
   ``pane_state``'s module docstrings for why a working session must never
   be disturbed; the same rule applies here even though this is a read-only
   discovery, not a switch — opening `/model` in a REAL session would leave
   a picker open in front of the operator/agent). Cached in Redis keyed by
   ``(harness, cli_version)`` so a CLI upgrade invalidates the old catalog
   automatically instead of silently serving stale rows forever.
   ``app.config.settings.model_aliases`` is the FALLBACK ONLY — served when
   the catalog is empty (cold cache, discovery not finished yet, or
   discovery genuinely failed) — never the primary source once a catalog
   exists.
2. OBSERVED WINDOW MAP (``observe_model_window`` /
   ``get_observed_model_windows``): every FRESH statusline-state read
   already tells us ``(model.id, context_window_size)`` for whatever model
   actually served that turn — persisted to one shared Redis hash, newest
   write wins (plain HSET, no versioning). This becomes the MIDDLE tier of
   ``transcript_chat.resolve_context_window``'s precedence chain:
   current-session statusline (stamped separately, per-event, by
   ``transcript_chat._stamp_usage_source``) > this observed map > the
   static config seed (``settings.context_windows``) > ``None``.
   ``transcript_chat.py`` does NOT import this module for the read side —
   callers (the router, the tailer) fetch the observed map themselves and
   pass it into ``resolve_context_window``/``read_history`` as a plain
   dict, keeping the parser's pure-function chain free of a Redis
   dependency and avoiding a circular import (the tailer, which writes
   observations, lives inside ``transcript_chat.py``).

Docker/cli-bridge only (v1) — ``harness_for`` returns ``None`` for every
other runtime, the same boundary ``agent_chat_input``'s capability
functions already enforce (no pane/tmux to discover from).

**Zwei Harnesses, ein Mechanismus (19.08.2026).** ``openclaude`` ist ein
Claude-Code-Fork: Transkript, Eingabe und Zustands-Sonde tragen unveraendert,
die Capabilities-Schicht nicht. Vier Unterschiede, alle live erhoben und in
den Konstanten unten dokumentiert:

1. Anderes Binary UND andere Versionsquelle — das Transkript-Feld ``version``
   sagt bei openclaude ``"unknown"``, ``openclaude --version`` dagegen
   ``0.7.0 (OpenClaude)``.
2. Scroll-Marker ``↑``/``↓`` in der Cursor-Spalte (``_MODEL_ROW_RE``).
3. Die Liste ist laenger als der Picker-Ausschnitt — sie muss GEBLAETTERT
   werden, sonst liefert eine einzelne ``capture-pane`` einen stillen
   Teilkatalog (``_PICKER_MORE_RE`` liefert die Sollzahl).
4. Der Picker OEFFNET nach EINEM Enter, nicht nach zweien — ein zweites
   Enter waehlt bereits aus, und die Wahl persistiert.

Und ein fuenfter, der ueber diesen Fork hinausgeht: **Effort haengt am
MODELL, nicht am Harness.** ``parse_effort_line`` liest die Aussage aus
demselben Picker-Pane, aus dem der Katalog kommt — ein zweiter Probelauf
waere ein zweites Wegwerf-Fenster fuer eine Information, die schon auf dem
Schirm stand. Darum liefert ``discover_harness_capabilities`` beides, und
``discover_model_catalog``/``discover_effort_support`` sind nur die zwei
Sichten darauf.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from typing import Any

from app.redis_client import RedisKeys, get_redis

logger = logging.getLogger("mc.harness_catalog")

_CATALOG_TTL_SECONDS = 24 * 3600  # 24h — see module docstring
_DISCOVERY_LOCK_TTL_SECONDS = 60  # generous vs. the few seconds discovery takes
_DISCOVERY_WINDOW_NAME = "mc-catalog-discovery"
_DISCOVERY_READY_TIMEOUT_SECONDS = 8
_DISCOVERY_POLL_INTERVAL_SECONDS = 0.3
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")

# Welches Binary gehoert zu welchem Harness. Ein Harness ohne Eintrag wird
# NIE angefasst — lieber kein Katalog als ``claude --version`` auf gut Glueck
# in einen kimi-/omp-Container (das Gate von 18.08.2026, jetzt als Tabelle).
# ``openclaude --version`` -> ``0.7.0 (OpenClaude)`` (live 19.08.2026); das
# Transkript-Feld ``version`` sagt bei openclaude ``"unknown"`` und taugt
# darum NICHT als Cache-Schluessel.
_HARNESS_BINARIES = {"claude": "claude", "openclaude": "openclaude"}

# Wie oft Enter noetig ist, damit sich der ``/model``-Picker OEFFNET.
# Claude Code: das erste Enter nimmt den Autocomplete-Vorschlag an, erst das
# zweite oeffnet. openclaude: EIN Enter oeffnet — das zweite waehlt bereits
# aus (live 19.08.2026 an ``/effort`` gesehen: Enter+Enter quittierte
# prompt "Set effort level to auto"). Ein Fehlgriff hier waere teuer, denn
# die Modellwahl persistiert ("Applies to this session and future OpenClaude
# sessions").
_PICKER_OPEN_ENTERS = {"claude": 2, "openclaude": 1}

# Der Picker zeigt einen FESTEN Ausschnitt von ~10 Zeilen — auch in einem
# 200x60-Fenster (live gegengeprueft). Geblaettert wird mit Down; die Liste
# laeuft am Ende um (von Zeile 7 fuehrten 9x Down auf 16, weitere 9x auf 9),
# darum genuegt EINE Richtung. Schrittweite knapp unter der Fensterhoehe,
# damit sich die Ausschnitte ueberlappen.
_PICKER_PAGE_STEP = 9
_PICKER_MAX_PAGE_ROUNDS = 8

# A /model picker row: an optional pointer/scroll marker, a number + period,
# then the label (the CURRENTLY active one carries a trailing "✔"), then the
# CLI's own column separator (2+ spaces, mirrored from
# pane_state._LABEL_SPLIT_RE) and a description we don't need here.
#
# ⚠️ Die Marker-Klasse ist NICHT kosmetisch: openclaude setzt ``↑``/``↓`` in
# dieselbe Spalte wie den Cursor ``❯``, sobald die Liste laenger als der
# Ausschnitt ist — also auf der ersten und letzten sichtbaren Zeile JEDER
# Aufnahme. Das alte Muster (nur ``❯``) verwarf genau die still.
_MODEL_ROW_RE = re.compile(
    r"^\s*(?:[❯↑↓>]\s*)*(?P<index>\d+)\.\s+(?P<label>\S.*?)(?:\s{2,}\S.*)?$"
)

# Fusszeile des Pickers: die Zahl der NICHT sichtbaren Zeilen. ``total =
# sichtbar + N`` — die einzige ehrliche Sollzahl, an der der Sammler merkt,
# ob er fertig ist (live nachgerechnet: 10 sichtbar + ``and 6 more…`` = 16,
# und der obere Ausschnitt zeigte dieselben 6).
_PICKER_MORE_RE = re.compile(r"\band\s+(\d+)\s+more")

# Die Effort-Zeile UNTER der Liste. Sie beschreibt die MARKIERTE Zeile —
# beim Oeffnen also das aktive Modell des Agenten. Beide Auspraegungen live
# gesehen (openclaude 0.7.0):
#   ``○ Effort not supported for qwen38-27b-unsloth-nvfp4``
#   ``◐ Medium effort (default) ← → to adjust``
# Claude Code schreibt die zweite als ``● High effort (default) ←/→ to adjust``.
_EFFORT_UNSUPPORTED_RE = re.compile(
    r"Effort not supported for\s+(?P<model>.+?)\s*$", re.MULTILINE
)
_EFFORT_LEVEL_RE = re.compile(r"(?P<level>[A-Za-z]+)\s+effort\b.*?to adjust")

# Known alias labels (lowercased) -> their /model command token. A row whose
# label does NOT match one of these (a local/custom model, e.g.
# "Qwen/Qwen3.6-35B-A3B-FP8") uses its own raw label as the command token
# verbatim — that IS the valid --model argument for those, live-verified
# together with the alias tokens below (Phase-0 discovery, 2026-08-18:
# `/model opus` as a direct argument persisted `"model":"opus"` into
# settings.json — the short alias token, not a full model id).
_KNOWN_ALIAS_COMMANDS = {
    "default": "default",
    "default (recommended)": "default",
    "sonnet": "sonnet",
    "opus": "opus",
    "haiku": "haiku",
}

# openclaude beschriftet seine Zeilen anders ("Opus 4.1", "Sonnet (1M
# context)"), und ein falsches Token ist hier kein Schoenheitsfehler: die CLI
# VALIDIERT (``/model zzz-not-a-model`` -> ``Model 'zzz-not-a-model' not
# found``), aber was sie annimmt, persistiert sofort in die settings.json des
# Agenten — ein geratenes Token schaltet also einen echten Agenten auf ein
# Modell, das niemand wollte.
#
# Darum wurde JEDES Token hier live gegengeprueft, in einem Wegwerf-Fenster
# mit EIGENEM ``CLAUDE_CONFIG_DIR`` (die echte settings.json des Agenten war
# dabei nachweislich unangetastet, md5 vorher==nachher):
#   ``default``  -> "Set model to … (default)", der ``model``-Schluessel
#                   verschwindet aus der Datei — genau die Bedeutung der Zeile
#   ``opus``     -> das ``✔`` landet auf Zeile 14 "Opus 4.1"
#   ``haiku``    -> das ``✔`` landet auf Zeile 15 "Haiku"
#   ``sonnet``   -> gueltig und persistiert
#
# ⚠️ EINE Unsicherheit bleibt bewusst stehen: ``sonnet`` erscheint danach als
# "16. sonnet ✔  Custom model", NICHT als Zeile 12 "Sonnet". Das Token ist
# also ein echtes, funktionierendes Sonnet — aber unbewiesen, ob es dieselbe
# Variante meint wie die Zeile. Weil es weder validierungs- noch
# betriebsgefaehrlich ist (gueltiges Modell), bleibt es drin.
# ``Sonnet (1M context)`` dagegen hat KEIN belegtes Token (``sonnet (1m
# context)`` und ``sonnet-1m`` -> "not found"; ``sonnet[1m]`` wird zwar
# angenommen, erscheint aber ebenfalls als "Custom model") — die Zeile faellt
# darum unten heraus und wird protokolliert, nicht geraten.
_OPENCLAUDE_ALIAS_COMMANDS = {
    "default": "default",
    "default (recommended)": "default",
    "sonnet": "sonnet",
    "opus": "opus",
    "opus 4.1": "opus",
    "haiku": "haiku",
}

_HARNESS_ALIAS_COMMANDS = {
    "claude": _KNOWN_ALIAS_COMMANDS,
    "openclaude": _OPENCLAUDE_ALIAS_COMMANDS,
}

# Was als blanke Modell-ID durchgeht und darum sein eigenes Token IST.
# Belegt an der Zeile des Agenten selbst: "qwen38-27b-unsloth-nvfp4" steht
# wortgleich als ``model`` in seiner settings.json. Alles mit Leerzeichen
# oder Klammern ist eine ANZEIGE-Beschriftung, kein Argument.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\\@+-]*$")


def harness_for(agent) -> str | None:
    """Which harness catalog applies to this agent, or ``None`` if none
    does. v1: docker/cli-bridge only — same boundary
    ``agent_chat_input.effort_capabilities``/``slash_command_capabilities``
    already enforce (no pane/tmux to discover from for any other runtime).

    Kritischer Zusatz (18.08.2026, kritischer Test-Durchgang): NUR fuer
    Harnesses mit bekanntem Binary (siehe ``_HARNESS_BINARIES``). Vorher galt
    JEDER cli-bridge-Agent als Claude — Kimi (kimi-CLI) und Sparky (omp)
    haetten damit einen /model-Picker-Probe in eine fremde TUI bekommen und
    canSwitchEffort=true gemeldet, worauf ein Klick /effort-Kommandos in eine
    CLI getippt haette, die sie nicht kennt. Eine fremde CLI ist kein
    defekter Claude, sondern ein anderes Gerät.

    Seit 19.08.2026 gehoert ``openclaude`` dazu — ein Claude-Code-Fork mit
    demselben Transkript-Format, aber EIGENEM Katalog, eigener Stufenliste
    und eigenem Zeilenformat (darum ein eigener Harness-Name, kein Alias
    auf "claude")."""
    runtime = getattr(agent, "agent_runtime", None)
    slug = getattr(agent, "slug", None)
    harness = getattr(agent, "harness", None)
    if runtime == "cli-bridge" and slug and harness in _HARNESS_BINARIES:
        return harness
    return None


def _parse_picker_page(
    pane_text: str, harness: str = "claude", dropped: list[str] | None = None
) -> tuple[dict[int, dict[str, str]], int | None, set[int]]:
    """Eine EINZELNE Picker-Aufnahme -> ``({zeilennummer: {"command","label"}},
    versteckte_zeilen, gesehene_zeilennummern)``.

    Die Zeilennummer ist der Schluessel, weil sie ueber Aufnahmen hinweg
    stabil ist: beim Blaettern ueberlappen sich die Ausschnitte, und nur der
    Index sagt zuverlaessig, ob zwei Zeilen dieselbe sind.

    ``versteckte_zeilen`` ist ``None``, wenn die Liste ganz in den Ausschnitt
    passt (keine ``and N more…``-Fusszeile) — dann gibt es nichts zu
    blaettern.

    ``gesehene_zeilennummern`` zaehlt AUCH die Zeilen, deren Kommando-Token
    unbelegt ist und die darum aus ``rows`` fallen. Nur so stimmt die
    Vollstaendigkeits-Rechnung ``total = sichtbar + versteckt``: wer nur die
    verwertbaren Zeilen zaehlt, unterschaetzt die Gesamtzahl und hoert zu
    frueh auf zu blaettern."""
    aliases = _HARNESS_ALIAS_COMMANDS.get(harness, _KNOWN_ALIAS_COMMANDS)
    rows: dict[int, dict[str, str]] = {}
    seen: set[int] = set()
    for line in pane_text.splitlines():
        m = _MODEL_ROW_RE.match(line)
        if m is None:
            continue
        seen.add(int(m.group("index")))
        raw_label = m.group("label").strip()
        label = raw_label.replace("✔", "").strip()
        label = re.sub(r"\s*\(recommended\)\s*$", "", label, flags=re.IGNORECASE).strip()
        command = aliases.get(label.lower())
        if command is None:
            # Kein bekannter Alias — nur eine blanke Modell-ID ist ihr
            # eigenes Token. Eine Anzeige-Beschriftung ("Sonnet (1M
            # context)") waere geraten, und Raten schaltet hier echte
            # Agenten um: die Zeile faellt heraus (der Aufrufer
            # protokolliert sie).
            candidate = raw_label.replace("✔", "").strip()
            if not _MODEL_ID_RE.match(candidate):
                if dropped is not None and label not in dropped:
                    dropped.append(label)
                continue
            command = candidate
        rows[int(m.group("index"))] = {"command": command, "label": label}

    more = _PICKER_MORE_RE.search(pane_text)
    return rows, (int(more.group(1)) if more else None), seen


def parse_model_picker(pane_text: str, harness: str = "claude") -> list[dict[str, str]]:
    """Pure parser: a captured ``/model`` picker pane -> ``[{"command":str,
    "label":str}, ...]``, one entry per numbered row (in row order), skipping
    the header/footer/effort-row lines that don't match the row shape at all.
    See ``_KNOWN_ALIAS_COMMANDS``/``_OPENCLAUDE_ALIAS_COMMANDS`` for the
    label->command derivation; the ``✔`` "currently active" marker and any
    trailing ``(recommended)`` suffix are stripped from the label used for
    BOTH the command lookup and the display label itself.

    Nur EIN Ausschnitt. Fuer den vollstaendigen Katalog blaettert
    ``_discover_via_throwaway_window`` und fuegt die Ausschnitte zusammen."""
    rows, _, _ = _parse_picker_page(pane_text, harness)
    return [rows[i] for i in sorted(rows)]


def parse_effort_line(pane_text: str) -> dict[str, object]:
    """Die Effort-Zeile des ``/model``-Pickers -> ``{"supported": bool|None,
    "model": str|None, "level": str|None}``.

    **Effort haengt am MODELL, nicht am Harness** — der teuerste Befund
    dieser Runde. Derselbe openclaude-Agent meldet fuer sein Spark-Modell
    ``○ Effort not supported for qwen38-27b-unsloth-nvfp4`` und eine
    Cursor-Zeile weiter fuer ``gpt-5.2-codex`` ``◐ Medium effort (default)
    ← → to adjust``.

    ``supported=None`` heisst ehrlich "steht nicht drin" (Adapter-Kontrakt:
    ``unknown`` ist ein erstklassiger Zustand) — nie "geht nicht"."""
    unsupported = _EFFORT_UNSUPPORTED_RE.search(pane_text)
    if unsupported:
        return {"supported": False, "model": unsupported.group("model").strip(), "level": None}
    level = _EFFORT_LEVEL_RE.search(pane_text)
    if level:
        return {"supported": True, "model": None, "level": level.group("level").lower()}
    return {"supported": None, "model": None, "level": None}


_UNKNOWN_EFFORT: dict[str, object] = {"supported": None, "model": None, "level": None}


async def resolve_cli_version(agent) -> str | None:
    """CLI version for cache-keying — ``docker exec -u agent
    mc-agent-{slug} <binary> --version``, parsed for a ``N.N.N`` pattern
    (real output: ``"2.1.234 (Claude Code)"`` / ``"0.7.0 (OpenClaude)"``).
    Das Binary kommt aus ``_HARNESS_BINARIES``; ein Harness ohne Eintrag
    fuehrt GAR NICHTS aus. Docker/cli-bridge only; ``None`` on any failure
    (container gone, unexpected output) — the caller treats a missing
    version the same as a cache miss it can't key, forcing fresh discovery
    rather than risking a wrong cache hit."""
    slug = getattr(agent, "slug", None)
    runtime = getattr(agent, "agent_runtime", None)
    binary = _HARNESS_BINARIES.get(getattr(agent, "harness", None))
    if runtime != "cli-bridge" or not slug or binary is None:
        return None

    argv = ["docker", "exec", "-u", "agent", f"mc-agent-{slug}", binary, "--version"]
    try:
        result = await asyncio.to_thread(
            subprocess.run, argv, capture_output=True, text=True, timeout=5
        )
    except Exception:
        logger.warning("harness_catalog: version check failed for slug=%s", slug, exc_info=True)
        return None

    if result.returncode != 0:
        return None
    m = _VERSION_RE.search(result.stdout or "")
    return m.group(0) if m else None


async def discover_harness_capabilities(agent) -> dict[str, object]:
    """Der EINE Wegwerf-Lauf, aus dem beide Fragen beantwortet werden:
    ``{"models": [{"command","label"}, ...], "effort": {"supported","model",
    "level"}}``.

    Warum zusammen: der ``/model``-Picker steht beim Oeffnen auf dem aktiven
    Modell des Agenten UND schreibt darunter, ob genau dieses Modell Effort
    kann. Die Effort-Antwort faellt also gratis ab — ein zweiter
    ``/effort``-Probelauf waere ein zweites Fenster fuer eine Information,
    die schon auf dem Schirm stand.

    Redis-cached by ``(harness, cli_version, slug)``, frisch entdeckt
    (Wegwerf-Fenster, nie die eigene Session des Agenten) bei Cache-Miss.
    Liefert leere Modelle und ``supported=None``, wenn: die Runtime keinen
    Harness hat (Boss/Host), die CLI-Version nicht ermittelbar ist, ein
    anderer Request gerade entdeckt (Lock) oder die Entdeckung scheitert.
    Wirft nie."""
    empty: dict[str, object] = {"models": [], "effort": dict(_UNKNOWN_EFFORT)}

    harness = harness_for(agent)
    if harness is None:
        return empty
    slug = agent.slug

    cli_version = await resolve_cli_version(agent)
    if cli_version is None:
        return empty

    redis = await get_redis()
    cache_key = RedisKeys.model_catalog(harness, cli_version, slug)
    try:
        cached = await redis.get(cache_key)
    except Exception:
        cached = None
    if cached:
        try:
            return _normalize_cached(json.loads(cached))
        except (json.JSONDecodeError, ValueError):
            pass  # fall through to a fresh discovery — corrupt cache entry

    lock_key = RedisKeys.model_catalog_discovery_lock(harness, cli_version, slug)
    try:
        acquired = await redis.set(lock_key, "1", nx=True, ex=_DISCOVERY_LOCK_TTL_SECONDS)
    except Exception:
        acquired = False
    if not acquired:
        # Another request is already discovering this exact (harness,
        # version, slug) triple — don't pile on a second throwaway window;
        # the caller falls back to the static alias list for this one
        # request.
        return empty

    try:
        discovered = await _discover_via_throwaway_window(agent)
    except Exception:
        logger.warning(
            "harness_catalog: discovery failed for slug=%s version=%s",
            getattr(agent, "slug", None), cli_version, exc_info=True,
        )
        return empty

    if discovered["models"]:
        try:
            await redis.set(cache_key, json.dumps(discovered), ex=_CATALOG_TTL_SECONDS)
        except Exception:
            logger.warning("harness_catalog: cache write failed for %s", cache_key, exc_info=True)
    return discovered


def _normalize_cached(cached) -> dict[str, object]:
    """Cache-Eintraege aus der Zeit vor dieser Runde sind eine BLANKE Liste.
    Ein Deploy darf daran nicht zerschellen — so ein Eintrag hat nur eben
    keine Effort-Aussage (``supported=None``), und die naechste
    Entdeckung nach Ablauf der TTL ergaenzt sie."""
    if isinstance(cached, list):
        return {"models": cached, "effort": dict(_UNKNOWN_EFFORT)}
    if isinstance(cached, dict):
        return {
            "models": cached.get("models") or [],
            "effort": cached.get("effort") or dict(_UNKNOWN_EFFORT),
        }
    return {"models": [], "effort": dict(_UNKNOWN_EFFORT)}


async def discover_model_catalog(agent) -> list[dict[str, str]]:
    """Nur der Modell-Teil von ``discover_harness_capabilities`` — die Form,
    an der ``agent_chat_input.model_options_capabilities`` haengt."""
    return (await discover_harness_capabilities(agent))["models"]  # type: ignore[return-value]


async def discover_effort_support(agent) -> dict[str, object]:
    """Nur der Effort-Teil: ``{"supported": bool|None, "model", "level"}``.
    Teilt sich Cache und Wegwerf-Lauf mit ``discover_model_catalog`` — wer
    beides in einem Request braucht, zahlt trotzdem nur einmal."""
    return (await discover_harness_capabilities(agent))["effort"]  # type: ignore[return-value]


async def _discover_via_throwaway_window(agent) -> dict[str, object]:
    """Opens a throwaway tmux window running a fresh session of this agent's
    OWN CLI, drives it through ``/model`` to capture the picker, pages
    through the whole list, then tears the window down — regardless of
    success or failure (``finally``). NEVER touches the agent's own window 0
    or any other real session.

    **Nie Enter im Picker.** Enter WAEHLT AUS, und die Wahl persistiert
    ("Applies to this session and future OpenClaude sessions") — ein
    Lese-Probe darf einen echten Agenten nicht umschalten. Abgebrochen wird
    mit Escape; live gegengeprueft, dass das auch nach dem Blaettern nichts
    veraendert (``⎿ Kept model as qwen38-27b-unsloth-nvfp4``, settings.json
    md5 vorher==nachher)."""
    slug = agent.slug
    harness = getattr(agent, "harness", None) or "claude"
    binary = _HARNESS_BINARIES.get(harness, "claude")
    window = _DISCOVERY_WINDOW_NAME

    await _tmux(slug, ["new-window", "-t", slug, "-n", window,
                        f"{binary} --dangerously-skip-permissions"])
    try:
        if not await _wait_for_ready(slug, window):
            return {"models": [], "effort": dict(_UNKNOWN_EFFORT)}

        await _send_literal(slug, window, "/model")
        for _ in range(_PICKER_OPEN_ENTERS.get(harness, 2)):
            await _send_enter(slug, window)
            await asyncio.sleep(_DISCOVERY_POLL_INTERVAL_SECONDS)

        pane_text = await _poll_for_picker(slug, window)
        if pane_text is None:
            await _send_key(slug, window, "Escape")
            return {"models": [], "effort": dict(_UNKNOWN_EFFORT)}

        # Die Effort-Zeile der ERSTEN Aufnahme gilt dem aktiven Modell des
        # Agenten — genau die Frage, die effort_capabilities stellt. Nach dem
        # Blaettern zeigt sie ein anderes Modell und waere die falsche Antwort.
        effort = parse_effort_line(pane_text)

        dropped: list[str] = []
        rows, hidden, seen = _parse_picker_page(pane_text, harness, dropped)
        total = (len(seen) + hidden) if hidden is not None else None
        rounds = 0
        while total is not None and len(seen) < total and rounds < _PICKER_MAX_PAGE_ROUNDS:
            await _tmux(slug, ["send-keys", "-t", f"{slug}:{window}",
                               "-N", str(_PICKER_PAGE_STEP), "Down"])
            await asyncio.sleep(_DISCOVERY_POLL_INTERVAL_SECONDS)
            page = await _capture(slug, window)
            rounds += 1
            if page is None:
                continue
            more_rows, _, more_seen = _parse_picker_page(page, harness, dropped)
            rows.update(more_rows)
            seen |= more_seen

        await _send_key(slug, window, "Escape")  # cancel — no selection change

        if total is not None and len(seen) < total:
            # Kein stilles Abschneiden: was fehlt, wird gesagt. Der Katalog
            # geht trotzdem raus — ehrlich unvollstaendig ist besser als
            # gar keine Auswahl.
            logger.warning(
                "harness_catalog: /model-Katalog unvollstaendig fuer slug=%s harness=%s "
                "— %d von %d Zeilen gelesen (%d Blaetter-Runden). Fehlende "
                "Modelle fehlen im Dropdown; der Terminal-Weg bleibt.",
                slug, harness, len(seen), total, rounds,
            )
        if dropped:
            # Ebenfalls kein stilles Abschneiden: diese Zeilen SIND im Picker,
            # aber ihr /model-Argument ist unbelegt (z. B. "Sonnet (1M
            # context)"). Raten wuerde den Agenten auf ein fremdes Modell
            # schalten — darum fehlen sie im Dropdown und stehen stattdessen
            # hier.
            logger.warning(
                "harness_catalog: %d /model-Zeile(n) ohne belegtes Kommando-Token "
                "fuer slug=%s harness=%s uebersprungen: %s. Umschalten geht dort "
                "nur ueber das Terminal, bis das Token live gegengeprueft ist.",
                len(dropped), slug, harness, ", ".join(dropped),
            )
        return {"models": [rows[i] for i in sorted(rows)], "effort": effort}
    finally:
        await _tmux(slug, ["kill-window", "-t", f"{slug}:{window}"])


async def _wait_for_ready(slug: str, window: str) -> bool:
    """Polls capture-pane for the CLI's own ready-signal glyphs (mirrored
    from ``docker_agent_sync._wait_for_window_ready`` — the same vocabulary
    ``pane_state`` already builds on) up to
    ``_DISCOVERY_READY_TIMEOUT_SECONDS``."""
    deadline = time.time() + _DISCOVERY_READY_TIMEOUT_SECONDS
    while time.time() < deadline:
        pane = await _capture(slug, window)
        if pane and any(sig in pane for sig in ("╭─", "❯", "> ", "$ ")):
            return True
        await asyncio.sleep(_DISCOVERY_POLL_INTERVAL_SECONDS)
    return False


async def _poll_for_picker(slug: str, window: str) -> str | None:
    deadline = time.time() + _DISCOVERY_READY_TIMEOUT_SECONDS
    while time.time() < deadline:
        pane = await _capture(slug, window)
        if pane and "Select model" in pane:
            return pane
        await asyncio.sleep(_DISCOVERY_POLL_INTERVAL_SECONDS)
    return None


async def _tmux(slug: str, tmux_args: list[str]) -> None:
    argv = ["docker", "exec", "-u", "agent", f"mc-agent-{slug}", "tmux", *tmux_args]
    await asyncio.to_thread(subprocess.run, argv, capture_output=True, timeout=10)


async def _send_literal(slug: str, window: str, text: str) -> None:
    await _tmux(slug, ["send-keys", "-t", f"{slug}:{window}", "-l", "--", text])


async def _send_key(slug: str, window: str, key: str) -> None:
    await _tmux(slug, ["send-keys", "-t", f"{slug}:{window}", key])


async def _send_enter(slug: str, window: str) -> None:
    await _send_key(slug, window, "Enter")


async def _capture(slug: str, window: str) -> str | None:
    argv = [
        "docker", "exec", "-e", "LANG=C.UTF-8", "-u", "agent", f"mc-agent-{slug}",
        "tmux", "capture-pane", "-p", "-t", f"{slug}:{window}",
    ]
    try:
        result = await asyncio.to_thread(
            subprocess.run, argv, capture_output=True, text=True, timeout=5
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


async def observe_model_window(model: str, window: int) -> None:
    """Persists one ``(model, context_window_size)`` observation from a
    FRESH statusline-state read to the shared Redis hash — fail-silent
    (a Redis hiccup must never break the usage-event stamping it rides
    along with). Newest write always wins (plain HSET)."""
    try:
        redis = await get_redis()
        await redis.hset(RedisKeys.model_window_observations(), model, window)
    except Exception:
        logger.warning("harness_catalog: observe_model_window failed for %s", model, exc_info=True)


async def get_observed_model_windows() -> dict[str, int]:
    """Reads the whole observed-window hash. Fail-silent -> ``{}`` (the
    caller's precedence chain falls through to the static config seed)."""
    try:
        redis = await get_redis()
        raw = await redis.hgetall(RedisKeys.model_window_observations())
    except Exception:
        logger.warning("harness_catalog: get_observed_model_windows failed", exc_info=True)
        return {}
    out: dict[str, int] = {}
    for model, value in (raw or {}).items():
        try:
            out[model] = int(value)
        except (TypeError, ValueError):
            continue
    return out
