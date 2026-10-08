# ADR-088 — Browser sessions: one browser area per agent session and head run

**Status:** Accepted · Addendum 2026-10-06 (operator decisions a–c, lifecycle)
**Datum:** 2026-10-06
**Scope:** Infra/cdp-browser · Backend/DB · Harness layer

## Context

Agents share one Chromium (`cdp-browser`, WebGL). The operator watches it in
the browser live panel (CDP screencast, view only). Since #746/#753 an
agent-aware front door (`cdp-gateway`, port 9300) labels each connection by
a `/a/<slug>/` path prefix and attributes tabs to agents. Two gaps remain:

- **The unit is the agent, and ownership is observed.** A tab belongs to
  "whoever created, claimed or last navigated it". A head run (ADR-085) has no
  slug at all, playwright-mcp serves every Claude agent as one unidentified
  client, and two runs of the same agent cannot be told apart.
- **Nothing is ever cleaned up.** Tabs live until Chromium restarts (two
  proof tabs of 2026-10-04 were still open two days later), and there is no
  record of what a run did in the browser once it ended.

The operator approved the target picture on 2026-10-06 (research of how
coding apps do it — Z-Code assigns an owner to every tab when it is created
and uses one browser context per session; ChatGPT agent, Claude in Chrome and
others also scope the browser to the task): every agent session and every
head run gets its **own browser area**, assigned when the session is
created; browsers start on demand, are cleaned up afterwards, the last image
stays visible, and the panel shows clear states. Taking over the page
(operator clicks/types) is explicitly later and needs its own decision.

## Decision

1. **The unit is the browser session.** One `browser_sessions` row per head
   run or per agent working phase: owner (`agent` + `agent_id`, or `head` +
   `head_run_id`), status `open → live → ended`, times, end reason. At most
   one not-ended session per owner (partial unique index). It is its own
   table — not a task column (task core frozen, ADR-085 §4).
2. **Ownership by construction.** MC registers the session at the gateway
   (`PUT /mc/sessions/<token>?session=<id>[&agent=<slug>]`); the harness uses
   the address `/s/<token>/`. Everything created over that address —
   contexts, tabs, popups — is the session's (`browserContextId`, opener,
   createTarget). `/mc/targets` reports `session` and `browserContextId`; a
   session opened for an agent still reports that agent, so the existing
   per-agent panel keeps working.
3. **The token is a credential.** An unknown `/s/` token is refused (404),
   never served as "unidentified". It is derived (HMAC of the session id with
   `SECRETS_ENCRYPTION_KEY`, which must stay stable anyway — not the JWT
   secret, whose rotation would strand open sessions), never
   stored, never listed (`GET /mc/sessions`, `GET /api/v1/browser-sessions`)
   and redacted from logs (`/mc/sessions/<token>`, `/s/<token>/`). Only the
   operator API's open call returns it (a repeat open returns the same,
   deterministic address).
4. **On demand, cleaned up, DB is the truth.** Registering creates nothing in
   Chromium. Ending a session (`DELETE /mc/sessions/<token>`) refuses the
   token from then on, cuts the session's open CDP connections, closes the
   tabs it **created** and disposes its contexts — a tab it merely claimed by
   navigating it is shown as the session's but never closed by its end. The
   gateway answers within a fixed cleanup deadline; leftovers are reported. The gateway register lives in memory; MC
   re-registers open sessions after a gateway restart (idempotent `PUT`).
   Sessions end with MC's run status, not with a dropped connection.
5. **Extend, don't replace (PRINCIPLES §3.10).** `/a/<slug>/` and unprefixed
   access stay unchanged until every harness has moved; claim-on-use stays as
   the rule for the shared default context.

Delivered in steps, each on its own: S1 foundation (this ADR's table, API,
gateway register and address) · lifecycle (re-register, idle, end on run
exit, last image) · harness wiring (playwright-mcp router per session, omp
relay prefix, head env) · tab strip and states in the panel · minimal
isolation per session (context filter, `Browser.close` blocked, switchable by
env) · image at run end in the run record.

## Addendum 2026-10-06 — operator decisions and the lifecycle

The operator decided the three open lifecycle questions on 2026-10-06:

- **(a) Persistent agents: one session per working phase.** It opens at the
  agent's first tab and ends after 30 minutes without browser activity
  (`browser_session_idle_s`). It is opened lazily by MC's lifecycle loop when
  a recently active tab on the agent's `/a/<slug>/` address shows up that no
  session owns (a tab idle past the limit, or a slug two agents share, opens
  nothing) — never through dispatch, which stays frozen (ADR-085 §4). Ending a phase can
  also close the agent's idle tabs (its connections stay, so the agent starts a
  new phase with its next tab) — **on by default** (`browser_idle_close_agent_tabs`)
  since a live test (2026-10-07) showed the local omp agent recovers from a tab
  closed under its open connection: its next browser call opened a working tab
  and answered normally. Setting it to false keeps only the record ending. An agent working without a break past `browser_session_max_age_s`
  only rolls over: the phase ends in the record and the next pass opens a new
  one, its tabs stay.
