# v4 — Plan Komplett-Neuaufbau Frontend („clean sheet")

> **Status: ENTWURF / wartet auf Operator-Go.** Erstellt 2026-07-18 nach dem
> Operator-Feedback „v3 sieht inkonsistent aus". Dieser Plan beschreibt den
> vollständigen Neuaufbau des MC-Frontends mit echtem Design-System —
> statt v3s Token-Reskin auf gewachsenen Strukturen.

---

## 1. Warum v3 inkonsistent wirkt (ehrliche Analyse)

| Problem | Ursache |
|---|---|
| Jede Seite „atmet" anders | v3 hat Tokens/Farben ersetzt, aber die heterogenen Seiten-Layouts behalten (Roster-Tabellen, Karten, Panels, Custom-Grids) |
| Mikro-Muster ungleichmäßig | Corner-Ticks, Messmarke, label-sys, Cyan-Kanten wurden seitenweise unterschiedlich tief eingesetzt |
| Kein echtes Komponenten-System | Jede Seite baut Buttons/Cards/Inputs per Hand mit Inline-Styles — ~15 Varianten derselben Primitive |
| Dichte schwankt | 10px-Mono hier, 16px-Body dort, Padding-Rhythmus nicht durchgehend |
| Icon-Systeme gemischt | EntityIcon, HarnessIcon, Lucide direkt, Emoji-Reste, Custom-SVGs ohne gemeinsame Größen/Rasten-Regel |

**Lehre:** Reskin reicht nicht. v4 = Struktur neu, Inhalte/Logik bleiben.

---

## 2. Was aus v3 übernommen wird (bewährt)

- **Design-Tokens:** Cyan `#00E5FF`, blau-getönte Off-Blacks, Radien eckig, Status-Vokabular (`lib/colors.ts` API)
- **Fonts:** Clash Display + General Sans + JetBrains Mono (self-hosted)
- **Shell-Konzept:** Sidebar mit Gruppen, Mobile Bottom-Tab-Bar, StatusBar-Datastream (Layout neu gebaut, Konzept bleibt)
- **EntityIcon / HarnessIcon** (als Teil des neuen Icon-Moduls)
- **Daten-Schicht komplett:** `lib/api`, `lib/store`, `lib/sse`, React-Query-Nutzung, xterm-Theme, Tests als Verhaltens-Referenz

## 3. Was wegfliegt

- Sämtliche Inline-Style-Blöcke in Seiten (ersetzt durch cva-Varianten in Primitives)
- `GlassCard` (Name+Konzept), `SpotlightCard`-Hover-Spielereien, Corner-Ticks/Messmarke (werden durch EIN konsistentes Card-System ersetzt)
- Seitenspezifische Ad-hoc-Layouts ohne Archetyp
- Dead Deps (three/@react-three — 0 Imports), `page.tsx.bak`-Datei

---

## 4. Architektur des Neuaufbaus

### 4.1 Schichten

```
tokens/        → unverändert aus v3 (colors.ts, globals.css @theme)
primitives/    → Button, IconButton, Input, Select, Textarea, Card,
                 Chip, Badge, Tabs, Dialog, Sheet, Toast, Tooltip,
                 Menu, Switch, Checkbox, Skeleton, EmptyState, PageHeader
archetypes/    → ListPage, DetailPage, DashboardPage, ConsolePage, FormSheet
features/      → tasks/, agents/, sessions/, … (Daten-Logik + arcytype-Befüllung)
shell/         → AppShell, Sidebar, TabBar, StatusBar, CommandPalette
```

### 4.2 Primitives (zuerst bauen, ~20 Stück)

Jede Primitive: cva-Varianten (size/tone/state), keine Inline-Styles in Konsumenten, Radix wo vorhanden. **PageHeader** ist Pflicht für jede Route: `eyebrow (label-sys) / title (display) / meta / actions` — ein Muster, überall.

### 4.3 Archetypes (Seiten-Schablonen)

| Archetyp | Struktur | Verwendet für |
|---|---|---|
| **ListPage** | PageHeader + Toolbar (Suche/Filter/Tabs) + Listen-Body (Rows ODER Cards, nie gemischt) + Pagination/Scroll | Tasks, Agents, Inbox, Files, Repos, Loops, Schedule, News, Content |
| **DetailPage** | PageHeader (Back, Titel, Meta, Actions) + Spalten-Layout (Main + Aside) | Agent-Detail, Job-Detail, Task-Detail (als Page, nicht Sheet), Repo-Detail |
| **DashboardPage** | PageHeader + KPI-Reihe + Widget-Grid (Bento, definierte Slot-Typen) | Home, Insights |
| **ConsolePage** | Vollfläche, Split-View (Master/Terminal), kein Page-Padding | Sessions, Office |
| **FormSheet** | ResponsiveModal/SlideOver mit FormGrid-Primitive | alle Create/Edit-Dialoge |

**Regel:** Eine Seite = EIN Archetyp. Keine Sonderlocken ohne ADR-artigen Eintrag in der Registry.

### 4.4 Icon-Modul

Ein `Icon`-Wrapper mit Größenraster (12/14/16/20/24) und Tönungstoken; EntityIcon + HarnessIcon als Sub-Module. Regel: gleiche Größen für gleiche Semantik (Avatar-Icon immer 16, Row-Meta immer 12, …).

### 4.5 Motion-Standard

Framer-Presets als Modul: `enterPage` (fade-up staggered), `openSheet`, `openDialog`, `layoutSpring`. Keine seitenweisen Custom-Transitions.

---

## 5. Feature-Registry (ERWEITERBAR — hier neue Features eintragen)

