# ADR-099 — Chat workspace: panel buttons in the chat header, a resizable side panel

**Status:** Accepted (2026-11-01) · amends ADR-090 §1 (the closed feature list) · touches ADR-085 §4 (build stop on the Sessions chat)
**Datum:** 2026-11-01
**Scope:** Frontend/Pages · Frontend/Chat

## Kontext

On the desktop the chats page (`/sessions`) showed a 58 px **panel strip**
(`components/chat/PanelRail.tsx`) right of the chat, with two buttons
(changes, browser) that open a side panel of fixed width (`45 %`, at most
720 px). The chat header carried a "live" badge and the detail-level picker
("Normal ▾").

The operator asked (2026-11-01): the panel strip, the "live" badge and the
"Normal" picker should go and the changes / browser buttons should sit in the
chat header; the side panel should be resizable. The context box above the
composer was also cut off whenever the side panel was open (two
`overflow-hidden` ancestors in `app/sessions/page.tsx`).

**Why an ADR:** ADR-085 §4 froze the Sessions chat; ADR-090 §1 unfroze the chat
of the two fixed agents only inside a **closed feature list**. New header
buttons and a resizable panel are not on that list. Without this ADR the PRs
below would bypass both.

**Out of scope:** the concept of the same day also proposed one chat rail on
the chats page (chat list with pinned pages, activity grouping, all pages as a
drawer). That part is **not** decided here — the operator parked it on
2026-11-01 while MC's direction is being re-thought. The left side of the
chats page stays as it is.

## Entscheidung

The operator approved the header buttons and the resizable panel on
**2026-11-01**, including the recommended defaults (E1, E4 below).

### 1. Additions to the ADR-090 §1 feature list

ADR-090 §1 gains exactly these items. They change **layout only** — no
backend, no migration, no new rights, no change to what a chat can send or
receive.

1. **Header buttons Changes and Browser** (desktop chat header): the panel
   strip goes; the two buttons sit in the header, pressed state as before
   (`aria-pressed`), a second click closes the panel. The "live" word becomes
   a small dot before the name with a spoken label and tooltip (i18n); "ended"
   stays a word because it is a warning. The detail level (compact / normal /
   verbose) moves into a ⋯ menu — moved, not removed. Group chats keep their
   own result-document button; heads and group chats get no changes / browser
   buttons.
2. **Resizable side panel** (desktop): a drag handle (`role="separator"`,
   arrow keys, double-click resets) for all three panel kinds (changes,
   browser, group result document); panel at least 320 px and at most 70 %,
   chat at least 400 px; the width is remembered per browser; when chat and
   panel do not fit side by side (today below ~1100 px of window) the panel
   lies over the chat instead, with its own close button.

The context-box fix (portal + fixed position, flip at the viewport edge) is a
bug fix and needs no list entry; it lands before the drag handle, because the
handle can make the chat narrower than before.

### 2. Operator decisions (2026-11-01)

| # | Question | Decision |
|---|---|---|
| E1 | What is "Normal"? | the detail-level picker in the chat header → into the ⋯ menu, not deleted |
| E4 | Phone header | changes / browser stay in ⋯ (K4: at most one action next to ⋯ — the mic); only "live" becomes a dot |
| E5 | ADR | yes, this ADR |

### 3. Phone

The phone flow (list → chat → back, ADR-090 "phone first") stays. On the phone
only the chat header changes ("live" → dot). No new header buttons on the
phone (K4); the panels open from the ⋯ sheet as before.

### 4. What this changes

| Where | Before | Now |
|---|---|---|
| ADR-090 §1 feature list | reliability, todo panel, cache rate, P1–P7, effort, automations | + header buttons changes / browser, resizable side panel (§1 above) |
| ADR-085 §4 | Sessions chat frozen except ADR-089 / ADR-090 | unchanged for input, delivery and group chat behaviour; only the chat header and the panel column per §1 |
| DESIGN.md "Navigation" | — | **unchanged** (the chat rail is not decided) |

## Alternativen

- **Keep the strip, only add a resize handle** → rejected by the operator: the
  strip is a column with two buttons and no information.
- **Changes / browser buttons in the phone header** → rejected: breaks K4 and
  takes about 56 px from the name (measured header metrics in `ChatView.tsx`).
- **A resize library** → not needed; the handle is one small component and a
  pure width module.

## Konsequenzen

### Positiv

- The chat header carries the panel buttons where the eye already is; one
  column less on the desktop.
- The panel width fits the task (a wide diff, a narrow browser picture).
- The context box and the composer menus are never cut off again.

### Negativ

- `app/sessions/page.tsx` and `ChatView.tsx` are large files touched in
  series; the PRs land one after the other.

### Rückweg

Each PR is reverted on its own; no data, no migration, no backend. The
remembered width lives in the browser and is ignored after a revert.

## Referenzen

- `frontend-v2/src/app/sessions/page.tsx`, `components/chat/ChatView.tsx`,
  `components/chat/PanelRail.tsx`, `components/chat/ContextPanel.tsx`,
  `components/chat/Composer.tsx`
- DESIGN.md K4, K11, K14
- Related ADRs: ADR-085, ADR-088, ADR-090, ADR-091
