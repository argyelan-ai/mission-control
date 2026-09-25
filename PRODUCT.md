# Product

## Register

product

## Users

Der Operator — betreibt Mission Control selbst-gehostet für die eigene Arbeit. IT-Profi, kein Frontend-Entwickler. Nutzt MC abends und am Wochenende, oft remote vom Notebook oder Handy. Er führt: Richtung, Freigaben, Prioritäten — er will kein Feuer löschen.

Sekundär: Agenten und kurzlebige Arbeits-Sessions („Köpfe") erscheinen als Subjekte im UI — ihr Zustand, ihre Ergebnisse und Laufakten. Die dauerhafte Agenten-Flotte ruht seit ADR-085 und bleibt für bestehende Installationen verfügbar.

## Product Purpose

Selbst-gehostete Basis für ein autonomes Team, das der Operator führt statt babysittet. Seit [ADR-085](docs/decisions/085-head-per-job.md) heisst „Team": ein geschriebenes Verfahren plus ein kurzlebiger Kopf pro Auftrag (eine Coding-CLI-Session im eigenen Worktree, die plant, prüft, einen PR öffnet, eine Laufakte schreibt und endet) — keine Dauer-Flotte mit festen Rollen.

MC wird dafür bewusst klein. Es behält, was nur der Operator hat: Regeln und Verfahren (als Dateien), den Vault (Markdown/Git), die Steuerung der eigenen GPU-Boxen, Kennzahlen (Verbrauch, Digest, Laufakten) und den Kern (API, DB, Auth, Scheduler). Aufträge starten, Fragen beantworten und Übersicht kommen von der Coding-CLI; konfigurieren geht vor bauen.

Erfolg heisst: Aufträge laufen ohne den Operator am Tisch durch, und wenn er hinschaut, sieht er in Sekunden, was auf ihn wartet, was herauskam und was es gekostet hat. Das UI ist ein Arbeitsinstrument, kein Showcase.

Regeln: [docs/PRINCIPLES.md](docs/PRINCIPLES.md) · Stand und nächste Schritte: [docs/ROADMAP.md](docs/ROADMAP.md).

## Brand Personality

Ernst. Ruhig. Präzise. — „Serious. Dark. No neon. No purple." (colors.ts-Doktrin)

Das Gefühl eines Operations-Rooms: konzentriert, vertrauenswürdig, unaufgeregt. Referenzen: Bloomberg Terminal (Dichte + Ernsthaftigkeit), Linear.app (Präzision + Reduktion), Stripe Dashboard (Klarheit). Vertrauen entsteht durch Zurückhaltung — das System wirkt kompetent, weil es nicht um Aufmerksamkeit buhlt.

## Anti-references

- **Generisches AI-Tool-Lila** (#8B5CF6/#7C3AED-Dashboards mit Purple-Gradient): MC hatte genau diesen Look und hat ihn mit der MC=Teal-Entscheidung (Juni 2026) bewusst abgelegt. Lila ist verboten.
- **Neon-Glow & Glassmorphism als Deko**: keine leuchtenden Schatten, kein backdrop-blur ohne Funktion.
- **Status-Feuerwerk**: keine 5 gleichlauten, gesättigten Farben pro Screen. Statusfarben sind gedämpft und sparsam.
- **SaaS-Marketing-Ästhetik**: Hero-Metriken, Gradient-Text, identische Karten-Grids — das ist eine Arbeitskonsole, keine Landing Page.

## Design Principles

1. **Eine Stimme.** Ein Akzent (Off-Cream #EBE8DE, achromatisch — Stand v4 „Signal", siehe DESIGN.md), Grau für Struktur. Farbe bedeutet Zustand oder Aktion — nie Dekoration.
2. **Status ist Information.** STATUS/LANE-Vokabular ist app-weit identisch: dieselbe Farbe heisst auf jeder Seite dasselbe.
3. **Ruhe vor Reiz.** Kein Glow, kein Blur, kein Effekt ohne Funktion. Die Aufmerksamkeit gehört den Daten und den Agents.
4. **Single Source.** `colors.ts` ist das einzige Farb-Vokabular. Lokale Paletten und Inline-Hex sind Regressions, keine Gestaltungsfreiheit.
5. **Bedienbar bleibt benutzbar.** Tastatur, Screenreader, Zoom — WCAG AA ist Untergrenze, nicht Ziel.

## Accessibility & Inclusion

- WCAG 2.2 AA als verbindlicher Standard, verifiziert per accesslint-Live-Scans (Stand Juni 2026: 0 Violations auf Home + Neuer-Auftrag-Modal inkl. aller aufgeklappten Zustände).
- Body-/Label-Text ≥4.5:1 auf allen Hintergründen (#050505–#161616); `textDim` nur für Deko/inaktive Icons.
- Interaktive Elemente mit echten Labels (aria-label / htmlFor+id), Fokus sichtbar, keine Keyboard-Traps.
- Pinch-Zoom nie blockieren (kein `maximumScale: 1`), `prefers-reduced-motion` respektieren.
- UI-Tests fahren alle Zustände aktiv durch (Modi, Akkordeons, Toggles) — ein Scan des Default-Zustands ist kein UI-Test.
