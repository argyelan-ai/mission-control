---
name: Mission Control
description: Dunkle Operations-Konsole für eine AI-Agent-Flotte — ein Teal-Signal, Grau für Struktur, Status als Information.
colors:
  accent: "#0FA3A3"
  accent-hover: "#14C4C4"
  accent-subtle: "#0FA3A31F"
  border-accent: "#0FA3A34D"
  bg-deep: "#050505"
  bg-base: "#0A0A0A"
  bg-surface: "#111111"
  bg-elevated: "#161616"
  bg-hover: "#1C1C1C"
  text-primary: "#EDEDED"
  text-secondary: "#A1A1A1"
  text-muted: "#888888"
  text-dim: "#6E6E6E"
  border-subtle: "#FFFFFF0A"
  border: "#FFFFFF0F"
  border-active: "#FFFFFF1A"
  status-online: "#2B9A4A"
  status-warning: "#B8870A"
  status-error: "#C23838"
  status-error-text: "#D05F5F"
  status-info: "#2E6FD8"
  status-info-text: "#5A8CE0"
  status-offline: "#3A3A3A"
  chart-ram: "#6B8E8E"
  chart-disk: "#86A0A0"
typography:
  headline:
    fontFamily: "Geist Sans, system-ui, sans-serif"
    fontSize: "20px"
    fontWeight: 600
    letterSpacing: "-0.02em"
  title:
    fontFamily: "Geist Sans, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 600
  body:
    fontFamily: "Geist Sans, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: "Geist Sans, system-ui, sans-serif"
    fontSize: "11px"
    fontWeight: 500
  mono:
    fontFamily: "Geist Mono, ui-monospace, monospace"
    fontSize: "12px"
    fontWeight: 400
rounded:
  sm: "6px"
  md: "8px"
  lg: "12px"
  xl: "16px"
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
    textColor: "{colors.text-primary}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.text-secondary}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
  card:
    backgroundColor: "{colors.bg-surface}"
    rounded: "{rounded.lg}"
    padding: "16px"
  input:
    backgroundColor: "{colors.bg-deep}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.lg}"
    padding: "8px 10px"
  chip-active:
    backgroundColor: "{colors.accent-subtle}"
    textColor: "{colors.accent}"
    rounded: "{rounded.md}"
    padding: "4px 10px"
---

# Design System: Mission Control

## 1. Overview

**Creative North Star: "Der Leitstand"**

Mission Control ist ein Leitstand: der ruhige, dunkle Kontrollraum, von dem aus der Operator seine AI-Agent-Flotte überwacht und steuert. Die Oberfläche verhält sich wie die Instrumententafel eines Kraftwerks bei Nacht — fast schwarz, präzise beschriftet, und Farbe erscheint ausschliesslich dort, wo sie etwas bedeutet: ein Teal-Signal für Aktion und Fokus, gedämpfte Zustandsfarben für die Lage der Flotte. Referenzpunkte sind Bloomberg Terminal (Ernsthaftigkeit, Dichte), Linear.app (Präzision, Reduktion) und Stripe Dashboard (Klarheit).

