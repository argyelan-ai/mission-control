"""Vorschau aus dem Terminal-Strom — die schnelle Haelfte der Chat-Ansicht.

Warum es das gibt (gemessen 01.09.2026): Die CLIs schreiben ihr Transkript
BLOCKWEISE. Zwischen Frage und fertiger Antwort liegen dort Sekunden voelliger
Stille, waehrend im Terminal der Text laengst laeuft — ein 150-Woerter-Absatz
erschien nach zehn Sekunden auf einen Schlag in der Datei, war im Pane aber ab
Sekunde vier zu sehen. Das Transkript bleibt die Wahrheit (Rollen, Werkzeuge,
Modell, Verbrauch); dieses Modul liefert, was bis dahin zu sehen ist.

Der Strom aus ``tmux pipe-pane`` ist rohe Terminal-Ausgabe mit Cursor-Spruengen
(``\\e[2B``, ``\\e[3G``), kein anhaengbarer Text. Deshalb rechnet ``pyte`` einen
Bildschirm nach, und gelesen wird der Bildschirm — nicht der Strom.

Bewusst OHNE CLI-spezifische Marker: omp kennzeichnet Antworten gar nicht,
Claude Code mit ``●``. Genommen wird stattdessen, was auf dem Bildschirm steht,
abzueglich des Rahmens der Oberflaeche. Das traegt jede CLI, auch die naechste.
"""
from __future__ import annotations

import re

import pyte

#: Rueckfall-Groesse des nachgerechneten Bildschirms, wenn tmux nicht sagt, wie
#: gross die Pane wirklich ist. Die echte Groesse ist PFLICHT (Live-Gate
#: 01.09.2026: Sparkys Pane war 168x45, mit 80 Spalten brach jede Zeile bei
#: Zeichen 80 ab und der Rest ging beim Neuzeichnen verloren). Der Rueckblick
#: faengt weggescrollte Zeilen auf.
COLS, ROWS, HISTORY = 80, 24, 3000

#: omp zeigt eingereihte Nachrichten in einer Box "Steering · N" mit N
#: nummerierten Zeilen. Das ist das Echo des Operators, nie Inhalt.
_STEERING = re.compile(r"^\s*Steering\s*·\s*(\d+)\s*$")

#: Zeilen, die zur Oberflaeche gehoeren und nie Inhalt sind. Bewusst als
#: Muster-Liste und nicht als Adapter-Methode: die Rahmen der vier CLIs
#: unterscheiden sich in den Zeichen, nicht in der Art.
# Claude Code zeichnet einen Werkzeugaufruf als Block: Kopf „● Name(…" (die
# Argumente brechen in Folgezeilen um), darunter „⎿"-Ergebniszeilen, die
# ebenfalls umbrechen. Der Kopf eines Bash-Aufrufs ist seine Beschreibung OHNE
# Klammern — erkennbar nur daran, dass direkt darunter „⎿" steht.
_TOOL_HEAD = re.compile(r"^\s*●\s*[A-Z][A-Za-z]*(?: [A-Z][A-Za-z]*)*\(")
_TOOL_RESULT = re.compile(r"^\s*⎿")
_BULLET = re.compile(r"^\s*●")
_MARKDOWN_MARKS = re.compile(r"\*\*|__|`|^\s*#{1,6}\s+", re.M)

_FURNITURE = re.compile(
    r"""
      ^\s*[─━═╭╰│╮╯┌└├┤]                    # Rahmen- und Trennlinien (├ = omp-Box)
    | ^\s*❯                                  # Eingabezeile
    | ^\s*⏵⏵                                 # Berechtigungs-Hinweis
    | ^\s*⎿\s*Tip:                           # eingeblendete Tipps
    | (bypass\ permissions|esc\ to\ interrupt|⟦esc⟧|for\ agents)
    | ^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏·✻✽✢✶✳✻*](\s|$)        # Spinner-Zeilen (auch allein)
    | ^\s*\w+…\s*$                           # "Working…", "Germinating…"
    | ^\s*\S+\ for\ \d+s                     # "Baked for 2s · done"
    | 📬\ Neue\ Nachrichten\ \(bis\ seq          # Zustell-Echo, das poll.sh eintippt
    | ^\s*@\S*\.msg-nudge\.msg                # Zustell-Echo, das die omp-Bridge eintippt
    | ^\s*\[Pasted\ text\ \#\d+                # Claude Code: eingefuegter Auftrag im Composer
    | ^\s*paste\ again\ to\ expand             #   … und sein Hinweis darunter
    | ^\s*(●\s*)?Running\ \d+\ .*…\s*$         # Claude Code: Werkzeug-Statuszeile
    | ^\s*Ran\ \d+\ shell\ command                #   … und ihr Abschluss
    | ^\s*●\s*$                                # Claude Code: Platzhalter der kommenden Antwort
    | ^\s*[▐▝▎]                               # Claude Code: Logo-Banner + „▎ Using …" nach Neustart
    | ^\s*Update\ Available\s*$                # omp: Update-Banner nach Frischstart
    | ^\s*New\ version\ .*Run:\ omp\ update      #   … zweite Zeile davon
    """,
    re.VERBOSE,
)


