# HANDOFF — Frontend v3 Redesign („Leitstand · argyelan Edition")

> **WICHTIG (2026-07-18):** Operator-Feedback zu v3: „inkonsistent". Falls
> Komplett-Neuaufbau entschieden wird, gilt der Plan in
> **`frontend-v2/docs/v4-rebuild-plan.md`** (inkl. erweiterbarer
> Feature-Registry für kommende UI-Seiten). Dieses Dokument hier beschreibt
> den v3-Zwischenstand.

> **Für die nächste Session (nach Compaction).** Stand: 2026-07-18.
> Branch: `feat/frontend-v3-redesign` im Repo `~/Workspace/Projects/mission-control`.
> **Nichts ist committet** — der ganze Stand liegt im Working Tree des Branch.

---

## 1. Was abgeschlossen ist

**Redesign v3 komplett umgesetzt + vom Operator abgenommen (Feedback-Runden 1+2):**

Runde 1: Grid-Hintergrund entfernt (ruhige Bühne), Flächen vereinheitlicht (547 Fixes/93 Dateien), UI-Sprache 100% Englisch.
Runde 2 (Mobile): Home-Header auf Mobile kompakt (einzeiliger Greeting, Datum in Meta-Zeile, Messmarke-Segment nur sm+), „New agent"-Button eckig/solid/dunkler Text (war Gradient-Pill + deutsch), Harness-Chip auf xs ausgeblendet (volle Agent-Namen), **Tasks-Overflow-Fix: klassisches Flex-`min-width:auto`-Problem — `min-w-0` auf Listen-Pane (`app/tasks/page.tsx:1116`) + Spalten-Root + Sticky-Row (`TaskListColumn`).** Regel fürs Merken: horizontale Clips auf Mobile fast immer fehlendes `min-w-0` in einer Flex-Kette.

Runde 3 (Emoji→SVG): Alle Emoji-Identitäten (Agenten, Boards, Skills, Schedule-Templates, Snooze/Key/Pin-Deko) durch Lucide-SVGs ersetzt. Zentrale Komponente: `src/components/shared/EntityIcon.tsx` — Map Emoji→Icon (inkl. U+FE0F/Skin-Tone-Normalisierung) + Key→Icon (`ENTITY_ICON_KEYS` für neue, emoji-freie Speicherung; Board-Picker speichert jetzt Keys statt Emojis, DB-Altbestand mit Emojis wird weiterhin aufgelöst). Fallback: `Bot`. ~30 Stellen in ~25 Dateien migriert. Wizard-Emoji-Eingabefeld bleibt (Backend-Feld), bekannte Emojis rendern als SVG.

