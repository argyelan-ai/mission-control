"""Die `.env` eines Zweibox-Rezepts schreiben (Rezept-Umschalter P3, 04.09.2026).

Warum es dieses Modul gibt
--------------------------
MC startet einen Verbund NICHT selbst auf zwei Boxen — das Rezept macht das
(ADR-077, Regel 5: „Kein neuer Multi-Host-Startcode"). MC spricht nur mit dem
Head; dessen Startskript zieht die zweite Box per SSH dazu. Woher weiss das
Skript, WELCHE zweite Box? Aus seiner eigenen `.env`.

Am 02.09.2026 wurde am echten Skript geprüft: es lädt seine `.env` VOR den
Standardwerten (`set -a`, danach `HEAD_IP="${HEAD_IP:-…}"`). Steht der Wert in
der `.env`, gewinnt die Datei gegen jede Umgebungsvariable, die MC mitgeben
könnte. Also schreibt MC die Datei — und nur die zwei, drei Schlüssel, die im
Katalog stehen (`local_recipes.env_map`), nie die ganze Datei.

Zwei Teile, getrennt testbar
----------------------------
* :func:`render_env_map` — reine Rechnung: Platzhalter → Adressen. Kein Netz.
* :func:`upsert_env_file` — schreibt über SSH, idempotent, mit Rücklesen.

Regeln, die hier teuer bezahlt sind
-----------------------------------
* **Idempotent.** Eine vorhandene Zeile `KEY=…` wird ERSETZT, eine fehlende
  angehängt. Alles andere in der Datei bleibt, wie es war — es ist die Datei
  des Betreibers, nicht unsere.
* **Ein Backup, einmalig.** `<pfad>.bak-mc` entsteht beim ersten Schreiben und
  wird nie überschrieben: sonst wäre nach dem zweiten Start die Sicherung des
  Originals weg.
* **Rücklesen ist Pflicht.** „Befehl lief durch" ist kein Beweis. Nach dem
  Schreiben wird die Datei gelesen und Wert für Wert verglichen; weicht etwas
  ab, bricht der Start mit 502 ab, statt ein Modell mit falschen Adressen zu
  starten.
* **Kein Python auf der Box vorausgesetzt.** POSIX-sh und awk, sonst nichts.
* **Sonderzeichen.** Jeder Wert geht über ``shlex.quote`` in die Umgebung des
  awk-Aufrufs (``ENVIRON``), nie in den awk-Programmtext — dort würde awk
  Backslash-Folgen selbst noch einmal auflösen.
* **`~` bleibt stehen.** Der Pfad gehört der Shell der BOX. Der Backend-
  Container hat ein anderes Zuhause; lokal aufgelöst zeigte er ins Leere.
"""

from __future__ import annotations

import logging
import re
import shlex
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — nur für die Typprüfung
    from app.models.host import Host

logger = logging.getLogger("mc.recipe_env")

#: Die erlaubten Platzhalter in ``local_recipes.env_map``.
#:
#: Drei Adressarten, weil es drei Netze gibt (siehe models/host.py):
#: ``ip`` = ``ssh_host`` (MC → Box), ``fabric_ip`` = das Verbund-Kabel
#: (Box ↔ Box; NULL fällt auf ssh_host zurück), ``ssh`` = ``user@host`` für
#: Skripte, die den Worker selbst per SSH anfassen.
PLACEHOLDERS: tuple[str, ...] = (
    "head_ip",
    "worker_ip",
    "head_fabric_ip",
    "worker_fabric_ip",
    "head_ssh",
    "worker_ssh",
)

#: Ein Umgebungsschlüssel, wie ihn eine `.env` kennt. Bewusst eng: der Name
#: landet in einem awk-Vergleich und in einer Shell-Zeile.
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")

#: Kurz — es wird nur eine Datei umgeschrieben, nichts geladen.
_WRITE_TIMEOUT = 20.0
_READ_TIMEOUT = 15.0


class EnvRenderError(ValueError):
    """Ein Katalogeintrag, der so nicht gerendert werden kann. Die Nachricht
    ist ein Satz für die Oberfläche."""


def _ssh_user_of(host: "Host") -> str | None:
    from app.config import settings

    return (host.ssh_user or settings.dgx_ssh_user or "").strip() or None


