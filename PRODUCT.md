# Product

## Register

product

## Users

Der Operator — Admin seiner selbst-gehosteten AI-Agent-Flotte. IT-Profi (Power BI / SQL / KI-Automatisierung), kein Frontend-Entwickler. Nutzt Mission Control abends und am Wochenende, meist in dunkler Umgebung, oft remote via Tailscale vom Notebook. Lange Monitoring-Sessions, unterbrochen von kurzen, schnellen Eingriffen: Task dispatchen, Agent prüfen, Approval erteilen.

Sekundär: die Agents selbst (Boss, Rex, FreeCode, …) erscheinen als Subjekte im UI — ihre Zustände, Pipelines und Terminals sind der Hauptinhalt fast jeder Seite.

## Product Purpose

Self-hosted Command Center für eine Multi-Runtime-AI-Agent-Flotte (Docker cli-bridge + Host launchd) — die „Jarvis"-Vision. Tasks erstellen und dispatchen, Agents und Runtimes überwachen, Schedules, Memory/Knowledge, Workflows und Deployments verwalten.

Erfolg heisst: der Operator erfasst den Systemzustand in Sekunden (Was läuft? Was klemmt? Wer ist blockiert?) und kann ohne Umwege eingreifen. Das UI ist ein Arbeitsinstrument für täglichen Dauereinsatz, kein Showcase.

## Brand Personality

Ernst. Ruhig. Präzise. — „Serious. Dark. No neon. No purple." (colors.ts-Doktrin)

Das Gefühl eines Operations-Rooms: konzentriert, vertrauenswürdig, unaufgeregt. Referenzen: Bloomberg Terminal (Dichte + Ernsthaftigkeit), Linear.app (Präzision + Reduktion), Stripe Dashboard (Klarheit). Vertrauen entsteht durch Zurückhaltung — das System wirkt kompetent, weil es nicht um Aufmerksamkeit buhlt.

## Anti-references

- **Generisches AI-Tool-Lila** (#8B5CF6/#7C3AED-Dashboards mit Purple-Gradient): MC hatte genau diesen Look und hat ihn mit der MC=Teal-Entscheidung (Juni 2026) bewusst abgelegt. Lila ist verboten.
- **Neon-Glow & Glassmorphism als Deko**: keine leuchtenden Schatten, kein backdrop-blur ohne Funktion.
- **Status-Feuerwerk**: keine 5 gleichlauten, gesättigten Farben pro Screen. Statusfarben sind gedämpft und sparsam.
- **SaaS-Marketing-Ästhetik**: Hero-Metriken, Gradient-Text, identische Karten-Grids — das ist eine Arbeitskonsole, keine Landing Page.

## Design Principles

1. **Eine Stimme.** Ein Akzent (Teal), Grau für Struktur. Farbe bedeutet Zustand oder Aktion — nie Dekoration.
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
