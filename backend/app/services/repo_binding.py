"""Repo-Bindungs-Waechter fuer Delegationen (Vorfall 2026-09: drei Karten ohne
`mc delegate --repo`, deren Beschreibungen konkrete Datei-Fundstellen nannten;
der Worker landete im gemeinsamen Ad-hoc-Klon `marknx/mc-workspace` statt im
System-Repo, und die Arbeit war wertlos).

Der Waechter schlaegt nur an, wenn ZWEI Dinge zusammenkommen:

  1. Die Delegation nennt eine konkrete Datei-Fundstelle (`pfad/datei.py:123`)
     — das ist der Beleg, dass die Arbeit an UNSERER Codebasis haengt und
     nicht an einem Ad-hoc-Klon.
  2. Weder ein explizites Repo noch ein Projekt **mit wirksamer
     Repo-Bindung** traegt die Bindung. Eine blosse `project_id` genuegt
     NICHT: von den 13 Projekten des Boards tragen 9 kein GitHub-Repo
     (gemessen 2026-09-16), und dort landet die Arbeit im selben gemeinsamen
     Ad-hoc-Klon — genau der Fall, den der Waechter verhindern soll.

Recherche- und Content-Karten nennen typischerweise keine Fundstellen und
laufen deshalb unberuehrt durch. Reine Dateinamen ohne Verzeichnis
(`Composer.tsx`) zaehlen bewusst NICHT als Fundstelle — sie sind ohne
Verzeichnis nicht eindeutig und treten auch in Prosa auf.

Messung am Bestand (alle 939 Karten des Boards, 2026-09-16): 70 Karten nennen
eine Fundstelle; davon tragen 3 ein `repo_id` und 1 ein `project_id`, das
uebrige 66 gar nichts. Wirksam gebunden sind also nur die 3 mit `repo_id`;
**67** nennen eine Fundstelle ohne wirksame Bindung. Genau **eine** Karte ist
heute allein ueber ein Projekt gebunden, dessen Projekt kein GitHub-Repo
traegt — die Korrektur blockiert auf diesem Bestand also eine Karte, nicht
keine. Die Projekt-Vererbung rettet dabei fast nichts: von den 52 Unterkarten
unter den 67 binden nur 2 ueberhaupt ueber ein wirksames Eltern-Repo, die
uebrigen 50 haengen an Eltern ohne jede Bindung. Von den 13 Projekten des
Boards tragen 9 kein GitHub-Repo, und auf diesen 9 liegen 46 Karten — der
Grund, warum "hat ein Projekt" nicht als Bindung durchgeht.
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


def project_binds_repo(project) -> bool:
    """Traegt dieses Projekt eine WIRKSAME Repo-Bindung?

    Eine blosse `project_id` genuegt nicht: ein Projekt ohne GitHub-Repo
    (beide Felder leer) erbt keine Bindung, sondern nur den gemeinsamen
    Ad-hoc-Klon — genau der Fall, den der Waechter verhindern soll. Beide
    Felder werden geprueft, weil `repo_registry.apply_repo_link` sie zusammen
    setzt und aeltere Karten nur das Legacy-Feld tragen koennen.
    """
    if project is None:
        return False
    return bool(getattr(project, "github_repo_name", None)) or (
        getattr(project, "repo_id", None) is not None
    )


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
