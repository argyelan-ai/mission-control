# ADR-084 — Helles Theme: Farben als CSS-Variablen, Umschalter, drei Farbregeln

**Status:** Accepted
**Datum:** 2026-09-13
**Scope:** Frontend/Design-System

## Kontext

Mission Control war „dark-mode only" (DESIGN.md, Palette „Signal"). Der Operator hat
am 13.09.2026 nach einer Design-Runde für die Task-Ansicht entschieden, dass eine
**helle Welt** („Papier, weisse Flächen, Tinte als Akzent") übersichtlicher ist und
app-weit umschaltbar sein soll — nicht nur auf einer Seite (eine helle Seite in einer
dunklen App wäre ein Fremdkörper).

Technische Ausgangslage (Bestandsaufnahme 13.09.):

- 228 Dateien lesen Farben als **JS-Konstanten** aus `lib/colors.ts` (`C.accent = "#EBE8DE"`)
  und schreiben sie in Inline-Styles — ein Umschalter, der nur CSS-Variablen setzt,
  würde diese Werte nicht erreichen.
- 458 Stellen in 84 Dateien **verketten Hex + Alpha** (`${C.error}26`) — das bricht,
  sobald ein Token keine Hex-Zeichenkette mehr ist.
- Canvas (Memory-Graph) und xterm brauchen echte Farbwerte, keine `var()`.
- Es gab keine Theme-Infrastruktur (kein `data-theme`, kein Provider, keine Präferenz).

## Entscheidung

1. **Alle Farb-Tokens sind CSS-Variablen.** `C`, `STATUS`, `LANE`, `STATUS_TEXT`, `P2`
   in `lib/colors.ts` liefern `var(--color-…)`. Die dunklen Werte stehen im
   `@theme`-Block von `globals.css` (Tailwind-Quelle), die hellen unter
   `:root[data-theme="light"]`. Ein Theme ist damit **ein Attribut auf `<html>`**.
2. **Transparenz nur über `alpha(token, a)`** (`color-mix(in srgb, … , transparent)`).
   Hex-Alpha-Verkettung ist verboten; die 458 Bestandsstellen wurden per Codemod
   umgestellt.
3. **Echte Farbwerte über `resolveColor(token)`** (liest `getComputedStyle`) — nur für
   Canvas/xterm/Export. Das xterm-Theme bleibt eine feste dunkle Konstante.
4. **`lib/theme.ts`** hält die Wahl (`dark | light | system`) in `localStorage`
   (`mc_theme`), wendet sie an und liefert ein abhängigkeitsfreies Vor-Paint-Skript
   für `<head>` (kein dunkler Blitz für helle Nutzer). Umschalter in den Settings
   („Appearance"). Server-seitige Persistenz (users.settings) ist ein Folgeschritt.
5. **Drei Farbregeln**, die in beiden Themes gelten:
   - **Terminal bleibt dunkel.** Ein Terminal ist Inhalt, nicht Chrome; Konvention und
     Kontrast der Agenten-CLIs verlangen es. Tokens `term`/`term-fg`.
   - **Messwert = Tinte, Zustand = Farbe.** GPU/RAM/Fortschritt füllen mit `fill`
     (hell: dunkles Warmgrau `#4A473F`, nie Schwarz); Zustände (arbeitet, wartet auf
     dich, fertig, blockiert) tragen die vier Status-Farben. Kontext-Balken bei Agenten
     ist ein Zustand → blau.
   - **Ein Bernstein-Vokabular.** „needs you", „pending", „review stuck", „status
     unknown" sind dieselbe Farbe mit demselben Sinn — überall.

## Alternativen

- **JS-Theme-Objekt zur Laufzeit tauschen** (`C` per Kontext/Hook) → Verworfen: 228
  Dateien müssten auf einen Hook umgestellt werden, SSR/Hydration-Konflikte bei
  Inline-Styles, doppelte Wahrheit zu den Tailwind-Tokens.
- **Nur die Task-Seite hell** → Verworfen: Fremdkörper; Farbregeln liessen sich nicht
  app-weit durchsetzen.
- **Reines Schwarz als heller Akzent für Flächen** → Verworfen (Operator 13.09.):
  Heartbeat-Blöcke, Balken und aktive Segmente stanzen Löcher ins Papier. Schwarz nur
  für Text und den einen Primär-Knopf; Flächen in `fill`.

## Konsequenzen

### Positiv
- Ein Attribut schaltet die ganze App; jede Seite ist sofort in beiden Welten prüfbar.
- `alpha()` macht Transparenz lesbar und theme-fest; Verkettungen können nicht mehr
  still brechen.
- Die Regeln sind Tokens, nicht Meinungen: `fill` statt `accent` für Messwerte.

### Negativ / Offen
- 196 Inline-Hex und 215 Inline-`rgba(...)` ausserhalb der Tokens sind dunkel getunt
  und sehen hell falsch aus. Sie werden seitenweise beim Design-Durchgang bereinigt
  (nicht Teil dieses ADR-PRs); ein Grep dafür gehört in den Privacy-/Lint-Lauf.
- `color-mix()` braucht Chrome ≥ 111 / Safari ≥ 16.2 — für den Operator-Stack erfüllt.
- DESIGN.md („dark-mode only") ist zu aktualisieren, sobald das helle Token-Set
  vom Operator abgenommen ist.
- Emoji-Icons wirken hell billig → mittelfristig Lucide-Set (eigene Entscheidung).

## Referenzen
- Design-Runde 13.09.2026: Task-Ansicht v3, heller UI-Vorschlag (Home · Tasks · Task),
  Stresstest (Sessions, Runtimes, Agents, Inbox).
- `frontend-v2/src/lib/colors.ts`, `frontend-v2/src/lib/theme.ts`,
  `frontend-v2/src/styles/globals.css`.