Runde 4 (Harness-Marken): CLI-Namen („CLAUDE CODE", „GROK", „HERMES") in der Agents-Liste durch SVG-Marken ersetzt. Neue Komponente `src/components/shared/HarnessIcon.tsx`: eigens gezeichnete geometrische Marken für Claude (8-strahlige Sternmarke) und Grok (gebrochenes X) — keine kopierten Logos — plus Lucide für omp (Terminal), openclaude (Boxes), hermes (AudioWaveform). Chip = 24px eckige Marke mit Tooltip/aria-Label (Text über `harnessLabel()`). Wizard-Harness-Auswahl zeigt Icon + Label.
- **Tokens:** Akzent argyelan-Cyan `#00E5FF` (altes Teal #0FA3A3 vollständig ersetzt, 0 Reste), blau-getönte Off-Blacks (`#04070C/#070B12/#0B111C/#101827/#162134`), eckige Radien (2/4/6/10px via `@theme` in `src/styles/globals.css`), Single Source `frontend-v2/src/lib/colors.ts` (API unverändert: `C`, `STATUS`, `LANE`, `STATUS_TEXT`, `XTERM_THEME`, `BRAND`, `WORKSPACE_COLORS`).
- **Fonts (self-hosted in `frontend-v2/public/fonts/`):** Clash Display (Display/Wordmark, `--font-display`/`.display`), General Sans (UI, `--font-sans`), JetBrains Mono (Signatur-Mono, `--font-mono`). Geist + Space Grotesk entfernt.
- **Signatur:** `.label-sys` (Mono-Micro-Labels), `.corner-ticks` (Cyan-Eckmarken), Messmarke (1px-Linie + 64px-Cyan-Segment) unter Seitentiteln, Mono-Datastream-StatusBar, 2px-Cyan-Kante auf Modals/Sheets.
- **Shell:** Sidebar (Gruppen OVERVIEW/OPERATIONS/ANALYSIS/SYSTEM, eckiger Aktivbalken), WorkspaceSwitcher, StatusBar (SYS OK · AGT x/y · BRD · Uhrzeit), MobileNav (**Bottom-Tab-Bar** Home/Tasks/Agents/Sessions/Menu + rechter Drawer, safe-area), AppShell (`.main-content-pb` für Tab-Bar), AmbientBackground (**ruhige Bühne**: Cyan-Schleier + Grain, KEIN Grid — Operator-Entscheid v3.1).
- **Seiten:** Login (asymmetrische Brand-Bühne), Home, Tasks + TaskDetailPanel-Chrome, Insights (KPIs/Charts), Office Org-Chart, Command Palette (rechtes Panel), Modal-/SlideOver-Chrome, Header aller 17 Routen vereinheitlicht, News/Content wieder in Nav (`VERTICALS.newsStudio: true` in `src/lib/verticals.ts` — Private-Deployment-Flag, Public-Release setzt es auf false).
- **Flächen-Sweep:** 547 Fixes/93 Dateien — alle `rgba(255,255,255,0.0x)`-Panels auf Token-Flächen (Fix für „Panels haben verschiedene Farben").
- **Sprache:** UI 100% Englisch (Operator-Entscheid — „MC Sprache ist englisch"). Auch Alt-DE-Strings migriert (Wizard, Runtimes, Vault, AgentActions…). **Ausnahme:** Verticals `news-studio` + `bench_studio` bleiben Deutsch (deren Tests assertieren DE — nicht anfassen).
- **Docs:** Root-`DESIGN.md` auf v3 umgeschrieben, `frontend-v2/docs/design-v3.md` (Implementierungs-Guide, enthält Sprach-Regel), `frontend-v2/docs/redesign-screenshot-checklist.md` (Inventar aller Overlays).

**Verifizierung:** 356/356 vitest grün (`--maxWorkers=4` nötig, sonst Last-Flakes!), `tsc --noEmit` sauber (Ausnahmen s.u.), Production-Build fehlerfrei (27 Routen), Screenshots Desktop+Mobile.

## 2. Screenshots & Zugang

- Vorher/Nachher-Serien: `~/Workspace/.mc-verify-shots/` (`vorher/`, `nachher/`, `final-v31/`, `mobile/`).
- Login für Previews: read-only Account `kimi@local` (Passwort NICHT in Dateien — beim Operator erfragen oder aus Session-Kontext). Login via Playwright: `input#email` + `input[type=password]` + `button[type=submit]` auf `/login`, Token landet in localStorage.
- Screenshot-Skript-Pattern: siehe gelöschte temp-Skripte — Basis: Playwright, `127.0.0.1` statt `localhost` (s. Fallstricke), `waitUntil: 'domcontentloaded'` + 3.5s Wartezeit (networkidle hängt wegen SSE).

## 3. Lauf betreiben

```bash
cd frontend-v2
npm run dev -- -p 3100        # Dev (Turbopack)
# oder:
npm run build && npm run start -- -p 3100   # Prod-Check
```

- API-Proxy: Dev-Server proxied `/api/*` → `localhost:8000` (Docker-Backend).
- **WICHTIG:** Dev und Build im selben `.next` → nie parallel. Vor `build`: `pkill -f "next dev"; rm -rf .next`.
- Tests: `npx vitest run --maxWorkers=4`.

## 4. Fallstricke (bitter gelernt)

- **`localhost` vs `127.0.0.1`:** fremder `next-server` v16 lauscht IPv6 `*:3000` und verschluckt localhost-Requests → immer 127.0.0.1 verwenden. (Operator will den Prozess evtl. killen — PID war 17048, `lsof -nP -iTCP:3000 -sTCP:LISTEN`.)
- **tsc-Altlasten** (nicht unsere): `.next/types`-Fehler `news/zzztest` (stale, nach `rm -rf .next` weg) und `RuntimeCard`-Export in `app/runtimes/page.tsx` (Next verbietet Zusatz-Exporte; `ignoreBuildErrors:true` deckelt es; Test importiert die Komponente von dort).
- **vitest-Flakes:** volle Suite ohne Worker-Limit produziert zufällige Timeouts — mit `--maxWorkers=4` stabil 356/356.
- **Sessions-Seite ist TABU für Strukturänderungen** (Operator: „muss genau gleich funktionieren") — nur Token-Farben ziehen mit; Texte wurden auf Englisch gestellt (war Teil der Sprach-Vereinheitlichung).

## 5. OFFENER PLAN — Verbesserungs-Backlog (Priorität)

**Sofort (vom Operator gewünscht, „sinnvolle Erkenntnisse"):**
1. **Dead 3D-Deps entfernen:** `three`, `@react-three/fiber`, `-drei`, `-postprocessing`, `@types/three` aus `frontend-v2/package.json` (0 Imports in src, Rest vom abgebauten 3D-Office). Danach `npm install` + Build + Tests.
2. **`src/app/page.tsx.bak.1778097707` löschen** (vergessenes Backup im App-Router).
3. **`RuntimeCard` aus `src/app/runtimes/page.tsx` nach `src/components/` verschieben** (Export-Regel); Import im Test `src/app/runtimes/__tests__/autostart-toggle.test.tsx` anpassen; danach `.next` neu bauen → tsc-Fehler weg.
4. **Font-Preload:** in `src/app/layout.tsx` `<head>`: `<link rel="preload" href="/fonts/GeneralSans-400.woff2" as="font" type="font/woff2" crossOrigin="anonymous">` + selbiges für `ClashDisplay-600.woff2`.

**Danach (Mittel):**
5. Fremden next-server v16 auf :3000 killen (mit Operator absegnen).
6. Visual-Regression als npm-Script (`scripts/`) auf Basis `docs/redesign-screenshot-checklist.md`.
7. Bundle-Diät Homepage (728 kB First Load): `react-syntax-highlighter` → schlanke Alternative oder lazy; Voice-Widget/LiveKit lazy laden (lädt initial auf jeder Seite).

**Größer:**
8. A11y-Feinschliff: Focus-Trap für Custom-Modals/Drawer, `aria-live` für Toasts.
9. Reconnect-State (SSE/Queries) sichtbar („reconnecting…") — Mobile-relevant.
10. Commit-Hygiene main: `docker/cli-versions.json` + Untracked-Dateien.
    (`docker-compose.agents.yml` stand hier auch mal — sie gehoert seit dem
    OSS-Split ausdruecklich NICHT mehr committet: sie beschreibt die eigene
    Flotte und ist gitignored. Siehe `docs/setup/updating.md`.)

## 6. Nächste Schritte danach

- **Commit + Merge:** Operator entscheidet. Branch ist bereit; nicht ohne explizites Go committen/pushen.
- **Live-Gang:** Docker-Image `ghcr.io/argyelan-ai/mc-frontend` neu bauen (Build-Arg `NEXT_PUBLIC_BRAND=argyelan.ai`), dann Container neu → Live auf `:3000`/Caddy.
- Danach: Nachher-Screenshots gegen Live + Checkliste abhaken.

## 7. Kontext-Notizen

- Operator-Account für Live-UI: Mark (`Operator-Mail (beim Operator erfragen)`); für Previews reicht `kimi@local` (viewer, read-only — nichts anklicken was mutiert).
- Secrets niemals in Dateien/Commits/Logs (Operator-Regel).
- Subagent-Hinweis: Hintergrund-Agenten können an Provider-Quota-Limits sterben (403) — dann Arbeit selbst im Main-Loop weiterführen (beim Flächen-Sweep so gehandhabt).
