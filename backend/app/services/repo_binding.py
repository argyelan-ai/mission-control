"""Repo-Bindungs-Waechter fuer Delegationen (Vorfall 2026-09: drei Karten ohne
`mc delegate --repo`, deren Beschreibungen konkrete Datei-Fundstellen nannten;
der Worker landete im gemeinsamen Ad-hoc-Klon `marknx/mc-workspace` statt im
System-Repo, und die Arbeit war wertlos).

Der Waechter schlaegt nur an, wenn ZWEI Dinge zusammenkommen:

  1. Die Delegation nennt eine konkrete Datei-Fundstelle (`pfad/datei.py:123`)
     — das ist der Beleg, dass die Arbeit an UNSERER Codebasis haengt und
     nicht an einem Ad-hoc-Klon.
  2. Weder ein explizites Repo noch ein Projekt traegt die Bindung.

Recherche- und Content-Karten nennen typischerweise keine Fundstellen und
laufen deshalb unberuehrt durch. Reine Dateinamen ohne Verzeichnis
(`Composer.tsx`) zaehlen bewusst NICHT als Fundstelle — sie sind ohne
Verzeichnis nicht eindeutig und treten auch in Prosa auf.

Messung am Bestand (905 Karten am 2026-09-16): 68 Karten nennen eine
Fundstelle, davon 51 aus Delegationen; genau 3 davon hatten eine Repo-Bindung.
Ohne den Waechter liefen also 48 Karten ins falsche Repo.
"""

from __future__ import annotations

import re

# Verzeichnis/Datei-Endung:Zeile — Verzeichnis-Praefix ist PFLICHT, weil ein
# blosser Dateiname die Fundstelle nicht eindeutig macht.
# Endungs-Whitelist statt "irgendwas nach dem Punkt": verhindert Falschtreffer
# auf Datumsangaben (`16.09.2026`), Versionen (`3.5`), Prozessnamen
# (`.tmux.sock`) und Satzpunkten mit nachfolgender Zahl.
# Das Lookbehind verbietet ein vorangehendes Wortzeichen oder einen Punkt,
# damit aus `.../foo/bar.py:12` kein Teiltreffer wird; ein fuehrender Schraegstrich
# (absolute Fundstelle `/workspace/repo/app/x.py:12`) bleibt erlaubt.
FILE_REFERENCE_RE: re.Pattern[str] = re.compile(
    r"(?<![\w.-])"
    r"(?:[\w.-]+/)+[\w.-]+\.(?:py|ts|tsx|js|jsx|mjs|md|sh|bash|yml|yaml|json"
    r"|toml|go|rs|java|rb|css|scss|html|sql|cfg|ini)"
    r":\d+\b"
)


def find_file_references(*texts: str | None) -> list[str]:
    """Alle Fundstellen (`pfad/datei.ext:ZEILE`) in den uebergebenen Texten."""
    found: list[str] = []
    for text in texts:
        if not text:
            continue
        found.extend(FILE_REFERENCE_RE.findall(text))
    return found


def enforce_repo_binding(
    *,
    title: str | None,
    description: str | None,
    repo_bound: bool,
    waiver_reason: str | None,
) -> list[str]:
    """Lehnt eine Delegation ab, die Fundstellen nennt, aber kein Repo bindet.

    `repo_bound` fasst ALLE Wege zusammen, auf denen die Bindung zustande
    kommen kann — explizites Repo, vererbtes Projekt, oder ein Parent mit
    Projekt. Der Aufrufer entscheidet das VOR diesem Aufruf, weil nur er die
    Vererbung kennt; hier steht nur noch die Regel.

    `waiver_reason` ist der bewusste Ausweg: wer weiss, dass die Karte kein
    Repo braucht (Recherche, Doku, fremdes Repo), markiert das explizit statt
    die Regel zu umgehen. Der Grund muss nicht leer sein — ein leerer Grund
    ist keine Markierung.

    Rueckgabe: die gefundenen Fundstellen (fuer den Audit-Kommentar des
    Aufrufers). Wirft HTTPException(422), wenn die Bindung fehlt.
    """
    references = find_file_references(title, description)
    if repo_bound or waiver_reason or not references:
        return references

    from fastapi import HTTPException

    listed = ", ".join(references[:3])
    if len(references) > 3:
        listed += f" und {len(references) - 3} weitere"
    raise HTTPException(
        status_code=422,
        detail=(
            f"Diese Karte nennt konkrete Datei-Fundstellen ({listed}), ist aber "
            "an kein Repo gebunden — die Arbeit landet sonst im gemeinsamen "
            "Ad-hoc-Klon statt in der Codebasis. Setze --repo <slug|uuid>, ein "
            "project_id, oder haenge die Karte an einen Parent mit Projekt. Ist "
            "das Fehlen bewusst (Recherche, Doku, Arbeit in einem anderen Repo), "
            "markiere es explizit mit --no-repo-reason '<Grund>'."
        ),
    )
