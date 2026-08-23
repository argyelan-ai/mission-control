# ADR-076 — Runde Formensprache für Anfassbares, eckig nur im Raster

**Status:** Accepted
**Datum:** 2026-08-23
**Scope:** Frontend/Design-System

## Kontext

Mission Control startete mit einer bewusst **eckigen** Formensprache. Das steht
so in `DESIGN.md` („Eckige Radien: sm 2px, md 4px, lg 6px, xl 10px", „die
Formensprache bleibt eckig") und in den Tokens von `globals.css`.

Die Praxis lief davon weg. Operator-Befund 23.08.2026: „wir bauen runde
Elemente — das ist zwar noch nicht konsistent über das ganze UI, wir müssen ab
jetzt aber drauf achten und in UI verankern."

Nachgemessen statt geschätzt (`frontend-v2/src`, `.tsx`):

| Klasse | Vorkommen | Wert | Bemerkung |
|---|---|---|---|
| `rounded-lg` | 436 | 6px | |
| `rounded-md` | 276 | 4px | |
| `rounded-xl` | 174 | 10px | |
| `rounded` (nackt) | **173** | 4px | **nicht in der Skala** — Tailwind-Standard |
| `rounded-full` | 139 | ∞ | |
| `rounded-sm` | 115 | 2px | |
| `rounded-2xl` | **30** | 16px | **nicht definiert** — Tailwind-Standard |

Zwei Erkenntnisse daraus:

1. **Die App war nie halb rund.** Sie war fast überall weich-eckig (2–10px);
   wirklich rund waren nur Pills und Avatare. Der Eindruck „teilweise schon
   rund" entstand durch genau diesen Bruch — zwei Formsprachen nebeneinander.
2. **Die Skala lief schon aus.** 203 Stellen (173 nackte `rounded` + 30
   undefinierte `rounded-2xl`) umgingen die Tokens still und landeten auf
   Tailwind-Standardwerten. Das ist der eigentliche Konsistenz-Killer — nicht
   die Zahlen, sondern die Lecks.

## Entscheidung

**Rund bekommt, was man anfässt oder was schwebt. Eckig bleibt, was dicht im
Raster liegt.**

| Rolle | Klasse | vorher | jetzt | Wofür |
|---|---|---|---|---|
| Dicht / Raster | `rounded-dense` | — | 4px | Tabellenzelle, Terminal, Code, Log |
| Marker | `rounded-sm` | 2px | 6px | Punkt, Mini-Chip, Badge |
| Bedienelement | `rounded-md` | 4px | 10px | Knopf, Eingabefeld |
| Karte | `rounded-lg` | 6px | 14px | Listenzeile, Kachel |
| Fläche | `rounded-xl` | 10px | 20px | Insel, Panel |
| Schwebend | `rounded-2xl` | *(16px, undef.)* | 28px | Dialog, Sheet |
| Pill | `rounded-full` | ∞ | ∞ | Chip, Avatar, Switch-Track |

Dazu Rollen-Aliasse (`--radius-control`, `--radius-card`, `--radius-surface`,
`--radius-floating`, `--radius-pill`), damit die Frage bei neuer Arbeit „was
ist das?" lautet und nicht „welche Zahl?".

**Warum die Ausnahme fürs Raster und nicht pauschal „alles rund":** Ein Radius
kämpft im Gitter gegen die Kante. In Tabellen, im Terminal und in Code-Blöcken
kostet er Platz, den dichte Daten brauchen, und macht weich, wo man Präzision
liest. Überall sonst sagt er das Gegenteil: anfassbar, beweglich, eigenständig.
Damit ist „teilweise rund" keine Inkonsequenz mehr, sondern eine Regel.

**Umgesetzt über die Tokens, nicht über die Aufrufstellen.** Sechs Zeilen in
`globals.css` verändern rund 1140 Stellen mit. Angefasst werden nur die
Ausnahmen und die Lecks.

## Alternativen

- **Bei eckig bleiben und die runden Stellen zurückbauen.** Verworfen: das
  Urteil über Form liegt beim Operator, und seine Richtung ist eindeutig.
  Ausserdem wäre es der teurere Weg — die runden Stellen sind über 75 Dateien
  verteilt, die Tokens sind eine Datei.
- **Pauschal alles rund, ohne Raster-Ausnahme.** Verworfen: in Tabellen und im
  Terminal frisst ein grosser Radius Platz und Präzision. Ohne Ausnahme hätten
  wir denselben Bruch nur mit umgekehrten Vorzeichen.
- **Nur neue Komponenten rund bauen, Bestand lassen.** Verworfen: genau so ist
  der jetzige Zustand entstanden. Ein Design-System, das nur für Neubau gilt,
  ist kein System.
- **Werte per Suchen-und-Ersetzen an den Aufrufstellen ändern.** Verworfen:
  1140 Stellen, kein Rückweg, und die nächste Nachkalibrierung wäre wieder ein
  Grossprojekt statt einer Zeile.

## Konsequenzen

### Positiv

- Eine Nachkalibrierung kostet künftig eine Zeile, nicht ein Refactoring.
- Die Leck-Stellen sind geschlossen; die Skala ist wieder die Wahrheit.
- `DESIGN.md` und Praxis sagen dasselbe. Vorher schrieb die Doku „eckig", und
  wer sie befolgte, baute gegen den Bestand.

### Negativ / Risiko

- **Der optische Diff ist gross und trifft alles auf einmal** — auch Seiten,
  die beim Umbau niemand angeschaut hat. Gegenmassnahme: nach dem Bau ein
  Bilder-Durchgang über Sessions, Tasks, Runtimes und Home; die Zahlen werden
  an Screenshots nachgezogen, nicht an einer Tabelle.
- Der Rückweg ist eine Zeile — das Risiko ist billig zu tragen.
- Die vormals nackten `rounded` liegen jetzt einheitlich auf `sm` (6px). Wo
  davon in Wahrheit ein Bedienelement steckt, gehört es auf `md` — das fällt
  im Bilder-Durchgang auf und wird gezielt gehoben, nicht pauschal.

### Fallstrick bei der Umsetzung (dokumentiert, weil er wiederkommt)

Der erste Sweep ersetzte auch das englische Wort „rounded" **in Kommentaren**
(„14px × 1.5, rounded" → „rounded-sm") und beschädigte acht Prosa-Stellen. Ein
Klassen-Sweep muss zwei Bedingungen prüfen: die Zeile ist kein Kommentar, und
der Treffer steht innerhalb eines Anführungszeichen-Paares.

## Referenzen

- Betroffene Dateien: `frontend-v2/src/styles/globals.css` (`@theme`-Block),
  `DESIGN.md` (Frontmatter, Doktrin, Shapes, Buttons, Chips, Cards),
  69 `.tsx` mit vormals nacktem `rounded`
- Verwandt: ADR-075 (Gruppenchat — dessen Dialoge waren der Anlass, an dem der
  Bruch sichtbar wurde)
