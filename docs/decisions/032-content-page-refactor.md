# ADR-032 — Content Page Refactor: Von 4 Tabs zu 2 Top-Level Pages

**Status:** Accepted
**Datum:** 2026-05-10
**Scope:** Frontend/Pages · UX/Navigation

## Kontext

Die `/content` Seite hatte 4 historisch gewachsene Top-Level Tabs:

| Tab | Zweck | Status |
|---|---|---|
| Pipeline | Generisches Board-Kanban (idea → script → publish) | Inaktiv |
| News Hub | News-Crawler + RSS + Public Site news.argyelan.ai | Aktiv |
| Shorts | Argyelan Multi-Format Content Director Console | Aktiv (neu) |
| LinkedIn Video | Legacy Video-Generation Pipeline | Inaktiv |

Der Operator nutzte Pipeline und LinkedIn Video nicht mehr. Shorts war das neue Master-Interface für Argyelan Content. News Hub war ein eigenständiges Monitoring-System, das zwar Trend-Signale für Shorts lieferte, aber als Sub-Tab unter "Content" semantisch falsch platziert war.

## Entscheidung

1. **News Hub wird eigene Top-Level Page** `/news` mit eigener Sidebar-Navigation (`Newspaper` Icon).
2. **`/content` wird auf ShortsHub reduziert** — keine Tabs mehr, nur noch Argyelan Content.
3. **Pipeline- und LinkedIn-Video-Komponenten werden Frontend-seitig entfernt** (Backend-Router bleiben vorerst unangetastet).

## Alternativen

- **Variante A (2 Tabs: Argyelan + News):** News unter `/content` gelassen. → Verworfen weil News ist kein Content-Edit-Tool, es ist ein Monitoring-System mit eigener Public Site. Semantisch falsch vermischt.
- **Variante C (News als Mini-Sektion in Shorts):** Trend Radar als einklappbare Komponente. → Verworfen weil NewsHub ist 1.208 Zeilen — eine Mini-Version wäre signifikante Neuentwicklung ohne klaren ROI.

## Konsequenzen

### Positiv
- Klare semantische Trennung: Content = Produktion, News = Monitoring
- `/content` von 559 Zeilen auf 26 reduziert
- Kein Context-Switch für den Operator zwischen Content-Planung und News-Crawler
- LinkedIn Video (746 Zeilen) komplett gelöscht — tot Code raus

### Negativ
- Sidebar wird um 1 Item länger (14 → 15 Items)
- News-Trend-Signale für Shorts erfordern jetzt Navigation `/news` → `/content`

## Referenzen

- Betroffene Dateien:
  - `frontend-v2/src/app/content/page.tsx` (26 Zeilen, down from 559)
  - `frontend-v2/src/app/news/page.tsx` (neu, 25 Zeilen)
  - `frontend-v2/src/components/layout/Sidebar.tsx` (+1 NAV_ITEM)
  - `frontend-v2/src/components/linkedin-video/LinkedInVideoPanel.tsx` (gelöscht, 746 Zeilen)
- Commits: `487b3813` — feat(content): add /news route + sidebar nav (merged to main)
- Verwandte ADRs: —
