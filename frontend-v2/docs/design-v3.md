# Design v3 — „Leitstand · argyelan Edition" (Implementierungs-Guide)

> Maßgeblich für alle UI-Arbeiten im Redesign-Branch `feat/frontend-v3-redesign`.
> Token-Single-Source: `src/lib/colors.ts` (`C`, `STATUS`, `LANE`, `STATUS_TEXT`).
> CSS-Tokens: `src/styles/globals.css` (`@theme` + `:root`).

## Grundprinzipien

1. **Ein Akzent:** argyelan-Cyan `#00E5FF` (`C.accent`). Kein zweiter Akzent, kein Purple, keine Regenbogen-Gradients.
2. **Blau-getönte Off-Blacks** statt Neutralgrau: `#04070C → #070B12 → #0B111C → #101827 → #162134` (deep→hover). Tiefe durch Flächenton, nicht durch Schatten.
3. **Eckig:** Radien sm=2 / md=4 / lg=6 / xl=10px (Tailwind `rounded-*` mappt automatisch über `@theme`). Keine `rounded-full`-Pills für strukturelle Elemente (Ausnahme: Status-Punkte, Avatare).
4. **Flat:** kein backdrop-blur, keine farbigen Schatten/Glows. Schatten nur auf Overlays (`--shadow-elevated`).
5. **Mono ist die Signatur-Stimme:** JetBrains Mono für Micro-Labels, IDs, Stats, Metadaten.

## Typografie

- **Display:** Clash Display (`var(--font-display)` oder Klasse `.display`) — Seitentitel, Hero-Zahlen. Weight 500–600, letter-spacing -0.02em.
- **Body/UI:** General Sans (`--font-sans`, Tailwind `font-sans` Default).
- **Mono:** `var(--font-mono)` / Tailwind `font-mono`.
- **Micro-Label:** Utility-Klasse `.label-sys` (mono 10px uppercase, ls .14em, muted). Varianten: `.label-sys--accent`, `.label-sys--dim`. Für Sektions-Header, Karten-Labels, Meta-Zeilen — sparsam, aber konsistent.

## Signatur-Elemente

- `.corner-ticks` — Cyan-Eckmarken (TL+BR) für hero-artige/aktive Panels. Max 1–2 pro View.
- **Ruhige Bühne:** der Hintergrund ist bewusst leer (dezenter Cyan-Schleier + Grain). KEINE Raster/Muster im Hintergrund (Operator-Entscheid v3.1) — der Fokus gehört dem Inhalt.
- **Messmarke:** 1px-Linie (`C.border`) mit 2px-Cyan-Segment links — Header-Trenner auf Seiten (siehe `app/page.tsx`).
- Status-Dots eckig (Quadrat) statt rund, wo sie als „Instrumenten-Lämpchen" wirken.

## Regeln

- Farben NUR aus Tokens (`C.*`, `var(--color-*)`). Kein Inline-Hex (Regression).
- **UI-Sprache: Englisch** (Operator-Entscheid v3.1). Code-Kommentare dürfen Deutsch bleiben; nutzersichtbare Strings (Labels, Buttons, Placeholder, aria-/title-Attribute, Toasts) sind Englisch.
- Text-Kontrast ≥4.5:1. Auf Cyan-Flächen: Text `C.onAccent` (#00252B), niemals weiß.
- Statusfarben bleiben funktional: online/warning/error/info unverändert; für Fliesstext `STATUS_TEXT`.
- Hover: Fläche eine Stufe aufhellen oder border-accent; Fokus-Ring global (`:focus-visible`).
- Motion: 100–300ms, ease `[0.16,1,0.3,1]` oder Spring; nur transform+opacity; `prefers-reduced-motion` respektieren.
- Mobile (iPhone): Touch-Targets ≥44px, safe-area via `.pt-safe/.pb-safe` etc., horizontale Scroller via `.tab-strip`.
- KEINE funktionalen Änderungen: Datenlogik, Queries, Handler, Routing bleiben unberührt. Nur Optik/Struktur im Markup.

## Verboten (AI-Slop)

- Purple/Violett jeglicher Form · Gradient-Text · Neon-Glow · Glassmorphism als Deko · identische 3er-Card-Grids ohne Bento-Varianz · Sun/Moon-Toggle · generische Stock-Metaphern.
