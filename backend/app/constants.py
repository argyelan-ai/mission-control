"""Single-source-of-truth constants referenced by multiple modules.

When a value (format, charter line, threshold) is hardcoded in more than
one place — template + error message + extraction regex — put it here
and import it everywhere.
"""
from __future__ import annotations


# ── Reflection format (Workstream D) ─────────────────────────────────────
#
# The operator's requirement: the reflection rules that agents read must live in a
# single place. Previously the four field names were hardcoded in three
# locations (SOUL.md.j2 KERNREGEL 4, agent_scoped.py error message,
# _extract_reflection_lesson regex). Now they're here; each location
# imports and renders them.

REFLECTION_REQUIRED_FIELDS: list[str] = [
    "Was wurde gemacht",         # What was done (fact)
    "Was hat funktioniert",      # What worked (success signals)
    "Was war unklar",            # What was unclear / gaps
    "Lesson fuer Agent-Memory",  # Lesson — what should be different next time
]

# Minimum character count for a reflection comment. Below this we reject
# the status transition — agents tend to write trivially short "reflections"
# when under pressure. Empirical: 80 is tight enough to force content,
# loose enough to not block agents who genuinely finished fast.
REFLECTION_MIN_CHARS: int = 80


# Team Reflection Charter — shared principles every agent follows when
# writing their self-reflection. See docs/superpowers/specs/
# 2026-04-20-agent-personas-draft.md for the per-persona reflection voice
# samples that sit under these principles.

REFLECTION_CHARTER: list[str] = [
    "Konkret schreiben, nicht abstract — nenne File-Path, Commit oder "
    "Command. Eine Lesson ohne Artefakt ist keine Lesson.",
    "Ehrend, nicht besserwisserisch — verweise auf andere Agents als "
    "Co-Kollegen. Keine Blame-Kultur.",
    "Team-Artefakt, nicht Tagebuch — die Lesson muss ein Kollege in drei "
    "Monaten lesen und anwenden koennen.",
    "Luecken benennen — wenn etwas unklar bleibt, dann explizit im "
    "'Was unklar'-Feld. Keine plausibel klingenden Fuellmaterial-Saetze.",
    "Persoenliche Stimme, gemeinsame Struktur — alle halten sich an die "
    "vier Felder, aber jeder klingt wie sich selbst.",
]
