# UI probe ("open everything")

Most visual bugs in the UI only show up once something is opened: a dropdown
that runs off the screen, a menu hidden behind a card, a dialog that ignores
Escape, a panel that makes the phone view scroll sideways. The UI probe finds
them mechanically. It visits every page at desktop and phone width, opens every
panel, dropdown, menu, tab, accordion and dialog one after the other,
screenshots each open state and writes a report.

**Run it before and after every UI wave** (and before a frontend deploy). The
"before" report is the baseline; the "after" report must not have more
findings or fewer opened controls.

## Run

```bash
cd frontend-v2
export MC_PROBE_TOKEN=...            # an admin token for the target instance
npm run probe -- --base http://localhost --out ../../ui-probe-runs/before-wave-2
```

**Getting a token.** Any valid admin access token works — the one the UI
keeps in local storage (`mc_auth_token`, browser dev tools) or a short-lived
one signed inside the backend container:

```bash
docker compose exec -T backend python -c "
from datetime import timedelta
from app.auth import create_access_token
print(create_access_token('<admin user id>', 'admin', <token_version>, timedelta(hours=3)))"
```

(`id` and `token_version` come from the `users` table.) Signing a token does
not write anything. Keep it in the environment variable only.

A full run (all pages, both widths) takes a while — roughly an hour on a
populated instance. Use `--route` and `--width` for quick checks.

| Argument | Default | Meaning |
|---|---|---|
| `--base URL` | `http://localhost` | The running UI (same origin as the API). |
| `--out DIR` | `<tmp>/mc-ui-probe/<timestamp>` | Output folder. Keep it outside the repo; `ui-probe-out/` is git-ignored as a fallback. |
| `--route /tasks` | all pages | Repeatable or comma-separated. `/agents/*` also takes `/agents/[id]`. |
| `--width 390` | `1440` and `390` | Repeatable or comma-separated. Widths ≤ 500 run as a touch phone. |
| `--max-per-page N` | no limit | Cap candidates per page (quick smoke runs). |
| `--nested-max N` | `20` | Second-level openers per opened state (`0` = first level only). |
| `--load-timeout MS` | `20000` | How long to wait for "Loading…" texts / `aria-busy` to disappear after a page load (clicks: up to 10 s). |
| `--all-repeats` | off | Probe every copy of a repeated component instead of the first and the last. |
| `--headed` | off | Show the browser. |
| `--theme light` | the app's own default | `dark`, `light` or `system`: stores the theme choice in local storage before any page script runs (the app's `<head>` script then applies it before the first paint, the real code path) and emulates the same OS colour scheme. The report names the theme; a page that renders another theme is a `theme` finding. |
| `--contrast` | off | Adds the text-contrast check (WCAG AA) to every state, see `contrast` below. |

The token is read from `MC_PROBE_TOKEN` only, placed into the browser's local
storage and used for the GET requests that resolve dynamic routes. It is never
printed or written to the report; logged URLs are stripped of query strings.

Exit codes: `0` done, `2` a write request got through (must never happen),
`3` the write-lock self-test failed (nothing was clicked), `64` usage error.

## What it does

1. **Pages** come from `src/app/**/page.tsx`, so a new page is probed without
   editing a list. Dynamic routes are filled with the first real id from a GET
   endpoint (`DYNAMIC_SOURCES` in `scripts/probe/lib/routes.mjs`, one GET or a
   chain such as board → task); a dynamic route without a source is skipped
   and listed in the report. Big panels that are reached by a deep link rather
   than a page of their own are listed in `EXTRA_VIEWS` — today the task
   detail (`/tasks?task=<id>`).
2. **Wait until loaded.** After every load and every click the probe waits
   until no short "Loading…" / "… wird geladen…" text and no `aria-busy`
   region is visible. A page that is still loading after `--load-timeout` is
   reported (`loading`, high) because its controls were probed incomplete.
3. **Candidates** per page and width: `button`, `select`, `[role=combobox]`,
   `[aria-haspopup]` (including menu items that open a submenu),
   `[aria-expanded=false]`, `summary`, `[role=tab]`. Sidebar and header
   controls are probed on the first page of each width only. **Repeated
   components** (the same menu on every row, "Actions: A", "Actions: B", …)
   are sampled: the first and the last are probed, the rest is counted as
   "sampled out" (`--all-repeats` turns this off).
4. Each candidate is **clicked, measured, screenshotted and closed** again. A
   floating layer is first checked for Escape (then a tap on the scrim, else
   a reload); a tab is switched back, a toggle clicked again. A native
   `select` is not clicked; its options are read and counted as "read", not
   "opened".
5. **Second level.** Every opener that the first click revealed — dropdowns
   and selects inside a settings tab, accordions in a section, tabs and wizard
   steps in a dialog, rows in a detail panel — is clicked, measured and
   screenshotted as well (`--nested-max` per state). "Close"/"Back" buttons
   and, inside a dialog, its own "Create …" confirm button
   are skipped there; if a nested click tears the parent down anyway, the
   parent is re-opened from a fresh load and the pass continues.
6. **Findings** per open state:

