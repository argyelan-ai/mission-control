# ADR-087 — Theme switch: dark stays the default, light is an option

**Status:** Accepted
**Datum:** 2026-09-24
**Scope:** Frontend/Design-System · Frontend/State · Docs

## Context

Mission Control was dark only. "Serious. Dark. Achromatic. Colour means
status — nothing else." is the doctrine at the top of `lib/colors.ts`, and
PRODUCT.md and DESIGN.md describe the console as a dark instrument room.

PR #537 (September 2026) proposed a light theme. It was never merged: it
reused ADR number 084 (taken since by the omp ACP decision), fell 63 commits
behind `main` with seven conflicts, and several of its light colours missed
WCAG AA. On 2026-09-24 the operator decided: **light mode yes, but as a
switch. Dark stays the default and the character; the condition is that the
switch works cleanly everywhere.** This ADR records that decision and the
technique, rebuilt fresh on `main`. #537 is replaced by this work.

What made a switch hard on `main`:

- All colour tokens in `lib/colors.ts` (`C`, `P2`, `STATUS`, `LANE`,
  `STATUS_TEXT`) were literal hex/rgba strings: about 5,100 uses in 230 files.
- About 580 places built a translucent colour by appending a hex alpha suffix
  (`${C.error}33`, `${cfg.color}1A`, `C.warning + "66"`). With variables that
  becomes `var(--color-status-error)33`: **invalid CSS without any type error
  or merge conflict** — the browser silently drops the declaration.
- Canvas code (memory graph) and `hexToRgb()` parsed colours as hex.
- The board picker saves a colour from `WORKSPACE_COLORS` to the database.
- About 280 raw `rgba()`/hex values and fixed Tailwind palette classes
  (`bg-black/60`, `bg-white/5`, `text-zinc-500`) in components.
- Tailwind v4 drops `@theme` variables it does not see used in CSS.

## Decision

**Dark is the default and the character; light is a per-browser option.**
Nobody sees anything new unless they pick light (or "follow the system") in
the switch. "Serious. Dark." stays the standard wording in PRODUCT.md and
DESIGN.md.

1. **Tokens are CSS variables.** Every UI colour value lives once in
   `styles/globals.css`: the plain `:root` block holds the dark values (the
   old values, unchanged), `:root[data-theme="light"]` overrides only the
   base tokens. Aliases (P2 set, status short names, chart series,
   subtle/border mixes) point at base tokens with `var()` and follow by
   themselves. Colour tokens live in `:root`, **not** in Tailwind's `@theme`.
   `lib/colors.ts` holds only the `var(--color-…)` names.
2. **Colour helpers.** `alpha(color, a)` (`color-mix(in srgb, X N%,
   transparent)`) is the only way to make a colour translucent.
   `resolveColor()` turns `var()` into a concrete value for consumers that
   cannot read CSS variables (the canvas graph).
3. **Values that leave the page stay literal.** `WORKSPACE_COLORS` (saved to
   the database as `board.color`), `BRAND` (external identities),
   `XTERM_THEME` (xterm cannot read variables) and the PWA manifest keep
   literal hex.
4. **Runtime.** `lib/theme.ts`: choice `dark | light | system`, default
   `dark`, stored per browser under `mc_theme` (every storage access in
   try/catch — private mode falls back to dark). `THEME_INIT_SCRIPT` is the
   first inline script in `<head>` and sets `data-theme` before the first
   paint (no flash). `startThemeSync()` follows an OS switch while the choice
   is "system" and a choice made in another tab; `applyTheme()` also updates
   `<meta name="theme-color">`. `useTheme()` re-renders every consumer.
5. **Switches.** Settings → Appearance (account group, for every user) and
   the user menu: a cycle button in the desktop sidebar footer (expanded and
   collapsed), the three-way radio group in the mobile menu.
6. **Colour rules for both modes.**
   - The terminal and syntax-highlighted code stay dark in both modes: they
     are content, not chrome, and their palettes are built for a dark ground.
   - Measurement is ink, state is colour (unchanged).
   - Never append a hex suffix to a colour; use `alpha()`. `alpha()` works
     in whole percent (`08` → 3 %, not 3.1 %) and mixes with `color-mix`,
     so it is not bit-identical to the old `#RRGGBBAA` — not visible in the
     before/after screenshots.
   - `data-theme` on `<html>` is the ONLY theme switch. There is no `dark`
     class on `<html>` and the Tailwind `dark:` variant is not used; a
     light-only override uses the `light:` variant (`globals.css`).
   - `status-offline` is deliberately faint in both modes (dark `#3a3a3a`,
     light `#c9c5bb`, about 1.4–1.7:1 against the surfaces): an offline dot
     is "nothing happening", and the text next to it carries the state.
     Lifting it to 3:1 (WCAG 1.4.11, e.g. light `#9d988c`) is an open
     operator decision, for both modes together.
   - Text tokens clear WCAG AA (4.5:1) on all five surfaces in light mode.
     `text-dim` is meant for decoration but is used for small meta text in
     many places (axe finding), so in light mode it is an AA text tone on
     the four resting surfaces too.
   - Status hues are used as text on their own 12 % tint (chips, pills), so
     in light mode they clear 4.5:1 there too; `STATUS_TEXT` online/error
     became tokens (`--color-status-{online,error}-text`, dark = the hue).
   - Browser proof: axe colour-contrast on 10 pages in light mode — 0
     violations. Dark: identical findings before/after (all pre-existing).
   - The memory graph resolves its canvas palette per theme and redraws on a
     switch; community colours have a deeper light set (≥3:1 on paper).
   - Agent identity hues keep their hue; the lightness is a CSS variable
     (75 % dark since the dark-contrast pass — was 55 % —, 25 % light).