- **(b) The shared Chromium is always on.** Only the per-session parts
  (contexts, tabs) start on demand and are cleaned up; MC never starts or
  stops the browser container for a session.
- **(c) Logins are kept per agent** — not a fresh incognito window per
  session. Delivered as its own step after per-session isolation, because a
  stored login may only ever be restored into a context that belongs to that
  one agent's session:
  - storage state (cookies + localStorage per origin) is saved at the end of a
    session and periodically while it lives, and restored into the agent's
    own context at the start of its next session;
  - encrypted at rest with MC's existing Fernet mechanism
    (`services/encryption.py`, `SECRETS_ENCRYPTION_KEY`, as for `secrets` and
    `credentials`), in its own table — not in `credentials`, which agents can
    read through the agent API (ADR-033); never in the repository, logs or
    list responses;
  - agents stay separated: agent A never gets agent B's state; the shared
    default context never gets a stored login; at most one live session per
    login identity (a second one starts fresh, no write race);
  - an operator action deletes an agent's stored logins (API first, button
    with the tab strip);
  - **heads start fresh.** A head gets an agent's logins only when the
    operator explicitly starts it *as* that agent — never derived from the
    card's assigned agent. Heads run unattended and their sandbox already
    denies browser profiles and secrets; a stored login is standing access,
    so the default is least privilege.

  **Security trade-off.** Stored logins are standing access to the operator's
  accounts. Whoever holds both the database and `SECRETS_ENCRYPTION_KEY` holds
  every agent's logins — the same exposure `secrets` and `credentials` have
  today. Plaintext exists only briefly in backend memory, in the gateway's
  memory while restoring, and inside Chromium. A malicious page inside an
  agent's own context can still read that agent's cookies, as it can today.
  Mitigations: separation per agent, the delete action, a size cap, and no
  plaintext in logs or listings.

**Lifecycle (this step).** A loop in the background-services process
(`browser_session_lifecycle`, every `browser_sessions_interval` s) registers
open sessions the gateway forgot after a restart, moves `open → live` at the
first tab, records the last activity (`idleSeconds` per tab from the
gateway), ends an agent's phase after the idle limit, ends a head's session
when its run reached a final state (a vanished process or a missing run folder
gets one pass of grace) or after the run's own `time_limit_s` plus a margin
(measured from the first tab, or from the opening if it never got one). While
the gateway's watcher reconnects (`/mc/health` not OK) it makes no activity-
based decision (no idle end, no new phase). Stored URLs carry no query or
fragment (OAuth codes). Its own calls to the gateway stay out of httpx's INFO
log. It keeps the **last image**: a
JPEG of the session's most recently active tab (`GET /mc/sessions/<token>/snapshot`),
taken at most every `browser_frame_interval_s` and only after new activity,
and once more right before the end; stored as
`<browser_sessions_root>/<session-id>/last.jpg` (only its time, URL and title
in the row), served by `GET /api/v1/browser-sessions/{id}/last-frame`, and
deleted `browser_frame_retention_days` after the end (the row is marked, so
each pass moves on to the next batch). It also reconciles the
other way: a gateway session whose row has ended (open/end race, a gateway
error on DELETE) is ended at the gateway, and tabs an ended session created and
left behind (a `/json/new` still in flight) — and contexts whose disposal
Chromium did not confirm in time, which therefore keep their owner — are
swept (`GET /mc/orphans` lists them, `POST /mc/orphans/close?session=<id>`
closes them, refused while the session is registered). Cleanup reports count
only confirmed closes. Both re-read the row first, so an open session is never touched.
Ending an agent's phase closes only tabs the agent created or nobody known
created — never a tab another owner created that the agent merely claimed.
While the gateway is unreachable the loop changes nothing.

## Addendum 2026-10-07 — minimal isolation per session

`cdp-gateway` can now isolate browser sessions, behind `CDP_GATEWAY_ISOLATE`
(compose sets `"0"`; `"1"` after the live check; back to `"0"` needs no
rebuild). Only `/s/<token>/` connections are affected; `/a/<slug>/` and
unprefixed ones stay byte for byte (PRINCIPLES §3.10).

