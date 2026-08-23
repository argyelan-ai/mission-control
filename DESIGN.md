---
name: Mission Control
description: Dunkle, achromatische Operations-Konsole für eine AI-Agent-Flotte — der einzige Akzent ist ein fast-weisses Off-Cream (#EBE8DE), das über Helligkeit trägt; Farbe bedeutet ausschliesslich Status. Neutrale Off-Blacks, runde Flächen bei eckigem Raster, Mono-Instrumentenstimme.
colors:
  accent: "#EBE8DE"
  accent-hover: "#F9F7EF"
  accent-deep: "#C1BEB2"
  accent-subtle: "rgba(235,232,222,0.10)"
  border-accent: "rgba(235,232,222,0.30)"
  on-accent: "#151411"
  bg-deep: "#0A0A0A"
  bg-base: "#101010"
  bg-surface: "#171717"
  bg-elevated: "#222222"
  bg-hover: "#2C2C2C"
  text-primary: "#EEEEEE"
  text-secondary: "#BABABA"
  text-muted: "#8F8F8F"
  text-dim: "#666666"
  border-subtle: "rgba(168,168,168,0.05)"
  border: "rgba(168,168,168,0.10)"
  border-active: "rgba(168,168,168,0.16)"
  status-online: "#55A964"
  status-warning: "#A67F3E"
  status-warning-text: "#B98F4D"
  status-error: "#FA4942"
  status-info: "#5890CA"
  status-offline: "#3A3A3A"
  chart-cpu: "#EBE8DE"
  chart-ram: "#8F8F8F"
  chart-disk: "#666666"
typography:
  display:
    fontFamily: "Clash Display, General Sans, sans-serif"
    letterSpacing: "-0.02em"
    note: "Seitentitel, Wordmark, KPI-Werte — weight 500–600"
  title:
    fontFamily: "General Sans, ui-sans-serif, sans-serif"
    fontSize: "16px"
    fontWeight: 600
  body:
    fontFamily: "General Sans, ui-sans-serif, sans-serif"
    fontSize: "14px"
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
  dense: "4px"
  sm: "6px"
  md: "10px"
  lg: "14px"
  xl: "20px"
  2xl: "28px"
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
    rounded: "{rounded.md}"
    padding: "8px 14px"
    fontWeight: 500
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.text-secondary}"
    rounded: "{rounded.md}"
    padding: "8px 14px"
    border: "1px solid {colors.border-active}"
  card:
    backgroundColor: "{colors.bg-surface}"
    rounded: "{rounded.md}"
    padding: "16px"
    border: "1px solid {colors.border}"
  input:
    backgroundColor: "{colors.bg-deep}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.md}"
    padding: "8px 12px"
  chip-active:
    backgroundColor: "{colors.accent-subtle}"
    textColor: "{colors.accent}"
    rounded: "{rounded.md}"
    padding: "4px 10px"
  switch:
    trackWidth: "36px"
    trackHeight: "20px"
    onColor: "{colors.accent}"
    thumbColor: "{colors.on-accent}"
---

# Design System: Mission Control v4 „Signal"

## Overview

**Creative North Star: „Der Leitstand — Signal"**

Mission Control ist der ruhige, dunkle Instrumentenraum, von dem aus der Operator seine AI-Agent-Flotte überwacht und steuert. v4 „Signal" zieht die Konsole radikal achromatisch: Struktur und Interaktion sprechen ausschliesslich über Helligkeit und Fläche, **Farbe ist allein den vier Statustönen vorbehalten**. Der Primär-Akzent ist kein Farbton, sondern ein fast-weisses Off-Cream (#EBE8DE) — es trägt über Leuchtkraft und Position, nie über Buntheit. Off-Blacks sind neutral (nie blaustichig, nie reines #000), die Formensprache ist rund für alles Anfassbare und eckig nur noch für dichte Daten, und eine Mono-Instrumentenstimme (JetBrains Mono) kennzeichnet jede Ansicht als Präzisionsgerät. Referenzen: Bloomberg Terminal (Dichte + Ernsthaftigkeit), Linear.app (Präzision + Reduktion), Stripe Dashboard (Klarheit). Die Doktrin steht wörtlich im Kopf von `colors.ts`: „Serious. Dark. Achromatic. Colour means status — nothing else."