7. **Guards (vitest, run in CI).**
   - `no-hex-alpha-concat` — any `${…}NN`, `x + "NN"`, `hexToRgb(` or
     `rgba(${…})` in source fails, with file:line.
   - `no-raw-colors` — ratchet: raw hex/rgba/Tailwind palette classes only in
     six allowlisted files with an exact count and a reason; the count may
     only go down.
   - `colors.tokens` — every token and every `var(--color-…)` used in `src`
     is defined in `:root`; board colours and the terminal theme are literal.
   - `theme.contrast` — WCAG contrast computed from `globals.css` for both
     modes, light parity for every literal dark token.
   - `theme` — executes the init script; storage failures, OS switch, other
     tab, meta theme-color.
   - `use-client-first` — a codemod must not put an import above
     `"use client"`.
   - `no-dark-class` — no `dark` class on `<html>`, no `dark:` utility, no
     `.dark` selector.

## Alternatives

- **Rebase PR #537** → rejected: seven conflicts over 63 commits, a
  colliding ADR number, light colours below AA, new tokens in `@theme`
  (tree-shaking risk), `hexToRgb`/database colour left open, no canvas redraw,
  no system/other-tab listener.
- **Light as the new default / light-first design** → rejected by the
  operator: dark is the product's character.
- **Tailwind `dark:` variants per component** → rejected: 0 existing uses,
  would double every colour declaration; variables switch everything at once.
- **Save the choice on the server (per user)** → later, if wanted. Per
  browser needs no API, works before login and on shared screens.
- **A second light-only token file** → rejected: one block of overrides next
  to the dark values keeps parity visible and testable.

## Consequences

### Positive
- Dark users see no change apart from the new switch (dark values identical;
  screenshots of 12 pages before/after differ only in live data, the new
  footer button and the new Appearance entry; axe colour-contrast findings in
  dark are identical before/after).
- A new colour must come from a token; the two guards make the silent
  failure modes (hex suffix, raw colour) red in CI.
- WCAG contrast is now computed in tests instead of claimed in comments.

### Negative
- Colours are strings like `var(--color-status-error)`: tests compare
  against the token, not a computed rgb (jsdom has no stylesheet).
- Canvas/xterm/third-party consumers need `resolveColor()` or literals.
- About 20 off-palette values (e.g. Tailwind red-500 errors, teal hints,
  near-black scrims) were folded into the nearest token — visually tiny in
  dark, but no longer bit-identical at those spots.
- Known dark-mode gaps, pre-existing and **not** changed here: status text
  (online/error/info, warning-text) on `bg-elevated`/`bg-hover` is 3.3–4.49:1
  (recorded in `theme.contrast.test` as a ratchet); `text-dim` used as meta
  text is 4.38:1 on `bg-surface`; agent identity hues on dark surfaces reach
  only ~2.3:1 as text. Follow-up: lift the dark `--color-status-*-text` and
  `--color-text-dim` tones (a visible dark change — operator decision).
  **Closed by the dark-contrast pass (operator-approved follow-up):** the dark
  status hues are lifted in OKLCH (same hue and chroma) to ≥4.5:1 on all five
  surfaces and on their own 12 % tint over surface/elevated; `text-muted`
  `#b3b3b3`, `text-dim` `#a3a3a3` (AA on every surface); `on-status` dark is
  `#161616`; agent lightness 75 %. `theme.contrast.test` has no dark
  exceptions any more. Text dimmed with `opacity` was removed in both modes
  (the light-only `light:opacity-100!` overrides are gone). UI probe dark
  contrast findings: 248 → see the PR.

### Light-only adjustments (UI probe, contrast check)

The UI probe (`--theme light --contrast`, see `docs/ui-probe.md`) opens every
page, panel and dialog in light mode and checks text contrast per state. What
it found and how it is fixed:

- **Text dimmed with `opacity`** (0.5–0.9 on muted tones, deactivated rows,
  trash items, hints) stays readable on dark but drops to 2–4:1 on paper.
  Fixed with the `light:` variant (`@custom-variant light` in `globals.css`):
  `light:opacity-100!` at those spots. Dark keeps its dimming unchanged; the
  state stays visible through chips/labels.
- **Text on solid fills:** `background: C.error` + `color: C.textPrimary` is
  ink on dark red in light (2.2:1) → new token `--color-on-status` (dark =
  `var(--color-text-primary)`, identical; light = white). Guarded by
  `on-status-text.test`.
- **Text on the accent fill with `C.textPrimary`** was invisible in BOTH
  modes (off-cream on off-cream in dark, 1.09:1) — five save/confirm buttons.
  Now `C.onAccent`. This is the one visible dark change of this round, a bug
  fix; the same test guards it.
- Light `--color-text-dim` is a step darker (`#625e56`, ≥4.85:1 on all five
  surfaces incl. hover), since it is used for counters on hovered rows.

## References

- Files: `frontend-v2/src/styles/globals.css`, `frontend-v2/src/lib/colors.ts`,
  `frontend-v2/src/lib/theme.ts`, `frontend-v2/src/lib/themeScript.ts`,
  `frontend-v2/src/app/layout.tsx`, `frontend-v2/src/app/providers.tsx`,
  `frontend-v2/src/components/shared/ThemeSwitch.tsx`,
  `frontend-v2/src/components/settings/AppearanceSection.tsx`,
  `frontend-v2/src/components/memory/graphConfig.ts`
- Tests: `frontend-v2/src/lib/__tests__/{no-hex-alpha-concat,no-dark-class,no-raw-colors,colors.tokens,colors.alpha,theme,theme.contrast,use-client-first}.test.ts`
- Replaces: PR #537 (not merged)
- Related: ADR-076 (radii), PRODUCT.md / DESIGN.md (design doctrine)
