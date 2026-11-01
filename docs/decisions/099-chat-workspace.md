# ADR-099 — Chat workspace: one chat rail on the chats page, panel buttons in the chat header, a resizable side panel

**Status:** Accepted (2026-11-01) · amends ADR-090 §1 (the closed feature list) · touches ADR-085 §4 (build stop on the Sessions chat) and DESIGN.md "Navigation"
**Datum:** 2026-11-01
**Scope:** Frontend/Pages · Frontend/Navigation · Frontend/Chat

## Kontext

On the desktop the chats page (`/sessions`) showed up to three columns left of
the chat and a fourth strip right of it:

- the **app sidebar** with every page (`components/layout/Sidebar.tsx`, 248 px),
- the **chat list** (`components/chat/SessionSidebar.tsx`, 256 px), grouped by
  project, with an Agents / Groups toggle and a separate heads section,
- the chat, whose header carried a "live" badge and the detail-level picker
  ("Normal ▾"),
- a 58 px **panel strip** (`components/chat/PanelRail.tsx`) with two buttons
  (changes, browser) that open a side panel of fixed width (`45 %`, at most
  720 px).

The operator asked for three things (2026-11-01): the left side should become
the chat navigation the way desktop coding clients do it; the panel strip, the
"live" badge and the "Normal" picker should go and the changes / browser
buttons should sit in the chat header; the side panel should be resizable.
The context box above the composer was also cut off whenever the side panel
was open (two `overflow-hidden` ancestors in `app/sessions/page.tsx`).

A concept with three drafts, a critique and phone and desktop mockups was
made the same day. The critique found what the app sidebar carries besides
pages — board picker, search (⌘K), the Jarvis mic (ADR-091), warning counts and
the status footer — so none of it may disappear behind a menu.

**Why an ADR:** ADR-085 §4 froze the Sessions chat; ADR-090 §1 unfroze the chat
of the two fixed agents only inside a **closed feature list**. A new left rail,
a new grouping of the chat list, new header buttons and a resizable panel are
not on that list. Without this ADR the four PRs below would bypass both.

## Entscheidung

The operator approved the concept on **2026-11-01**, including every
recommended default (E1–E6 below).

### 1. Additions to the ADR-090 §1 feature list

ADR-090 §1 gains exactly these items. They change **layout and navigation
only** — no backend, no migration, no new rights, no change to what a chat can
send or receive.

1. **Chat rail and activity grouping** (chats page only, desktop):
   - one left rail instead of app sidebar + chat list: wordmark, Jarvis mic,
     collapse button, search (⌘K), "+ New chat", a few pinned pages (default
     Home, Tasks, Inbox, Runtimes) and "All pages ›";
   - "All pages ›" opens the full app sidebar (every page, board picker, user
     menu) as a drawer from the left; Esc, ✕ or a click outside closes it;
   - the status footer stays at the bottom of the rail;
   - the chat list is grouped like a desktop chat client: **Pinned** on top
     (pinned chats never move), then **Today / Yesterday / Last 7 days /
     Older** by last activity. The project is a small suffix in the row, not
     a group. Group chats and heads are normal rows of the same list (group
     icon / member count); the Agents / Groups toggle goes. One status dot per
     row: green = working, ochre = waits for the operator, grey = quiet.
   - The phone list (390 px) uses the same grouping; each row shows name,
     last message or state below, time right.
2. **Header buttons Changes and Browser** (desktop chat header): the panel
   strip goes; the two buttons sit in the header, pressed state as today
   (`aria-pressed`). The "live" word becomes a small dot with a spoken label
   and tooltip (i18n); "ended" stays a word because it is a warning. The
   detail level (compact / normal / verbose) moves into the ⋯ menu — moved,
   not removed. Group chats keep their own result-document button; heads and
   group chats get no changes / browser buttons.
3. **Resizable side panel** (desktop): a drag handle (`role="separator"`,
   arrow keys, double-click resets) for all three panel kinds (changes,
   browser, group result document); panel at least 320 px and at most 70 %,
   chat at least 400 px; the width is remembered per browser; under ~1100 px
   viewport the panel lies over the chat instead of beside it.

