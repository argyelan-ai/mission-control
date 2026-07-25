---
name: Mission Control
description: Dunkle Operations-Konsole für eine AI-Agent-Flotte — argyelan-Cyan als einziges Signal, blau-getönte Off-Blacks, eckige Präzision, Mono-Instrumentenstimme.
colors:
  accent: "#00E5FF"
  accent-hover: "#6BEAFF"
  accent-deep: "#00B4CC"
  accent-subtle: "#00E5FF19"
  border-accent: "#00E5FF4D"
  on-accent: "#00252B"
  bg-deep: "#04070C"
  bg-base: "#070B12"
  bg-surface: "#0B111C"
  bg-elevated: "#101827"
  bg-hover: "#162134"
  text-primary: "#EDF2FA"
  text-secondary: "#A5B0C2"
  text-muted: "#7E8A9E"
  text-dim: "#566178"
  border-subtle: "#92AACE0D"
  border: "#92AACE1A"
  border-active: "#92AACE29"
  status-online: "#2B9A4A"
  status-warning: "#B8870A"
  status-error: "#C23838"
  status-error-text: "#D05F5F"
  status-info: "#2E6FD8"
  status-info-text: "#5A8CE0"
  status-offline: "#3A3A3A"
  chart-cpu: "#00E5FF"
  chart-ram: "#5E83A8"
  chart-disk: "#7D92AD"
typography:
  display:
    fontFamily: "Clash Display, General Sans, sans-serif"
    letterSpacing: "-0.02em"
    note: "Seitentitel, Wordmark, KPI-Werte — weight 500–600"
  headline:
    fontFamily: "General Sans, ui-sans-serif, sans-serif"
    fontSize: "20px"
    fontWeight: 600
    letterSpacing: "-0.02em"
  title:
    fontFamily: "General Sans, ui-sans-serif, sans-serif"
    fontSize: "14px"
    fontWeight: 600
  body:
    fontFamily: "General Sans, ui-sans-serif, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "10px"
    fontWeight: 500
    letterSpacing: "+0.14em"
    note: "label-sys — uppercase Micro-Labels, die Instrumentenstimme"
  mono:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "12px"
    fontWeight: 400
rounded:
  sm: "2px"
  md: "4px"
  lg: "6px"
  xl: "10px"
  full: "9999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "16px"
  lg: "24px"
  xl: "32px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
    fontWeight: 600
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.text-secondary}"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
  card:
    backgroundColor: "{colors.bg-surface}"
    rounded: "{rounded.md}"
    padding: "16px"
    border: "1px solid {colors.border}"
  input:
    backgroundColor: "{colors.bg-deep}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.sm}"
    padding: "8px 10px"
  chip-active:
    backgroundColor: "{colors.accent-subtle}"
    textColor: "{colors.accent}"
    rounded: "{rounded.sm}"
    padding: "4px 10px"
---

# Design System: Mission Control v3

## 1. Overview

**Creative North Star: „Der Leitstand — argyelan Edition"**