def _address_of(host: "Host") -> str | None:
    return (host.ssh_host or "").strip() or None


def placeholder_values(head: "Host", worker: "Host | None") -> dict[str, str | None]:
    """Die Adressen hinter den Platzhaltern — ``None``, wo es sie nicht gibt."""
    head_ip = _address_of(head)
    worker_ip = _address_of(worker) if worker is not None else None
    head_user = _ssh_user_of(head)
    worker_user = _ssh_user_of(worker) if worker is not None else None
    return {
        "head_ip": head_ip,
        "worker_ip": worker_ip,
        # NULL = „nimm ssh_host" (models/host.py, Migration 0192).
        "head_fabric_ip": (head.fabric_ip or "").strip() or head_ip,
        "worker_fabric_ip": (
            ((worker.fabric_ip or "").strip() or worker_ip) if worker is not None else None
        ),
        "head_ssh": f"{head_user}@{head_ip}" if head_user and head_ip else head_ip,
        "worker_ssh": (
            f"{worker_user}@{worker_ip}" if worker_user and worker_ip else worker_ip
        ),
    }


def render_env_map(
    env_map: dict[str, Any] | None, head: "Host", worker: "Host | None" = None
) -> dict[str, str]:
    """``{"KEY": "{worker_fabric_ip}"}`` → ``{"KEY": "192.0.2.11"}``.

    Ein Wert darf Text um den Platzhalter herum haben
    (``"tcp://{worker_fabric_ip}:29500"``); mehrere Platzhalter in einem Wert
    sind erlaubt.

    Raises :class:`EnvRenderError` (eine ``ValueError``) mit einem Satz, wenn
    ein Platzhalter unbekannt ist, ein Schlüssel kein Umgebungsname ist, oder
    die gemeinte Adresse fehlt (z.B. ``{worker_ip}`` ohne Worker-Box). Ein
    stiller Fallback wäre hier das Schlimmste: das Rezept würde starten und
    die falsche Box anfassen.
    """
    if not env_map:
        return {}
    values = placeholder_values(head, worker)
    rendered: dict[str, str] = {}
    for raw_key, raw_value in env_map.items():
        key = str(raw_key).strip()
        if not _KEY_RE.match(key):
            raise EnvRenderError(
                f"'{raw_key}' ist kein gültiger Name für eine Umgebungsvariable "
                f"(erlaubt: Buchstaben, Ziffern, Unterstrich; nicht mit einer Ziffer beginnend)."
            )
        template = "" if raw_value is None else str(raw_value)
        for name in _PLACEHOLDER_RE.findall(template):
            if name not in PLACEHOLDERS:
                raise EnvRenderError(
                    f"Unbekannter Platzhalter '{{{name}}}' bei '{key}' — erlaubt sind: "
                    + ", ".join("{" + p + "}" for p in PLACEHOLDERS)
                    + "."
                )
            if values.get(name) is None:
                raise EnvRenderError(
                    f"Für '{{{name}}}' bei '{key}' gibt es keine Adresse — "
                    f"trage sie an der Box nach (SSH-Adresse bzw. Verbund-Adresse)."
                )
        rendered[key] = _PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]), template)
    return rendered


def quote_remote_path(path: str) -> str:
    """Pfad für die Shell der BOX quoten, ohne ``~`` zu töten.

    ``shlex.quote("~/x/.env")`` ergäbe ``'~/x/.env'`` — und damit ein Zuhause
    namens „~". Darum bleibt eine führende Tilde unzitiert; der Rest wird
    normal gequotet, denn der Pfad kommt aus dem Katalog.
    """
    path = path.strip()
    if path == "~":
        return "~"
    if path.startswith("~/"):
        rest = path[2:]
        return "~/" + shlex.quote(rest) if rest else "~/"
    return shlex.quote(path)