Dieses System lehnt explizit ab: das generische AI-Tool-Lila, Farb-Gradients, Neon-Glow, Glassmorphism/`backdrop-blur` als Deko, farbige Schatten und SaaS-Marketing-Ästhetik.

**v3 ist zurückgezogen.** Bis Juli 2026 war der Akzent ein elektrisches argyelan-Cyan (#00E5FF) auf blau-getönten Off-Blacks (System „argyelan Edition"). Dieser Look ist vollständig abgelöst: **jeder verbleibende Cyan-, Teal- oder Lila-Wert im Code ist eine Regression** (Ausnahmen sind explizit dokumentiert — siehe „Do's and Don'ts"). Einzige Farbquelle ist `frontend-v2/src/lib/colors.ts`; `styles/globals.css` spiegelt dieselben Werte als CSS-Custom-Properties.

**Key Characteristics:**
- Neutrale Off-Black-Schichtung (#0A0A0A → #2C2C2C) — Tiefe durch Flächenton, nie durch Schatten
- Ein Akzent, achromatisch hell (#EBE8DE): trägt über Helligkeit + Fläche, ≤10% jeder Fläche; Text darauf immer dunkel (#151411)
- Farbe = Status. Nur vier bunte Töne existieren (Grün/Ocker/Rot/Blau); Fehler schlägt Warnung über Buntheit, nicht Helligkeit
- Runde Radien (6–28px) für Anfassbares, 4px im Raster; 1px-Linien, Corner-Ticks als Positionsmarken
- JetBrains Mono als Signatur: Micro-Labels (`.label-sys`), IDs, Stats, Datastream — Clash Display für Titel/Wordmark, General Sans für UI-Text
- Ruhige Motion: kurze Fades/Slides mit ease-out, kein Bounce, kein Glow

## Colors

Eine fast monochrome, neutral-dunkle Architektur, in der der helle Akzent über Leuchtkraft spricht und die vier Statustöne die einzige Buntheit tragen.

### Primary (achromatisch)
- **Signal-Akzent** (#EBE8DE): Der einzige Akzent — ein Off-Cream, kein Farbton. Primäraktionen, aktive Zustände, Fokus-Ringe, Selektion, Messmarken. Hover-Stufe #F9F7EF (heller), Tiefe #C1BEB2 (gedimmt, für Rahmen/Verlaufsanfang). Flächig nur als Tönung: `accent-subtle` (rgba(235,232,222,0.10)) für aktive Chips/Selektion, `border-accent` (rgba(235,232,222,0.30)) für betonte Rahmen.
- **On-Accent** (#151411): Text/Icons auf Akzent-Flächen — 15:1 Kontrast, „Reverse Video". Niemals Weiss auf dem hellen Akzent.

### Neutral
- **Off-Black-Schichtung** (#0A0A0A / #101010 / #171717 / #222222 / #2C2C2C): bg-deep → bg-hover. Tiefe entsteht durch Aufhellen der Fläche. Nie reines #000, nie blaustichig.
- **Text-Treppe** (#EEEEEE / #BABABA / #8F8F8F): primary für Inhalte, secondary für Beschreibungen, muted für Meta/Platzhalter — alle ≥4.5:1 auf bg-deep–bg-elevated. **#666666 (text-dim) ist nur für Deko und inaktive Icons zugelassen, nie für Fliesstext.**
- **Neutrale Rahmen** (Basisfarbe #A8A8A8 mit Alpha 0.05 / 0.10 / 0.16): subtle → active. Rahmen strukturieren, sie schmücken nicht.

### Status & Lanes (die einzige Buntheit)
- **Online-Grün** (#55A964), **Warn-Ocker** (#A67F3E), **Fehler-Rot** (#FA4942), **Info-Blau** (#5890CA), **Offline-Grau** (#3A3A3A): entsättigt, kein Glow. „busy"/„in Arbeit" ist ein Info-Zustand (Blau), kein Akzent. Der wartende-auf-Operator-Zustand (`user_test`) trägt den hellsten Ton (accent #EBE8DE), nicht Buntheit. Lane-Zuordnung ausschliesslich über die `LANE`-Map in `colors.ts`.
- **Fehler vor Warnung über Chroma:** Rot (Chroma .215) schlägt Ocker (.095) durch Sättigung, nicht durch Helligkeit — beide bleiben gleich hell.
- **Status-Text-Stufe** (#B98F4D): für Fliesstext auf Karten gilt die `STATUS_TEXT`-Map; nur das Ocker wird auf #B98F4D geliftet (Token selbst = 4.34:1 auf #222222). Grün/Rot/Blau bleiben unverändert AA-sicher.
- **Chart-Töne**: CPU = accent #EBE8DE, RAM #8F8F8F, Disk #666666 — Ressourcen-Serien tragen über Helligkeit, nicht über Farbton.

### Named Rules
**Die Eine-Stimme-Regel.** Es gibt genau einen Akzent, und er ist achromatisch: Helligkeit ist das Signal. Er belegt ≤10% jeder Fläche. Einen zweiten (bunten) Akzent einzuführen ist verboten.
**Die Farbe-heisst-Status-Regel (v4).** Ist etwas bunt, muss es etwas bedeuten. Buntheit ist ausschliesslich den vier Statustönen vorbehalten — keine dekorative Farbe, kein „nur für die Optik".
**Die Vokabular-Regel.** Farben kommen ausschliesslich aus `colors.ts` (`C`, `STATUS`, `LANE`, `STATUS_TEXT`, `P2`). Lokale Paletten und Inline-Hex in Komponenten sind Regressionen und werden entfernt.
**Die Lila-Null-Regel.** Kein Purple/Violett in irgendeiner Form — auch nicht „nur für diese eine Karte".

## Typography

**Display Font:** Clash Display (Seitentitel, Wordmark, KPI-Werte) — self-hosted (`public/fonts/`, Schnitte 400/500/600/700), via `.display` und `--font-display`.
**Body Font:** General Sans (UI-Text) — self-hosted (400/500/600), `--font-sans`, app-weiter Default auf `<body>`.
**Mono Font:** JetBrains Mono (Micro-Labels, IDs, Terminals, Logs, Zahlenkolonnen) — self-hosted (400/700), `--font-mono`, die Signatur-Stimme. Symbols Nerd Font Mono ergänzt Terminal-Glyphen.

**Character:** Eine Ingenieur-Stimme: markante Display-Schrift für Momente, neutrales Sans für Arbeit, Mono für alles, was ein Instrument ansagen würde. Hierarchie entsteht über Grösse + Gewicht (400/500/600), nicht über Farbwechsel.

### Hierarchy
- **Display** (Clash Display, 500–600, -0.02em): Seitentitel (eine pro Seite), grosse KPI-Werte (30px, `.display`), Wordmark.
- **Title** (General Sans, 600, 16px): Sektions- und Modal-/Kartentitel (`h2`).
- **Body** (General Sans, 400, 14px, lh 1.6): Inhalte, Beschreibungen, Kommentare. UI-Detailtext oft 11–13px.
- **Label-sys** (JetBrains Mono, 500, 10px, uppercase, +0.14em, text-muted): Sektions-Marken, Formular-Labels, Meta-Zeilen. Utility `.label-sys` (+ Varianten `--accent`, `--dim`).
- **Mono** (JetBrains Mono, 400, 12px): Task-IDs, Branch-Namen, Terminal-Inhalte, Einheiten-Suffixe an Zahlenfeldern, Metriken.

### Named Rules
**Die Dichte-Regel.** Leitstand-Dichte ist gewollt: 11–14px UI-Text ist Standard, aber jede Stufe unter 13px braucht hohen Kontrast und 500er-Gewicht oder besser.
**Die Verbotene-Fonts-Regel.** Kein Inter/Roboto/Arial/system-ui als UI-Font; die drei self-hosted Familien sind gesetzt. `system-ui` steht nur als Fallback in der Font-Stack.

## Layout

- **App-Shell:** feste Sidebar (Desktop) bzw. Bottom-Tab-Bar (Mobile) + TopBar + StatusBar. Die Shell nutzt ausschliesslich das `P2`-Token-Set aus `colors.ts` (auf dieselben System-A-Werte wie `C` gezogen), die Seiten nutzen `C` — beide sind seit v4 ein System.
- **Dichte statt Weite:** kompakte Paddings (Karten 16px, kompakt 12px), enge Zeilenabstände, 1px-Trenner. Sektionen im Formular werden durch eine Mono-Micro-Label-Zeile + 1px-Hairline (`border-subtle`) eröffnet, nicht durch grosse Überschriften.
- **Progressive Disclosure:** selten genutzte Optionen leben hinter einer „Erweitert"-Klappe (Chevron + `Settings2`-Icon, Mono-Uppercase-Label), animiert über Höhe+Opacity (0.15–0.2s).
- **Spacing-Rhythmus:** 4 / 8 / 12 / 16 / 24 / 32 / 64px (`--space-*`). Formularfelder-Abstand 16–20px, Rail-Abstand 20px.
- **Breakpoints:** md 768px (Mobile→Desktop-Wechsel: Bottom-Nav→Sidebar, Modal-slide-up→zentriert), lg 1024px (zweispaltige Layouts wie die Task-Maske hero+rail).
- **Mobile-Disziplin:** Touch-Targets ≥44px, Safe-Areas (`pt-safe`/`pb-safe`, Dynamic Island), Pinch-Zoom nie blockiert, iOS-Input-Font ≥16px gegen Auto-Zoom.

## Elevation & Depth

Flach per Doktrin. Tiefe entsteht durch tonale Schichtung (bg-deep → bg-hover), nicht durch Schatten. Schatten existieren nur an Overlays, die physisch über der Seite liegen (Modals, Dropdowns, Drawer) — dunkel und diffus, nie farbig, nie glühend. `backdrop-blur` ist kein Gestaltungsmittel. Kein Glow: ein weisser Halo wäre auf Schwarz das lauteste Element der Seite; Selektion trägt deshalb über Fläche + Rahmen, nicht über Schein.

### Shadow Vocabulary
- **card** (`0 1px 2px rgba(0,0,0,0.3), 0 4px 16px rgba(0,0,0,0.2)`): definierter Token für leicht abhebende Flächen — sparsam; Flächen im Seitenfluss bleiben schattenlos.
- **elevated** (`0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)`): Modals/Dropdowns/Drawer. Der ResponsiveModal-Panel nutzt exakt diesen Schatten.

### Named Rules
**Die Flach-Regel.** Karten und Sektionen im Seitenfluss tragen keinen Schatten — Rahmen + Flächenton genügen. Wer einen Schatten setzen will, baut in Wahrheit ein Overlay.
**Die Kein-Halo-Regel (v4).** Aktive/selektierte Elemente heben sich über `accent-subtle`-Fläche + Akzent-Rahmen; ein Glow/`box-shadow` in Akzentfarbe ist verboten.

## Shapes

Runde Formensprache für alles Anfassbare, eckig nur im Raster — plus die
wiedererkennbaren Instrumenten-Marken.

**Die Regel (ADR-076, Operator-Entscheid 23.08.2026):** Rund bekommt, was man
anfässt oder was schwebt. Eckig bleibt, was dicht im Raster liegt — dort kämpft
ein Radius gegen die Kante, kostet Platz, den Daten brauchen, und macht weich,
wo man Präzision liest.

Gewählt wird die **Rolle**, nicht die Zahl. Die Werte hängen an den Tokens in
`globals.css`; eine Nachkalibrierung passiert dort und nirgends sonst.

| Rolle | Klasse | Wert | Wofür |
|---|---|---|---|
| Dicht / Raster | `rounded-dense` | 4px | Tabellenzelle, Terminal, Code-Block, Log-Zeile |
| Marker | `rounded-sm` | 6px | Punkt, Mini-Chip, Badge |
| Bedienelement | `rounded-md` | 10px | Knopf, Eingabefeld |
| Karte | `rounded-lg` | 14px | Listenzeile, Kachel |
| Fläche | `rounded-xl` | 20px | Insel, Panel |
| Schwebend | `rounded-2xl` | 28px | Dialog, Sheet |
| Pill | `rounded-full` | ∞ | Chip, Avatar, Switch-Track |

**Nie eine freie Zahl** (`rounded-[13px]`) und **nie das nackte `rounded`** —
beides umgeht die Skala. Genau das war der Grund für den Bruch: 166 nackte
`rounded` und ein undefiniertes `rounded-2xl` liefen an den Tokens vorbei,
während die Doku „eckig" vorschrieb und die Praxis längst rundete.
- **1px-Linien:** Rahmen, Hairline-Trenner (`border-subtle`), Sektions-Unterkanten. Struktur zeigt sich in Linien, nicht in Flächenschmuck.
- **Corner-Ticks** (`.corner-ticks`): 10px-Eckmarken (oben-links + unten-rechts) in `border-accent` auf hero-artigen/aktiven Panels. Signatur-Detail — sparsam, max 1–2 pro View.
- **Akzent-Kante:** 2px `accent`-Streifen oben auf Modals/Sheets — die „Gehäuse-Markierung" von Overlays.
- **Status-Dot:** kleiner Punkt (rund; im dichten Raster `dense`) in STATUS-Farbe, immer mit Textlabel — Farbe trägt nie allein.
- **Mono-Datastream:** StatusBar als Instrumentenzeile in JetBrains Mono uppercase.

## Components

Werkzeuge, keine Schmuckstücke: zurückhaltend im Ruhezustand, eindeutig im aktiven Zustand.

### Buttons
- **Shape:** `rounded-md` (10px); echte Pills `rounded-full`
- **Primary:** Akzent-Fläche (#EBE8DE) mit dunklem Text (#151411, `on-accent`); Hover = brightness/`accent-hover`. Kein Gradient.
- **Ghost / Sekundär:** Transparent, 1px Rahmen (`border-active`), Text secondary; Hover → bg-hover + Text primary.
- **Focus:** global 2px Akzent-Ring mit 2px Offset (`:focus-visible`).
- **Destruktiv:** Fehler-Rot #FA4942 nur für endgültige Aktionen, sonst Ghost mit rotem Text.

### Chips / Pills
- **Aktiv-Muster:** `${farbe}22` Hintergrund + `${farbe}55` Rahmen + Farbtext (loopMeta/Task-Maske); die `Pill`-Komponente nutzt `${farbe}1F` / `${farbe}26`. `rounded-sm` (6px) für Badges, `rounded-full` für echte Pills; kein text-shadow.
- **State:** Aktiv-Zustand immer über Fläche UND Rahmen, nie nur über Text. Auswahl-Chips ohne Statusbedeutung nutzen den Akzent als Farbe.

### Cards / Panels
- **Corner:** `rounded-lg` (14px); ganze Inseln/Panels `rounded-xl` (20px)
- **Background:** bg-surface (#171717) oder bg-elevated (#222222), je eine Stufe über dem Seitengrund
- **Border:** 1px `border` (rgba(168,168,168,0.10)); Hover → bg-elevated + `border-active`
- **Shadow:** keine (Flach-Regel)
- **Padding:** 16px (kompakt 12px)

### Inputs / Fields
- **Style:** bg-deep (#0A0A0A) Fläche, 1px `border`, md/xl-Radius, Text primary, Platzhalter text-muted
- **Focus:** Rahmen → `${accent}66` + weicher Ring `box-shadow: 0 0 0 3px rgba(235,232,222,0.10)` (Akzent-Alpha, kein bunter Glow). Einfache Felder setzen den Rahmen auf `border-accent`.
- **Label:** `.label-sys` (mono uppercase) oder text-muted, oberhalb des Felds, immer mit `htmlFor`/`id` oder `aria-label`. Zahlenfelder tragen Mono-Einheiten-Suffixe.

### Switch / Toggle
- Track 36×20px, `full`-Radius. An = `accent`-Fläche + `accent`-Rahmen, Thumb 14×14 in `on-accent`. Aus = bg-elevated + `border`, Thumb in text-muted. `role="switch"` + `aria-checked` + `aria-label`.

### Status-Anzeigen (Signature)
- **StatusDot:** Punkt in STATUS-Farbe + Textlabel; Farbe allein trägt nie die Information.
- **Lane-Header & Priority-Marker:** Farben ausschliesslich aus `LANE`; Priorität critical=Rot, high=Ocker, medium=neutral/Akzent, low=text-muted.

### Navigation
- **Sidebar (Desktop):** P2-bg, Einträge ~13px, Gruppen-Marken via `.label-sys`; aktiv = accent-subtle Fläche + heller Akzent-Text/Balken; inaktiv text-secondary, Hover bg-hover. Wordmark in Clash Display.
- **Mobile:** Bottom-Tab-Bar in der Daumen-Zone, safe-area-aware; voller Nav-Tree als Drawer. Top-Bar trägt nur Wordmark + Voice.

## Do's and Don'ts

### Do:
- **Do** jede Farbe aus `colors.ts` beziehen (`C`, `STATUS`, `LANE`, `STATUS_TEXT`, `P2`) — neue Bedeutung ⇒ neues Token, erst dann verwenden.
- **Do** Buntheit ausschliesslich für Status einsetzen; Struktur/Interaktion tragen über Helligkeit (Akzent) und Fläche.
- **Do** Tiefe über Flächenton lösen (eine Stufe heller = eine Ebene höher).
- **Do** Kontraste prüfen: Body/Labels ≥4.5:1; auf Akzent-Flächen immer #151411-Text; text-dim (#666666) nur für Deko.
- **Do** jeden interaktiven Zustand über Fläche+Rahmen sichtbar machen: Hover, Fokus-Ring, aktive Chips — nie über einen Glow.
- **Do** `prefers-reduced-motion` respektieren; Motion = kurzes Fade/Slide mit ease-out (`cubic-bezier(0.16,1,0.3,1)`, 100–300ms), nur transform+opacity.
- **Do** Touch-Targets ≥44px und Safe-Areas auf iPhone einhalten.

### Don't:
- **Don't** einen bunten Akzent einführen — der Akzent ist achromatisch. Cyan/Teal (#00E5FF, #0FA3A3) sind v3-Regressionen.
- **Don't** Lila/Violett in irgendeiner Form — die zentrale Anti-Referenz.
- **Don't** Neon-Glow, farbige Schatten, Akzent-`box-shadow`-Halos, `backdrop-blur` als Deko.
- **Don't** Gradient-Text (`background-clip: text`) oder Farb-Verläufe als Akzent.
- **Don't** lokale Farbpaletten oder Inline-Hex in Komponenten anlegen — auch nicht „nur temporär".
- **Don't** Statusfarben für Deko nutzen oder pro Seite umdeuten.
- **Don't** identische Karten-Grids ohne Varianz, Hero-Metrik-Schablonen, Stock-/Marketing-Metaphern — Arbeitskonsole mit Signatur, keine Landing Page.

> **Dokumentierte Ausnahmen von der Achromatik** (kein Regressions-Verstoss): externe Marken-Identitäten (`BRAND` — LinkedIn, Sprach-Badges etc.), die Terminal-ANSI-Palette (`XTERM_THEME`, inkl. cyan/magenta — Terminal-Content-Treue) und die frei wählbaren Board-Identitätsfarben (`WORKSPACE_COLORS`, mit bewussten Extras pink/orange/blau — nicht strukturell, kein Purple). Diese sind in `colors.ts` benannt und zentralisiert.