Mission Control ist der ruhige, dunkle Kontrollraum, von dem aus der Operator seine AI-Agent-Flotte überwacht und steuert. v3 überführt den bewährten Leitstand-Charakter (Bloomberg-Terminal-Ernsthaftigkeit, Linear-Präzision) in die argyelan-Brand-DNA: **ein** elektrisches Cyan (#00E5FF) als einziges willentliches Signal, blau-getönte Off-Blacks statt Neutralgrau, eckige Formensprache und eine Mono-Instrumentenstimme, die jede Ansicht als Präzisionsinstrument kennzeichnet.

Dieses System lehnt explizit ab: das generische AI-Tool-Lila, Cyan→Purple→Pink-Gradients, Neon-Glow, Glassmorphism als Deko, generische Dark-Admin-Optik und SaaS-Marketing-Ästhetik. Das alte Teal (#0FA3A3, bis Juni 2026) ist abgelöst — verbleibende Teal- oder Lila-Werte im Code sind Regressionen.

**Key Characteristics:**
- Blau-getönte Off-Black-Schichtung (#04070C → #162134) — Tiefe durch Flächenton, nie durch Schatten
- Ein Akzent (Cyan #00E5FF), sparsam = wertvoll; auf Cyan-Flächen immer dunkler Text (#00252B)
- Eckige Radien (2–10px), 1px-Linien, Corner-Ticks als Positionsmarken
- JetBrains Mono als Signatur: Micro-Labels (`.label-sys`), IDs, Stats, StatusBar-Datastream
- Clash Display für Titel/Wordmark, General Sans für UI-Text
- Ruhige Motion: kurze Fades/Slides mit ease-out oder Spring, kein Bounce

## 2. Colors

Eine fast monochrome, kalt-bläuliche Architektur, in der das Cyan als einziges Signal spricht und Statusfarben leise Auskunft geben.

### Primary
- **argyelan-Cyan** (#00E5FF): Der einzige Akzent. Primäraktionen, aktive Zustände, Fokus-Ringe, „busy"-Status, Messmarken. Hover-Stufe #6BEAFF (heller, für dunklen Grund), Tiefe #00B4CC. Flächig nur als Tönung: `accent-subtle` (#00E5FF19) für aktive Chips/Hintergründe, `border-accent` (#00E5FF4D) für betonte Rahmen.
- **On-Accent** (#00252B): Text/Icons auf Cyan-Flächen — niemals Weiss auf Cyan.

### Neutral
- **Tiefschwarz-Blau-Schichtung** (#04070C / #070B12 / #0B111C / #101827 / #162134): bg-deep → bg-hover. Tiefe entsteht durch Aufhellen der Fläche, nicht durch Schatten. Nie reines #000, nie neutrales Grau.
- **Text-Treppe** (#EDF2FA / #A5B0C2 / #7E8A9E): primary für Inhalte, secondary für Beschreibungen, muted für Meta/Platzhalter (AA-sicher). **#566178 (text-dim) ist nur für Deko und inaktive Icons zugelassen, nie für Text.**
- **Kalt getönte Rahmen** (rgba(146,170,206,0.05/0.10/0.16)): subtle → active. Rahmen strukturieren, sie schmücken nicht.

### Status & Lanes
- **Online-Grün** (#2B9A4A), **Warn-Ocker** (#B8870A), **Fehler-Rot** (#C23838), **Info-Blau** (#2E6FD8), **Offline-Grau** (#3A3A3A): entsättigt, nie leuchtend. Lane-Zuordnung ausschliesslich über die `LANE`-Map in `colors.ts`.
- **Status-Text-Stufen** (#D05F5F / #5A8CE0): für Fliesstext gilt die `STATUS_TEXT`-Map; die Basistöne bleiben für Flächen, Rahmen und Icons.
- **Chart-Töne**: CPU = Cyan #00E5FF, RAM #5E83A8, Disk #7D92AD — Cyan-Blau-Familie, kein eigenes Farbuniversum.

### Named Rules
**Die Eine-Stimme-Regel.** Cyan ist die einzige Akzentfarbe und belegt ≤10% jeder Fläche. Eine zweite Akzentfarbe einzuführen ist verboten.
**Die Vokabular-Regel.** Farben kommen ausschliesslich aus `colors.ts` (`C`, `STATUS`, `LANE`). Lokale Paletten und Inline-Hex in Komponenten sind Regressionen und werden entfernt.
**Die Lila-Null-Regel.** Kein Purple/Violett in irgendeiner Form — auch nicht „nur für diese eine Karte".

## 3. Typography

**Display Font:** Clash Display (Seitentitel, Wordmark, KPI-Werte) — self-hosted via Fontshare.
**Body Font:** General Sans (UI-Text) — self-hosted.
**Mono Font:** JetBrains Mono (Micro-Labels, IDs, Terminals, Logs, Zahlenkolonnen) — bundled, die Signatur-Stimme.

**Character:** Eine Ingenieur-Stimme: markante Display-Schrift für Momente, neutrales Sans für Arbeit, Mono für alles, was ein Instrument ansagen würde. Hierarchie entsteht über Grösse + Gewicht (400/500/600), nicht über Farbwechsel.

### Hierarchy
- **Display** (Clash Display, 500–600, -0.02em): Seitentitel (eine pro Seite), grosse KPI-Werte, Wordmark. Utility `.display`.
- **Title** (600, 14px): Sektions- und Kartentitel.
- **Body** (400, 13px, lh 1.6): Inhalte, Beschreibungen, Kommentare.
- **Label-sys** (JetBrains Mono, 500, 10px, uppercase, +0.14em): Sektions-Marken, Formular-Labels, Meta-Zeilen. Utility `.label-sys` (+ Varianten `--accent`, `--dim`).
- **Mono** (400, 12px): Task-IDs, Branch-Namen, Terminal-Inhalte, Metriken.

### Named Rules
**Die Dichte-Regel.** Leitstand-Dichte ist gewollt: 11–14px UI-Text ist Standard, aber jede Stufe unter 13px braucht ≥5.3:1 Kontrast und 500er-Gewicht oder besser.
**Die Verbotene-Fonts-Regel.** Kein Inter/Roboto/Arial/system-ui als UI-Font; Geist ist mit v3 ausgemustert.

## 4. Elevation

Flach per Doktrin. Tiefe entsteht durch tonale Schichtung (bg-deep → bg-hover), nicht durch Schatten. Schatten existieren nur an Overlays, die physisch über der Seite liegen (Modals, Dropdowns, Drawer) — dunkel und diffus, nie farbig, nie glühend. `backdrop-blur` ist kein Gestaltungsmittel; die Legacy-`GlassCard` ist v3 entglast (flache Surface, Name aus Kompatibilität behalten).

### Shadow Vocabulary
- **overlay** (`box-shadow: 0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)`): Nur für Modals/Dropdowns/Drawer.

### Named Rules
**Die Flach-Regel.** Karten und Sektionen im Seitenfluss tragen keinen Schatten — Rahmen + Flächenton genügen. Wer einen Schatten setzen will, baut in Wahrheit ein Overlay.

## 5. Signatur-Elemente (v3)

Drei wiedererkennbare Marken, die jede Ansicht als argyelan-Leitstand kennzeichnen:

1. **Ruhige Bühne** — der Hintergrund ist leer: ein dezenter Cyan-Schleier + Grain, keine Raster/Muster (Operator-Entscheid v3.1). Der Fokus gehört dem Inhalt; Präzision zeigt sich in Typografie und Linien, nicht in Deko.
2. **Corner-Ticks** (`.corner-ticks`) — 10px-Cyan-Eckmarken (TL+BR) auf hero-artigen Panels. Max 1–2 pro View.
3. **Messmarke** — 1px-Linie mit 64px-Cyan-Segment links, der Header-Trenner auf Seiten.
4. **Mono-Datastream** — StatusBar als Instrumentenzeile: `SYS OK · AGT 14/15 · BRD … · 18:43:44`, JetBrains Mono uppercase.

Zusätzlich: **Cyan-Kante** (2px) oben auf Modals/Sheets — die „Gehäuse-Markierung" von Overlays.

## 6. Components

Werkzeuge, keine Schmuckstücke: zurückhaltend im Ruhezustand, eindeutig im aktiven Zustand.

### Buttons
- **Shape:** Eckig (2–4px)
- **Primary:** Cyan-Fläche (#00E5FF) mit dunklem Text (#00252B), Hover = brightness +10%; kein Gradient nötig
- **Hover / Focus:** Aufhellen um eine Stufe; Fokus = 2px Cyan-Ring mit 2px Offset (global via `:focus-visible`)
- **Ghost:** Transparent, 1px Rahmen (border), Text secondary; Hover → bg-hover + Text primary
- **Destruktiv:** Fehler-Rot #C23838 nur für endgültige Aktionen, sonst Ghost mit rotem Text

### Chips / Pills
- **Style:** Farbton-22-Muster — `${farbe}22` Hintergrund, `${farbe}55` Rahmen, Farbtext bei aktiv; inaktiv transparent mit border + text-muted. Eckig (rounded-sm), kein text-shadow.
- **State:** Aktiv-Zustand immer über Farbe UND Rahmen, nie nur über Text

### Cards / Containers
- **Corner Style:** 4–6px
- **Background:** bg-surface (#0B111C) oder bg-elevated (#101827), je eine Stufe über dem Seitengrund
- **Shadow Strategy:** Keine (Flach-Regel); Hover hellt Fläche/Rahmen auf
- **Border:** 1px border (rgba(146,170,206,0.10))
- **Internal Padding:** 16px (kompakt 12px)

### Inputs / Fields
- **Style:** bg-deep (#04070C) Fläche, 1px Rahmen, 2–4px Radius, Text primary, Platzhalter text-muted
- **Focus:** Cyan-Ring (global), Rahmen → border-accent
- **Label:** `.label-sys` (mono uppercase), oberhalb des Felds, immer mit `htmlFor`/`id` oder `aria-label`

### Status-Anzeigen (Signature)
- **StatusDot:** 8px Punkt in STATUS-Farbe + Textlabel; Farbe allein trägt nie die Information. In Instrument-Kontexten (System Health) eckig statt rund.
- **Lane-Header & Priority-Marker:** Farben ausschliesslich aus `LANE`; Priorität: critical=Rot, high=Ocker, medium=neutral, low=transparent.

### Navigation
- **Sidebar (Desktop):** bg-base, Einträge 13px, Gruppen-Marken via `.label-sys`; aktiv = accent-subtle Fläche + 2px-Cyan-Balken links (eckig) + accent-light Text; inaktiv text-secondary, Hover bg-hover. Wordmark in Clash Display, Akzent-Teil in Cyan.
- **Mobile:** Bottom-Tab-Bar (Home/Tasks/Agents/Sessions/Menü) in der Daumen-Zone, safe-area-aware; voller Nav-Tree als Drawer von rechts. Top-Bar trägt nur Wordmark + Voice.

## 7. Do's and Don'ts

### Do:
- **Do** jede Farbe aus `colors.ts` beziehen (`C`, `STATUS`, `LANE`) — neue Bedeutung ⇒ neues Token, erst dann verwenden.
- **Do** Tiefe über Flächenton lösen (eine Stufe heller = eine Ebene höher).
- **Do** Kontraste prüfen: Body/Labels ≥4.5:1; auf Cyan-Flächen immer #00252B-Text.
- **Do** jeden interaktiven Zustand sichtbar machen: Hover, Fokus-Ring, aktive Chips mit Fläche+Rahmen.
- **Do** `prefers-reduced-motion` respektieren; Motion = kurzes Fade/Slide mit ease-out (100–300ms), nur transform+opacity.
- **Do** Touch-Targets ≥44px und Safe-Areas auf iPhone einhalten (Tab-Bar, pt-safe/pb-safe).

### Don't:
- **Don't** Lila/Violett in irgendeiner Form — die zentrale Anti-Referenz.
- **Don't** Neon-Glow, farbige Schatten, `backdrop-blur` als Deko — „Glassmorphism als Default" ist verboten.
- **Don't** Gradient-Text (`background-clip: text`) und Cyan→irgendwas-Verläufe — Cyan steht solo.
- **Don't** lokale Farbpaletten oder Inline-Hex in Komponenten anlegen — auch nicht „nur temporär".
- **Don't** Statusfarben für Deko nutzen oder pro Seite umdeuten.
- **Don't** identische Karten-Grids ohne Bento-Varianz, Sun/Moon-Toggles, Stock-Metaphern — Arbeitskonsole mit Signatur, keine Landing Page.