def _upsert_command(path: str, values: dict[str, str]) -> str:
    """Ein einziges POSIX-sh-Skript: Backup einmalig, dann Zeile für Zeile.

    ``set -e`` bricht beim ersten Fehler ab; ohne das würde ein voll gelaufenes
    Dateisystem als „geschrieben" durchgehen und erst das Rücklesen es merken
    (das es zwar täte — aber ein Abbruch am Ort des Fehlers ist ehrlicher).
    """
    quoted = quote_remote_path(path)
    lines = [
        "set -e",
        f"f={quoted}",
        # Ordner muss existieren — MC legt keine Rezept-Ordner an.
        'd=$(dirname "$f")',
        '[ -d "$d" ] || { echo "Ordner $d gibt es auf dieser Box nicht" >&2; exit 3; }',
        '[ -f "$f" ] || : > "$f"',
        # Einmalig: das Original des Betreibers sichern.
        '[ -f "$f.bak-mc" ] || cp "$f" "$f.bak-mc"',
    ]
    for key, value in values.items():
        lines.append(
            f"MC_K={shlex.quote(key)} MC_V={shlex.quote(value)} "
            'awk \'BEGIN{k=ENVIRON["MC_K"]; v=ENVIRON["MC_V"]} '
            'index($0, k "=")==1 { if (!d) { print k "=" v; d=1 } next } '
            "{ print } "
            'END{ if (!d) print k "=" v }\' "$f" > "$f.mc-tmp"'
        )
        # `cat >` statt `mv`, damit Rechte und Besitzer der Originaldatei bleiben.
        lines.append('cat "$f.mc-tmp" > "$f"')
        lines.append('rm -f "$f.mc-tmp"')
    return "\n".join(lines)


def parse_env_text(text: str) -> dict[str, str]:
    """``KEY=wert``-Zeilen einer `.env` lesen. Kommentare und Leeres fliegen
    raus; die letzte Nennung eines Schlüssels gewinnt (so liest die Shell auch)."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if _KEY_RE.match(key):
            found[key] = value
    return found


async def upsert_env_file(host: Any, path: str, values: dict[str, str]) -> list[str]:
    """Die genannten Schlüssel in die `.env` auf der Box schreiben und den
    Erfolg BEWEISEN (zurücklesen und vergleichen).

    ``host`` ist ein ``ResolvedHost`` (dieselbe SSH-Primitive wie jeder andere
    Lebenszyklus-Schritt: ``runtime_manager._ssh_run``).

    Returns: die geschriebenen Schlüssel, in der Reihenfolge des Katalogs.
    Raises: ``recipe_switcher.RecipeStartError`` (502) mit einem Satz, wenn das
    Schreiben scheitert oder die Datei danach etwas anderes enthält.
    """
    # Spät importiert: recipe_switcher importiert dieses Modul beim Start —
    # ein Import auf Modulebene wäre ein Ring.
    from app.services.recipe_switcher import RecipeStartError
    from app.services.runtime_manager import _ssh_run

    if not values:
        return []
    if not (path or "").strip():
        raise RecipeStartError(422, "Das Rezept sagt nicht, wo seine .env liegt (env_file fehlt).")

    try:
        _, stderr, code = await _ssh_run(
            _upsert_command(path, values), host=host, timeout=_WRITE_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001 — jeder SSH-Fehler ist derselbe Satz
        raise RecipeStartError(
            502, f"Die .env {path} liess sich nicht schreiben: {exc}"
        ) from exc
    if code != 0:
        raise RecipeStartError(
            502,
            f"Die .env {path} liess sich nicht schreiben "
            f"(Exit {code}{': ' + stderr if stderr else ''}).",
        )

    try:
        content, _, read_code = await _ssh_run(
            f"cat {quote_remote_path(path)}", host=host, timeout=_READ_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001
        raise RecipeStartError(
            502, f"Die .env {path} liess sich nach dem Schreiben nicht lesen: {exc}"
        ) from exc
    if read_code != 0:
        raise RecipeStartError(
            502, f"Die .env {path} liess sich nach dem Schreiben nicht lesen (Exit {read_code})."
        )

    on_disk = parse_env_text(content)
    wrong = [key for key, value in values.items() if on_disk.get(key) != value]
    if wrong:
        raise RecipeStartError(
            502,
            f"Die .env {path} steht nach dem Schreiben nicht so da, wie sie soll "
            f"({', '.join(sorted(wrong))}) — Start abgebrochen, statt mit falschen "
            f"Adressen zu starten.",
        )
    logger.info("recipe_env: %s auf %s geschrieben (%s)", path, getattr(host, "slug", "?"), ", ".join(values))
    return list(values.keys())