- **Unit of separation = browser context.** A session's tabs are those in
  contexts it owns — the ones its client creates (Playwright `--isolated`)
  and one context the gateway makes for it on first need — plus tabs it
  created. A `createTarget`, cookie-trio (`Storage.get/set/clearCookies`),
  permission or download command without a context goes to the session's
  context instead of Chromium's shared default context — **on every CDP
  session**, a tab's own and a page socket included: on Chromium 154 these
  take their context from the parameter wherever they are sent (the cookie
  trio on a tab session read, wrote and cleared the shared default context;
  with the injected context Chromium refuses it there instead). `/json/new`
  too. The gateway's context is disposed with the session.
- **What a session cannot see or do:** other targets in events,
  `getTargets` and `getBrowserContexts`; `attachToTarget`/`closeTarget`/
  `activateTarget` or a page socket or `/json/close|activate` for a foreign
  tab; a foreign `browserContextId` or window; a CDP session id it does not
  hold (top level or in params); non-flat ("wrapped") sessions. Browser-level
  commands are an allowlist (what Puppeteer and Playwright use for their own
  tabs and contexts); anything else browser-wide is refused, and so are
  `Browser.*` methods outside that list and `Target.setRemoteLocations` on a
  tab's session. Other commands on a tab's own session are not limited
  (lab-checked: `Storage.clearDataForOrigin`/`clearDataForStorageKey` and
  `Network.clearBrowserCookies` there leave the default context alone).
  `Browser.close` and `Browser.crash` never reach Chromium **from any
  session** (Chromium 154 exits on one sent on a tab session too): `{}` back,
  and only that connection ends.
- **Fail closed.** Only a JSON object with a non-negative int `id`, a string
  `method` (params an object, sessionId a string) is accepted, parsed the way
  Chromium parses it (raw control characters inside strings allowed), and
  forwarded **re-serialised** — Chromium never sees bytes the filter did not
  read. Anything else (unparseable text, a float id, invalid UTF-8, a binary
  frame, an unmasked or oversized control frame, a new message inside a
  fragmented one) ends the connection. A message over 64 MiB ends it too,
  logged (owner key, never the token) — a very large `getResponseBody` or
  full-page screenshot fails on an isolated session.
- **No hangs for others:** Playwright and Puppeteer auto-attach with
  `waitForDebuggerOnStart`. When such a client is auto-attached to a foreign
  tab, the gateway hides the event, resumes the tab and detaches.
  Lab-verified with real Chromium: dropping the event without either makes
  the other session's new tab hang.
- **Not a sandbox.** This separates honest clients sharing one browser; a
  page can still reach whatever the browser can reach on the network, and
  the unprefixed and agent paths stay open on the internal network.

Verified: unit and socket tests with a fake Chromium, and
`docker/cdp-browser/test_isolation.sh` in CI — real Chromium, a
Puppeteer-pattern client next to real Playwright, isolation on and off.
Checked once more in the lab with real puppeteer-core 24 (omp's library).

## Alternatives

- **Keep the agent as the unit, improve the guessing** → rejected: heads have
  no stable name, and "who navigated last" mislabels shared tabs by design.
- **One Chromium per agent or run** → rejected for now: ~400 MB each on a
  shared Docker VM; a browser context gives the separation at a fraction of
  the cost. Stays possible later as another provider behind the same seam.
- **Stateless signed tokens the gateway verifies itself** → rejected: needs
  a shared secret in the browser container and has no revocation when a
  session ends; an in-memory register MC refills is simpler.
- **Store the token in the database** → rejected: a dump would hand out live
  browser addresses; deriving it costs nothing.

## Consequences

### Positive
- Heads can get a browser at all, and every tab has an owner from the start.
- One clean end path: tabs, contexts and connections go away together.
- The table is where the last image, tab counts and history attach later.

### Negative
- The gateway register is lost on restart until MC re-registers (lifecycle step).
- Until isolation lands, a session can still see and touch other tabs in the
  shared browser; logins are shared within Chromium's default context.
- `/mc/*` stays unauthenticated on the internal network; ending a session
  needs its token, listing does not reveal it.
- Residual token risks: (a) right after a gateway restart, anyone on the
  internal network could pre-register a known session id (ids are visible in
  `/mc/targets`) with a token of their own, so MC's re-register gets 409 and
  that session must be ended and reopened; (b) changing
  `SECRETS_ENCRYPTION_KEY` changes every token and strands the sessions open
  at that moment (end them first).

## References

- `docker/cdp-browser/gateway/cdp_gateway.py` (register, `/s/<token>/`, `end_session`)
- `backend/app/models/browser_session.py`, `backend/alembic/versions/0211_browser_sessions.py`
- `backend/app/services/browser_sessions.py`, `backend/app/routers/browser_sessions.py`
- Related ADRs: ADR-085 (head per job, build stop), ADR-086 (harness × runtime cross-switch)
- PRs: #745, #746, #753 (panel follows tab, per-agent panel, attribution by path)
