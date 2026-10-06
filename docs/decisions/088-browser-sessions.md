# ADR-088 — Browser sessions: one browser area per agent session and head run

**Status:** Accepted
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
   a stable server secret: `BROWSER_SESSION_SECRET`, else the encryption key —
   not the JWT secret, whose rotation would strand open sessions), never
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

## References

- `docker/cdp-browser/gateway/cdp_gateway.py` (register, `/s/<token>/`, `end_session`)
- `backend/app/models/browser_session.py`, `backend/alembic/versions/0211_browser_sessions.py`
- `backend/app/services/browser_sessions.py`, `backend/app/routers/browser_sessions.py`
- Related ADRs: ADR-085 (head per job, build stop), ADR-086 (harness × runtime cross-switch)
- PRs: #745, #746, #753 (panel follows tab, per-agent panel, attribution by path)