Dieses System lehnt explizit ab, was PRODUCT.md als Anti-Referenzen führt: das generische AI-Tool-Lila (#8B5CF6/#7C3AED), Neon-Glow, Glassmorphism als Deko und SaaS-Marketing-Ästhetik. MC trug diesen Look bis Juni 2026 und hat ihn mit der MC=Teal-Entscheidung bewusst abgelegt — jeder verbleibende Lila-Wert im Code ist eine Regression, kein Stilmittel.

**Key Characteristics:**
- Fast-schwarze tonale Schichtung (#050505 → #1C1C1C) statt Schatten-Tiefe
- Ein Akzent (Teal #0FA3A3), bewusst entsättigt — nie Neon
- Status-Vokabular app-weit identisch (STATUS/LANE in `colors.ts`)
- Hohe Informationsdichte: 11–14px UI-Text, klare Label-Hierarchie
- Ruhige Motion: kurze Fades/Slides mit ease-out, kein Bounce

## 2. Colors

Eine fast monochrome Grau-Architektur, in der das Teal als einziges willentliches Signal spricht und Statusfarben leise Auskunft geben.

### Primary
- **Leitstand-Teal** (#0FA3A3): Der einzige Akzent. Primäraktionen, aktive Zustände, Fokus-Ringe, „busy"-Status. Hover-Stufe #14C4C4. Flächig nur als Tönung: `accent-subtle` (#0FA3A31F) für aktive Chips/Hintergründe, `border-accent` (#0FA3A34D) für betonte Rahmen.

### Neutral
- **Tiefschwarz-Schichtung** (#050505 / #0A0A0A / #111111 / #161616 / #1C1C1C): bg-deep → bg-hover. Tiefe entsteht durch Aufhellen der Fläche, nicht durch Schatten. Inputs liegen auf bg-deep, Karten auf bg-surface/bg-elevated, Hover hellt eine Stufe auf.
- **Text-Treppe** (#EDEDED / #A1A1A1 / #888888): primary für Inhalte, secondary für Beschreibungen (~7.3:1), muted für Meta/Platzhalter (~5.3:1 — AA-sicher). **#6E6E6E (text-dim) ist nur für Deko und inaktive Icons zugelassen, nie für Text.**
- **Weiss-Alpha-Rahmen** (#FFFFFF0A / #FFFFFF0F / #FFFFFF1A): subtle → active. Rahmen strukturieren, sie schmücken nicht.

### Status & Lanes
- **Online-Grün** (#2B9A4A), **Warn-Ocker** (#B8870A), **Fehler-Rot** (#C23838), **Info-Blau** (#2E6FD8), **Offline-Grau** (#3A3A3A): entsättigt, nie leuchtend. Lane-Zuordnung (inbox/in_progress/review/…) ausschliesslich über die `LANE`-Map in `colors.ts`.
- **Status-Text-Stufen** (#D05F5F / #5A8CE0): Fehler-Rot und Info-Blau erreichen als Fliesstext keine 4.5:1 — für Text gilt die `STATUS_TEXT`-Map; die Basistöne bleiben für Flächen, Rahmen und Icons.
- **Chart-Töne**: CPU = Teal, RAM #6B8E8E, Disk #86A0A0 — Teal-Grau-Familie, kein eigenes Farbuniversum.

### Named Rules
**Die Eine-Stimme-Regel.** Teal ist die einzige Akzentfarbe und belegt ≤10% jeder Fläche. Eine zweite Akzentfarbe einzuführen ist verboten.
**Die Vokabular-Regel.** Farben kommen ausschliesslich aus `colors.ts` (`C`, `STATUS`, `LANE`). Lokale Paletten und Inline-Hex in Komponenten sind Regressionen und werden entfernt.
**Die Lila-Null-Regel.** Kein Purple/Violett in irgendeiner Form — auch nicht „nur für diese eine Karte".

## 3. Typography

**Body Font:** Geist Sans (mit system-ui Fallback)
**Mono Font:** Geist Mono (IDs, Terminals, Logs, Zahlenkolonnen)
**Wordmark:** Space Grotesk — ausschliesslich für den MC-Schriftzug, nie für UI-Text.

**Character:** Ein technisches, neutrales Sans-Paar — die Stimme eines Instruments, nicht einer Marke. Hierarchie entsteht über Grösse + Gewicht (400/500/600), nicht über Farbwechsel.

### Hierarchy
- **Headline** (600, 20px, -0.02em): Seitentitel, eine pro Seite.
- **Title** (600, 14px): Sektions- und Kartentitel.
- **Body** (400, 13px, lh 1.6): Inhalte, Beschreibungen, Kommentare.
- **Label** (500, 11px): Formular-Labels, Meta-Angaben, Tabellen-Header. Uppercase nur für ≤2 Wörter mit +0.05em Tracking.
- **Mono** (400, 12px): Task-IDs, Branch-Namen, Terminal-Inhalte, Metriken.

### Named Rules
**Die Dichte-Regel.** Leitstand-Dichte ist gewollt: 11–14px UI-Text ist Standard, aber jede Stufe unter 13px braucht ≥5.3:1 Kontrast und 500er-Gewicht oder besser.

## 4. Elevation

Flach per Doktrin. Tiefe entsteht durch tonale Schichtung (bg-deep → bg-hover), nicht durch Schatten. Schatten existieren nur an Overlays, die physisch über der Seite liegen (Modals, Dropdowns, Drawer) — dunkel und diffus, nie farbig, nie glühend. `backdrop-blur` ist kein Gestaltungsmittel; die Legacy-Klasse `.glass-card` wird ausgemustert.

### Shadow Vocabulary
- **overlay** (`box-shadow: 0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)`): Nur für Modals/Dropdowns/Drawer.

### Named Rules
**Die Flach-Regel.** Karten und Sektionen im Seitenfluss tragen keinen Schatten — Rahmen + Flächenton genügen. Wer einen Schatten setzen will, baut in Wahrheit ein Overlay.

## 5. Components

Werkzeuge, keine Schmuckstücke: zurückhaltend im Ruhezustand, eindeutig im aktiven Zustand.

### Buttons
- **Shape:** Sanft gerundet (8px)
- **Primary:** Teal-Fläche (#0FA3A3, Hover #14C4C4) mit hellem Text; grosse Submit-Aktionen dürfen den dezenten Verlauf `linear-gradient(135deg, #14C4C4, #0FA3A3)` tragen
- **Hover / Focus:** Aufhellen um eine Stufe; Fokus = 2px Teal-Ring mit 2px Offset (global via `:focus-visible`)
- **Ghost:** Transparent, 1px Rahmen (#FFFFFF0F), Text secondary; Hover → bg-hover + Text primary
- **Destruktiv:** Fehler-Rot #C23838 nur für endgültige Aktionen, sonst Ghost mit rotem Text

### Chips / Pills
- **Style:** Farbton-22-Muster — `${farbe}22` Hintergrund, `${farbe}55` Rahmen, Farbtext bei aktiv; inaktiv transparent mit border + text-muted
- **State:** Aktiv-Zustand immer über Farbe UND Rahmen, nie nur über Text

### Cards / Containers
- **Corner Style:** 12px
- **Background:** bg-surface (#111111) oder bg-elevated (#161616), je eine Stufe über dem Seitengrund
- **Shadow Strategy:** Keine (Flach-Regel); Hover hellt Fläche/Rahmen auf
- **Border:** 1px #FFFFFF0F
- **Internal Padding:** 16px (kompakt 12px)

### Inputs / Fields
- **Style:** bg-deep (#050505) Fläche, 1px Rahmen, 12px Radius, Text primary, Platzhalter text-muted
- **Focus:** Teal-Ring (global), Rahmen → border-accent
- **Label:** 11px, 500, oberhalb des Felds, immer mit `htmlFor`/`id` oder `aria-label`

### Status-Anzeigen (Signature)
- **StatusDot:** 8px Punkt in STATUS-Farbe + Textlabel; Farbe allein trägt nie die Information
- **Lane-Header & Priority-Marker:** Farben ausschliesslich aus `LANE`; Priorität: critical=Rot, high=Ocker, medium=neutral, low=transparent — Lila ist als Prioritätsfarbe abgeschafft

### Navigation
- **Sidebar:** bg-base, Einträge 13px; aktiv = accent-subtle Fläche + Teal-Icon; inaktiv text-secondary, Hover bg-hover

## 6. Do's and Don'ts

### Do:
- **Do** jede Farbe aus `colors.ts` beziehen (`C`, `STATUS`, `LANE`) — neue Bedeutung ⇒ neues Token, erst dann verwenden.
- **Do** Tiefe über Flächenton lösen (eine Stufe heller = eine Ebene höher).
- **Do** Kontraste prüfen: Body/Labels ≥4.5:1; text-muted (#888888) ist die dunkelste zulässige Textfarbe.
- **Do** jeden interaktiven Zustand sichtbar machen: Hover, Fokus-Ring, aktive Chips mit Fläche+Rahmen.
- **Do** `prefers-reduced-motion` respektieren; Motion = kurzes Fade/Slide mit ease-out (100–300ms).

### Don't:
- **Don't** Lila/Violett in irgendeiner Form — das „generische AI-Tool-Lila" (#8B5CF6, #7C3AED, #A78BFA) ist die zentrale Anti-Referenz aus PRODUCT.md.
- **Don't** Neon-Glow, farbige Schatten, `backdrop-blur` als Deko — „Glassmorphism als Default" ist verboten; `.glass-card` nicht neu verwenden.
- **Don't** Gradient-Text (`background-clip: text`) — `GradientText` wird entfernt, nicht nachgeahmt.
- **Don't** lokale Farbpaletten oder Inline-Hex in Komponenten anlegen — auch nicht „nur temporär".
- **Don't** Statusfarben für Deko nutzen oder pro Seite umdeuten — „Status-Feuerwerk" ist eine Anti-Referenz.
- **Don't** Side-Stripe-Borders >1px als Farbakzent, Hero-Metrik-Schablonen oder identische Karten-Grids — Arbeitskonsole, keine Landing Page.