class PanePreview:
    """Haelt einen nachgerechneten Bildschirm und gibt seinen Text aus.

    ``feed`` darf beliebig oft mit Teilstuecken aufgerufen werden — der
    Bildschirm ist zustandsbehaftet, der Text waechst also mit dem Strom. Genau
    das braucht die Live-Ansicht: bei halb eingespieltem Strom kommt ein
    korrektes PRAEFIX heraus, kein Kauderwelsch (in der Machbarkeitsprobe an
    230 von 556 Zeichen belegt).
    """

    def __init__(self, cols: int = COLS, rows: int = ROWS) -> None:
        self.cols, self.rows = cols, rows
        self._screen = pyte.HistoryScreen(cols, rows, history=HISTORY, ratio=0.5)
        self._stream = pyte.Stream(self._screen)

    def fresh(self) -> "PanePreview":
        """Ein leerer Bildschirm derselben Groesse — fuer den Neustart, wenn
        die Strom-Datei geleert wurde."""
        return PanePreview(self.cols, self.rows)

    def feed(self, chunk: bytes | str) -> None:
        if not chunk:
            return
        if isinstance(chunk, bytes):
            # Ein Teilstueck kann mitten in einer UTF-8-Folge enden — ersetzen
            # statt werfen, der naechste Zufluss bringt den Rest ohnehin neu.
            chunk = chunk.decode("utf-8", errors="replace")
        self._stream.feed(chunk)

    def _lines(self) -> list[str]:
        rows = list(self._screen.history.top)
        rows += [self._screen.buffer[y] for y in range(self.rows)]
        return [self._row_text(row) for row in rows]

    def _row_text(self, row) -> str:
        """Eine Bildschirmzeile als Text — oder leer, wenn sie NUR aus
        kursiven Zeichen besteht.

        omp zeichnet die Zusammenfassung seines Denkens kursiv und grau, den
        Antworttext aufrecht. Die Denk-Zeile ist kein Inhalt (Live-Gate
        02.09.2026: sie stand in der Vorschau, in der Antwort nie). Ein
        einzelnes kursives Wort in einer sonst aufrechten Zeile bleibt.
        """
        chars = [row[x] if x in row else None for x in range(self.cols)]
        text = "".join(c.data if c is not None else " " for c in chars).rstrip()
        inked = [c for c in chars if c is not None and c.data.strip()]
        if inked and all(c.italics for c in inked):
            return ""
        return text

    def text(self) -> str:
        """Der sichtbare Inhalt ohne den Rahmen der Oberflaeche."""
        return "\n".join(self._content_lines()).strip()

    def text_after(self, anchor: str) -> str:
        """Nur das, was NACH ``anchor`` auf dem Bildschirm steht.

        Der Bildschirm traegt auch aeltere Zuege. Die Live-Ansicht will aber
        ausschliesslich das Neue: der Aufrufer reicht die letzte ihm bekannte
        Zeile aus dem Transkript herein (oder das Echo der gerade gesendeten
        Nachricht) und bekommt zurueck, was seitdem dazugekommen ist.

        Gesucht wird ueber ALLE Zeilen, auch die des Rahmens: das Echo einer
        gesendeten Nachricht steht in der Eingabezeile, und die faellt sonst
        schon vor der Suche weg. Zurueck kommt nur Inhalt.

        Eine lange Zeile steht im Terminal umgebrochen ueber mehrere
        Bildschirmzeilen; der Schnitt gehoert HINTER die letzte davon. Gesucht
        wird darum mit Kopf UND Schwanz des Ankers, genommen wird der letzte
        Treffer — sonst tropfte der Rest der alten Antwort in die Vorschau.

        Ohne Treffer kommt der ganze Inhalt zurueck — lieber zu viel zeigen als
        eine Antwort verschlucken; die Wahrheit raeumt ohnehin auf.
        """
        # Das Transkript traegt Markdown ('- **Bereit** für …'), der Bildschirm
        # zeigt es gerendert ('- Bereit für …'). Die Auszeichnung faellt darum
        # vor der Suche weg (live 03.09.2026: sonst kein Treffer, Rueckfall
        # auf den ganzen Bildschirm nach der fertigen Antwort).
        needle = re.sub(r"\s+", " ", _MARKDOWN_MARKS.sub("", anchor)).strip()
        lines = self._lines()
        if not needle:
            return self.text()
        # Gesucht wird im ZUSAMMENGEFUEGTEN Text, nicht Zeile fuer Zeile: die
        # CLI bricht an Wortgrenzen um, Kopf oder Schwanz des Ankers liegen
        # also gern ueber zwei Bildschirmzeilen (live 02.09.2026: '…sowie der'
        # | 'Kilauea auf Hawaii.'). Jede Zeile kennt ihren Startversatz im
        # Ganzen; die Fundstelle wird darueber auf die Zeile zurueckgerechnet.
        flat_lines = [re.sub(r"\s+", " ", line).strip() for line in lines]
        joined, starts = "", []
        for flat in flat_lines:
            starts.append(len(joined))
            joined += flat + " "
        head, tail = needle[:24], needle[-24:]
        end = -1
        for probe in (needle, tail, head):
            if len(probe) <= 8:
                continue
            pos = joined.rfind(probe)
            if pos >= 0:
                end = pos + len(probe) - 1
                break
        if end < 0:
            # Kurzer Anker (ein Wort, ein Emoji — Marks 'danke', 03.09.2026):
            # als Teilstueck traefe er mitten im Wort, als GANZE Zeile ist er
            # eindeutig. Genommen wird die letzte Zeile, die genau so lautet.
            hits = [
                i for i, flat in enumerate(flat_lines)
                if flat.lstrip("❯>● ") == needle
            ]
            if not hits:
                return self.text()
            return "\n".join(self._filter(lines[hits[-1] + 1 :])).strip()
        cut = max(i for i, start in enumerate(starts) if start <= end)
        return "\n".join(self._filter(lines[cut + 1 :])).strip()

    def _content_lines(self) -> list[str]:
        return self._filter(self._lines())

    @staticmethod
    def _filter(lines: list[str]) -> list[str]:
        """Rahmen weg — und wiederholte Zeilen weg.

        Beim Umbrechen zeichnet eine CLI ihren Absatz neu: die alte Fassung
        liegt dann im Rueckblick, die neue im Bild, und beide zusammen ergaeben
        einen Text, der Passagen doppelt zeigt. Fuer eine Vorschau ist das der
        schlimmste Fehler — sie soll nicht wie ein Fehler aussehen. Eine Zeile,
        die (bis auf Leerraum) schon dasteht, wird darum nur einmal gezeigt.

        Die Grenze von 30 Zeichen schuetzt kurze, echt wiederholte Zeilen
        (``}``, ``---``, ein Listenpunkt ``2.``) davor, faelschlich zu
        verschwinden.
        """
        out: list[str] = []
        seen: set[str] = set()
        skip = 0            # verbleibende Zeilen der Steering-Box
        in_tool = False     # innerhalb eines Werkzeug-Blocks (bis Leerzeile / neues „●")
        for i, raw in enumerate(lines):
            line = raw.strip()
            if not line:
                in_tool = False
            elif _BULLET.match(raw):
                nxt = next((l for l in lines[i + 1:] if l.strip()), "")
                in_tool = bool(_TOOL_HEAD.match(raw) or _TOOL_RESULT.match(nxt))
            elif _TOOL_RESULT.match(raw):
                in_tool = True
            if in_tool:
                continue
            if skip and re.match(r"^\d+\.\s", line):
                skip -= 1
                continue
            steering = _STEERING.match(line)
            if steering:
                skip = int(steering.group(1))
                continue
            if not line or _FURNITURE.search(raw):
                continue
            key = re.sub(r"\s+", " ", line)
            if len(key) >= 30:
                if key in seen:
                    continue
                seen.add(key)
            out.append(line)
        return out
