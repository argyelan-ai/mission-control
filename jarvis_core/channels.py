"""Kanal-Definitionen fuer Jarvis (ADR-061).

Ein ``Channel`` beschreibt, ueber welchen Weg der Operator mit Jarvis spricht und
welche Faehigkeiten dieser Weg hat. Die Tool-Handler (``jarvis_core.tools``)
degradieren ihr Verhalten anhand dieser Capabilities:

- ``supports_cards`` — kann visuelle Cards auf ein Display pushen (Voice-Drawer).
  Telegram hat das nicht → show_* liefert stattdessen Text/Link zurueck.
- ``supports_graph_highlight`` — kann den 3D-Memory-Graph fernsteuern (nur am
  Desk / im Voice-Frontend). Telegram → hoefliche Ablehnung.
- ``persona_addendum`` — der kanal-spezifische Teil des System-Prompts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Channel:
    """Ein Interaktionskanal (voice, telegram, ...)."""

    name: str
    label: str
    supports_cards: bool
    supports_graph_highlight: bool


# Voice: gesprochener Kanal mit Voice-Drawer-Display (Cards) und 3D-Graph.
VOICE = Channel(
    name="voice",
    label="Voice",
    supports_cards=True,
    supports_graph_highlight=True,
)

# Telegram: reiner Text-/Sprachnachrichten-Kanal, kein Display, kein Graph.
TELEGRAM = Channel(
    name="telegram",
    label="Telegram",
    supports_cards=False,
    supports_graph_highlight=False,
)


BY_NAME = {c.name: c for c in (VOICE, TELEGRAM)}