| Type | Severity | Meaning |
|---|---|---|
| `offscreen` | high | A floating layer sticks out of the viewport. |
| `covered` | high | `elementFromPoint` probe: 2+ of 5 points on the layer hit old page content — the layer sits behind something. |
| `clipped` | high | The layer is cut off by an ancestor with `overflow: hidden`. |
| `h-scroll` | high | The page scrolls sideways. |
| `loading` | high / medium | The page (high) or an opened view (medium) still shows "Loading…" after the wait. |
| `esc` | medium | Escape does not close a floating layer, or only a synthetic Escape event does (the handler exists but misses a real key press — users are affected). Desktop widths only: phones have no Escape key. |
| `empty` | medium | A layer opened with no text, media or controls, or a page shows nothing. |
| `clipped-text` | medium | Text cut off without an ellipsis. |
| `console-error` | medium | Browser console error in that state (errors caused by the probe's own lock are filtered). |
| `small-target` | low | Controls under 44 px, phone widths only. |
| `theme` | high | With `--theme`: the page renders another theme than the forced one. |
| `contrast` | high | With `--contrast`: text below WCAG AA (4.5:1, large text 3:1) against the background right behind it. One finding per colour pair, with example texts. |

**How `contrast` measures (a heuristic, not axe).** For every visible element
with its own letters or digits (the whole page on load, only the new elements
of an opened state) the probe reads the text colour and the background colour
and opacity of every ancestor up to `<html>`, normalised to sRGB through a
canvas (so `color-mix()`/`oklch()` work). `scripts/probe/lib/contrast.mjs`
composites them like the browser does (an ancestor's opacity fades its
background and the text together; a white canvas at the bottom) and computes
the WCAG ratio. Disabled controls are exempt, as in WCAG. Its limits: what
really sits behind a positioned element is not always its DOM ancestor, text
on a background image or gradient is skipped and counted ("skipped" in the
report header), and canvas drawings (the memory graph) are not text.

7. **Output** in `--out`: `report.md` (opened X of Y per page, findings with
   screenshot paths, guarded controls, blocked writes), `probe.json` (all raw
   data) and `<width>/<page>/NNN-<control>.png`.

"Opened" counts clicks that produced a new visible state (a layer, an
expanded section, a switched tab). "Nested" counts second-level states.
"No change" controls did nothing visible (often plain actions). A finding
that repeats lists every opener that led to it.

### What it does not reach (yet)

- Third level and beyond (e.g. wizard step 3 of 5 is only reached if step 2's
  "Next" was a second-level opener; controls inside that step are not probed).
- Hover-only tooltips and popovers.
- Clickable cards and rows without a button role (`div`/`a` with `onClick`)
  — they are invisible to a role-based probe and to screen readers alike. The
  task detail is covered through its deep link; other such cards should get a
  proper role, which then makes them probe-able automatically. `withoutAriaRole` in the JSON counts
custom openers without `aria-expanded` / `role=tab` — a number that should only
go down.

## Write lock

The probe runs against live instances, so it must not be able to change
anything through the UI:

- Every request that is not `GET`, `HEAD` or `OPTIONS` is aborted in the
  browser and counted. WebSockets are refused entirely, service workers are
  blocked.
- A second, independent counter records any write request that *finished*. It
  must stay 0; if not, the run exits with code 2.
- Before any page is opened, a **self-test** POSTs to a non-existent path in a
  locked browser and checks that it was blocked. If not, the run stops.
- Controls whose label reads like an action are never clicked, English and
  German (delete, stop, dispatch, approve, deploy, restart, switch, save,
  send, submit, log out, löschen, speichern, freigeben, …; full list in
  `scripts/probe/lib/guard.mjs`). Submit buttons, switches, checkboxes and menu
  items are never clicked either. Tabs are exempt: they only change the view.
  "New"/"Add"/"Create" buttons are allowed because they open dialogs; the
  dialog's own confirm button is never clicked.
- Browser `confirm()`/`alert()` dialogs are dismissed; popups are closed.
- The token is written into local storage on the app's own origin only,
  never into a foreign iframe or popup. Console messages and page errors are
  scrubbed (`token=…`, `Bearer …`, JWT-shaped strings, the token itself)
  before they reach `probe.json` / `report.md`.
- The self-test also opens a WebSocket and checks that the probe refused it
  (it counts its own socket only: a dev server's hot-reload socket is refused
  as well, which `next dev` survives).

**Remaining risk: GET requests with side effects.** The lock works on HTTP
methods, not on the database. A few read endpoints update derived data as a
side effect (for example `GET /api/v1/groups/{id}/rounds` refreshes usage
numbers and commits). The normal UI does exactly the same when someone looks
at that page, so the probe causes nothing a visitor would not — but "no
write requests" is not the same as "no database write".

**Privacy of the output.** Screenshots, `probe.json` and `report.md` show the
instance's real data (agent names, task titles, project names). Keep `--out`
outside the repository and never attach run output to a PR or an issue.

Note that some pages send harmless read-like POSTs (for example a view
tracker); they are blocked and counted as well, so a non-zero "blocked" number
is expected. "passed" is the number that must be 0.

## Tests

The pure parts (route list, never-click list, finding heuristics, report) are
covered by vitest: `npx vitest run scripts/probe`.
