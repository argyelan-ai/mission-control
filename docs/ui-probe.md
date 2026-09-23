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

| Argument | Default | Meaning |
|---|---|---|
| `--base URL` | `http://localhost` | The running UI (same origin as the API). |
| `--out DIR` | `<tmp>/mc-ui-probe/<timestamp>` | Output folder. Keep it outside the repo; `ui-probe-out/` is git-ignored as a fallback. |
| `--route /tasks` | all pages | Repeatable or comma-separated. `/agents/*` also takes `/agents/[id]`. |
| `--width 390` | `1440` and `390` | Repeatable or comma-separated. Widths ≤ 500 run as a touch phone. |
| `--max-per-page N` | no limit | Cap candidates per page (quick smoke runs). |
| `--headed` | off | Show the browser. |

The token is read from `MC_PROBE_TOKEN` only, placed into the browser's local
storage and used for the GET requests that resolve dynamic routes. It is never
printed or written to the report; logged URLs are stripped of query strings.

Exit codes: `0` done, `2` a write request got through (must never happen),
`3` the write-lock self-test failed (nothing was clicked), `64` usage error.

## What it does

1. **Pages** come from `src/app/**/page.tsx`, so a new page is probed without
   editing a list. Dynamic routes are filled with the first real id from a GET
   endpoint (`DYNAMIC_SOURCES` in `scripts/probe/lib/routes.mjs`); a dynamic
   route without a source is skipped and listed in the report.
2. **Candidates** per page and width: `button`, `select`, `[role=combobox]`,
   `[aria-haspopup]`, `[aria-expanded=false]`, `summary`, `[role=tab]`.
   Sidebar and header controls are probed on the first page of each width only.
3. Each candidate is **clicked, measured, screenshotted and closed** again
   (Escape; then a tap on the scrim; else the page is reloaded). A native
   `select` is not clicked; its options are read instead. Inside a freshly
   opened dialog, its tabs and accordions are opened too ("nested").
4. **Findings** per open state:

| Type | Severity | Meaning |
|---|---|---|
| `offscreen` | high | A floating layer sticks out of the viewport. |
| `covered` | high | `elementFromPoint` probe: 2+ of 5 points on the layer hit old page content — the layer sits behind something. |
| `clipped` | high | The layer is cut off by an ancestor with `overflow: hidden`. |
| `h-scroll` | high | The page scrolls sideways. |
| `esc` | medium / low | Escape does not close a floating layer (low: only a synthetic Escape closes it — check by hand). |
| `empty` | medium | A layer opened with no text, media or controls, or a page shows nothing. |
| `clipped-text` | medium | Text cut off without an ellipsis. |
| `console-error` | medium | Browser console error in that state (errors caused by the probe's own lock are filtered). |
| `small-target` | low | Controls under 44 px, phone widths only. |

5. **Output** in `--out`: `report.md` (opened X of Y per page, findings with
   screenshot paths, guarded controls, blocked writes), `probe.json` (all raw
   data) and `<width>/<page>/NNN-<control>.png`.

"Opened" counts clicks that produced a new visible state. "No change" controls
did nothing visible (often plain actions). `withoutAriaRole` in the JSON counts
custom openers without `aria-expanded` / `role=tab` — a number that should only
go down.

## Read-only by construction

The probe runs against live instances, so it must not be able to change
anything:

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

Note that some pages send harmless read-like POSTs (for example a view
tracker); they are blocked and counted as well, so a non-zero "blocked" number
is expected. "passed" is the number that must be 0.

## Tests

The pure parts (route list, never-click list, finding heuristics, report) are
covered by vitest: `npx vitest run scripts/probe`.