The context-box fix (portal + fixed position, flip at the viewport edge) is a
bug fix and needs no list entry; it lands before the drag handle, because the
handle can make the chat narrower than today.

### 2. Operator decisions (2026-11-01)

| # | Question | Decision |
|---|---|---|
| E1 | What is "Normal"? | the detail-level picker in the chat header → into the ⋯ menu, not deleted |
| E2 | How is the app menu reached from the chat rail? | one rail (wordmark, mic, collapse, search, + new chat, pinned pages, "All pages ›" drawer, status footer) |
| E3 | Chat rail on every page? | **chats page only** for now; every other page keeps today's app sidebar |
| E4 | Phone header | changes / browser stay in ⋯ (K4: at most one action next to ⋯ — the mic); only "live" becomes a dot |
| E5 | ADR + DESIGN.md "Navigation" | yes, this ADR |
| E6 | Chat list grouping | pinned on top, then by last activity; project as suffix; groups and heads as rows; one dot per row |

### 3. Phone

The phone flow (list → chat → back, ADR-090 "phone first") stays. On the phone
only the chat header changes ("live" → dot) and the chat list gets the
activity grouping of §1.1. No new header buttons on the phone (K4).

### 4. What this changes

| Where | Before | Now |
|---|---|---|
| ADR-090 §1 feature list | reliability, todo panel, cache rate, P1–P7, effort, automations | + chat rail and activity grouping, header buttons changes / browser, resizable side panel (§1 above) |
| ADR-085 §4 | Sessions chat frozen except ADR-089 / ADR-090 | unchanged for input, delivery and group chat behaviour; only layout and navigation of the chats page per §1 |
| DESIGN.md "Navigation" | desktop = sidebar with pages | desktop = sidebar with pages, **except the chats page**: one chat rail with pinned pages and the full sidebar as a drawer |
| ADR-091 (mic) | mic in the sidebar (desktop) and the chat header (phone) | desktop mic moves into the chat rail head on `/sessions`; phone unchanged |

## Alternativen

- **Only a ☰ menu in the chat rail** → rejected: mic, search, warning counts
  and the status footer would hide behind one more click (regression against
  ADR-091).
- **Keep the 64 px collapsed app sidebar next to the chat list** → rejected:
  still two left rails, which is what the operator wants gone.
- **Chat rail on every page** → later: a rebuild of all pages without an
  intermediate step. Revisit after two weeks of daily use with the chats page.
- **Changes / browser buttons in the phone header** → rejected: breaks K4 and
  takes about 56 px from the name (measured header metrics in `ChatView.tsx`).

## Konsequenzen

### Positiv

- One left rail on the chats page; nothing is lost (every page stays one
  click away via the pinned pages, the drawer, ⌘K and the phone menu).
- The chat header carries the panel buttons where the eye already is.
- The context box and the composer menus are never cut off again.

### Negativ

- `app/sessions/page.tsx`, `ChatView.tsx` and `SessionSidebar.tsx` are large
  files touched in series; PRs must land one after the other.
- Two navigation shapes on the desktop (chat rail on `/sessions`, sidebar
  elsewhere) until the "every page" question is decided.

### Rückweg

Each PR is reverted on its own; no data, no migration, no backend.
Remembered widths and pins live in the browser and are ignored after a
revert.

## Referenzen

- `frontend-v2/src/app/sessions/page.tsx`, `components/chat/ChatView.tsx`,
  `components/chat/PanelRail.tsx`, `components/chat/SessionSidebar.tsx`,
  `components/chat/ContextPanel.tsx`, `components/chat/Composer.tsx`,
  `components/layout/Sidebar.tsx`, `components/layout/AppShell.tsx`, `lib/nav.ts`
- DESIGN.md K4, K11, K14 and "Navigation"
- Related ADRs: ADR-085, ADR-088, ADR-090, ADR-091