> Format pro Zeile: **Feature/Route · Archetyp · Datenquelle · benötigte Primitives · Status · Notizen.**
> Bei neuem Feature: Zeile ergänzen → Archetyp wählen → Primitives checken → loslegen.
> Neue Primitive NUR bei echtem Bedarf (Registry-Eintrag begründen).

### 5.1 Bestand (Rebuild-Kandidaten)

| # | Feature / Route | Archetyp | Daten | Primitives-Bedarf | Status |
|---|---|---|---|---|---|
| 1 | Login `/login` | Dashboard (eigen) | auth | Input, Button, Card | v3 ✓ (übernehmen) |
| 2 | Home `/` | Dashboard | system, activity, pipeline | KPI, Card, Sparkline, Feed | rebuild |
| 3 | Tasks `/tasks` | List + Detail | tasks | Toolbar, Row, Chip, Tabs, Sheet | rebuild |
| 4 | Agents `/agents` | List | agents | Row, Chip, Menu, Sheet | rebuild |
| 5 | Agent-Detail `/agents/[id]` | Detail | agent, logs | Tabs, Card, FormGrid | rebuild |
| 6 | Sessions `/sessions` | Console | tmux/ws | Split, Tabs, StatusChip | rebuild (Funktionalität 1:1!) |
| 7 | Office `/office` | Console | org | Canvas-Wrapper, Zoom | rebuild |
| 8 | Inbox `/inbox` | List | approvals | Row, Card, ActionBar | rebuild |
| 9 | Insights `/insights` | Dashboard | metrics | KPI, Chart (recharts-Wrapper), Tabs | rebuild |
| 10 | Memory `/memory` | List + Graph | vault | Row, Panel, GraphCanvas | rebuild |
| 11 | Files `/files` | List | files | Tree, Preview, ActionBar | rebuild |
| 12 | Repos `/repos` | List | repos | Row, Sheet, Badge | rebuild |
| 13 | Skills `/skills` | List (Tabs) | skills | Matrix, Editor | rebuild |
| 14 | Runtimes `/runtimes` | List | runtimes | Row, Editor, StatusChip | rebuild |
| 15 | Loops `/loops` | List | loops | Row, Dialog | rebuild |
| 16 | Schedule `/schedule` | List | jobs | Row, KPI, Heatmap, Dialog | rebuild |
| 17 | Settings `/settings` | Detail (Sektionen) | user/system | FormGrid, Nav | rebuild |
| 18 | News `/news` (privat) | List | news | Row, Toolbar, Chip | später (Vertical) |
| 19 | Content `/content` (privat) | List | content | Row, Card | später (Vertical) |
| 20 | Bench `/bench` (privat) | List | bench | Row, Dialog | später (Vertical) |
| 21 | Agent-Wizard `/agents/wizard` | FormSheet (Steps) | wizard | Stepper, FormGrid, Review | rebuild |
| 22 | Setup `/setup` | FormSheet (Steps) | setup | Stepper | rebuild |

### 5.2 Geplant/neu (Platzhalter — Operator ergänzt)

| # | Feature / Route | Archetyp | Daten | Primitives-Bedarf | Status |
|---|---|---|---|---|---|
| N1 | _(neu)_ Agent Meetings | List + Console | agent_meetings (DB existiert) | Row, Chat-Stream | ☐ idea |
| N2 | _(neu)_ Notifications-Center | List | notifications (DB existiert) | Row, Badge, Popover | ☐ idea |
| N3 | _(neu)_ Deploy-History | List (Timeline) | deploy_history (DB existiert) | Timeline, StatusChip | ☐ idea |
| N4 | _(neu)_ … | … | … | … | ☐ |

---

## 6. Reihenfolge & Prozess

1. **Fundament (1–2 Tage):** Primitives + PageHeader + Icon-Modul + Motion-Presets. Storybook-ähnliche `/dev/primitives`-Seite (intern, nicht prod) zum Abnehmen.
2. **Shell (0.5 T):** AppShell/Sidebar/TabBar/StatusBar auf Primitives umgebaut.
3. **Pilot (0.5 T):** Home + Tasks als Referenz für ListPage/DashboardPage — **Operator-Review-Gate hier, bevor Rest gebaut wird.**
4. **Rollout:** Agents(+Detail), Sessions (Console, Funktionalität 1:1), Insights, Inbox, dann Rest in Registry-Reihenfolge.
5. **Verticals zuletzt:** News/Content/Bench erst wenn Kern steht.
6. **Cleanup:** three-Deps raus, `.bak` weg, RuntimeCard-Export fixen, LegacyMemoryPage prüfen (ersetzt?).

**Qualitäts-Gates pro Seite:** Screenshot Desktop+Mobile in Registry verlinkt, Tests grün, kein Inline-Style außer dynamischen Werten (Breiten, Positionen).

## 7. Aufwand (grob)

Fundament 1.5–2 T · Shell 0.5 · Pilot 0.5 · Kernseiten (6×) 2–2.5 · Rest 1.5–2 · Verticals 1 — **≈ 6–8 Tage** bei voller Dringlichkeit, parallelisierbar auf Kern- vs. Rest-Seiten.

## 8. Was mit Branch `feat/frontend-v3-redesign` passiert

Option A (empfohlen): als Zwischenstand committen + taggen (`v3-reskin`), aber NICHT mergen; v4 startet auf neuem Branch `feat/frontend-v4` von main + Cherry-Picks aus v3 (Tokens, Fonts, Icons, vertikale Flags).
Option B: v3 mergen (es ist besser als main-Stand), v4 baut darauf auf. Operator entscheidet.
