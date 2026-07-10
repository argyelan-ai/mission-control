# ADR-060 — Agent Onboarding Wizard + Generic Host-Agent Provisioning

**Status:** Accepted
**Datum:** 2026-07-10
**Scope:** Backend/Provisioning · Backend/API · Frontend/Pages · Backend/DB

## Kontext

Creating a new agent previously meant picking between three separate modals
depending on runtime (cli-bridge quick-create, a manual gateway-era dialog,
and an ad-hoc host-agent flow) — each with its own field set, its own
partial validation, and its own idea of what "provisioned" meant. None of
them let an operator preview the rendered SOUL.md before committing, pick
scopes with an explanation of what each one unlocks, or see whether the
agent was actually reachable after creation (the old synchronous
trigger-based dry-run relied on the gateway's chat-send RPC, which Phase 29
(ADR-039) removed — leaving no readiness signal at all for host agents).

Host runtime agents (Boss/Hermes/Jarvis-style, but now generic — any
future host-side agent) additionally had a narrower problem: nothing
rendered their launchd `.plist` / `run.sh` / `agent.env` outside of the
Hermes-specific bootstrap path (ADR-029). A wizard that wants to onboard
*any* host agent needs a generic staging service, not another
runtime-specific special case.

## Entscheidung

1. **One create path.** The wizard (`frontend-v2/src/app/agents/wizard/`)
   funnels every runtime through the existing `POST /agents` endpoint with
   create-time `harness`, `scopes`, `soul_md`, and `skill_filter` — no
   parallel creation code path. A live SOUL.md preview is served by a new,
   side-effect-free endpoint, `POST /agents/preview-soul`, which renders
   the template against a transient (non-persisted) `Agent` instance so
   typing in the wizard never writes to the DB.

2. **Generic host-agent file provisioning.** `services/host_provisioning.py`
   stages `.plist` + `run.sh` + `agent.env` into `~/.mc/agents/<slug>/` for
   *any* host runtime agent (Hermes's dedicated bootstrap path,
   `bootstrap_hermes_agent`, stays untouched for `runtime_type == "hermes"`
   — this is the fallback for everything else). The endpoint only stages
   files; loading the `launchctl` job is gated behind
   `settings.host_agent_autoload_enabled` (default `False`) so a fresh
   install never has MC silently registering background processes on the
   operator's Mac. When disabled, the response returns the exact
   `launchctl bootstrap` command for the operator to run by hand.

3. **`_provision_agent_background` excludes host agents.** `create_agent`
   in `routers/agents.py` already skipped this background task for
   `free-code-bridge` and `manual` runtimes; `host` joins that exclusion
   list. The wizard's `ReviewStep` calls `POST /agents/{id}/provision`
   explicitly right after create, and that call is the *only* path that
   should stage host files — letting the background task race it hits the
   no-op `runtime == "host"` branch already in
   `services/provisioning.py::provision_agent_background()` and falsely
   flips `provision_status` to `"provisioned"` with nothing actually
   staged.

4. **Readiness check replaces the trigger dry-run.**
   `POST /agents/{id}/health-check` reports runtime-aware liveness signals
   (cli-bridge: helper reachability + heartbeat status; host: recent
   `last_seen_at`) instead of sending a synchronous probe message through a
   channel that no longer exists post-gateway-sunset. The wizard polls this
   endpoint on its final step until `ready: true`.

## Alternativen

- **Keep the three separate creation modals, just add a host-file-staging
  button to the existing host modal.** Verworfen: doesn't fix the
  divergent-validation problem, and a fourth flow (SOUL.md preview) would
  have needed its own modal too — the wizard's whole point is one
  step-by-step flow that adapts by runtime, not by separate screen.
- **Auto-load the launchd job immediately on provision.** Verworfen: a
  fresh MC install provisioning an unreviewed host agent should not
  silently register a background process on the operator's Mac without an
  explicit opt-in; `host_agent_autoload_enabled` keeps staging and loading
  as two distinct, separately-consentable steps.
- **Keep scheduling `_provision_agent_background` for host agents and make
  the no-op branch a true no-op (skip the `provision_status` write
  entirely).** Verworfen: still leaves a background task racing the
  wizard's explicit provision call for no benefit — the correct fix is not
  scheduling a task that has nothing useful to do for this runtime at
  create time.
- **Revive the old trigger-based dry-run for a "does it respond" check.**
  Verworfen: the underlying gateway RPC channel was permanently removed in
  Phase 29 (ADR-039); rebuilding a synchronous message channel just for a
  wizard dry-run would reintroduce the exact coupling that sunset was meant
  to remove. Liveness signals that already exist (heartbeat, helper probe,
  `last_seen_at`) are honest proxies without new infrastructure.

## Konsequenzen

### Positiv
- Single, testable creation path for every runtime — no more drift between
  three separate modals' validation and defaults.
- Live SOUL.md preview lets an operator catch a bad persona before
  creating the agent, at zero DB cost (transient render, never persisted).
- Generic host-agent staging works for any future host-side agent, not
  just Hermes — no new runtime-specific bootstrap code needed per agent.
- The host-background-provisioning race is closed: `provision_status`
  accurately reflects whether files were actually staged.
- `host_agent_autoload_enabled` keeps the "stage vs. load" decision
  explicit and reversible — an operator can always inspect the rendered
  files before running `launchctl` by hand.

### Negativ
- Two provisioning code paths for host agents now exist side by side
  (`bootstrap_hermes_agent` for `runtime_type == "hermes"`,
  `host_provisioning.stage_host_agent_files` for everything else) — a
  future refactor should fold Hermes into the generic path once its
  history-specific quirks (tmux session bootstrap) are generalized.
- The readiness check's host branch is a coarse `last_seen_at < 180s`
  heuristic, not a true health probe — a host agent whose launchd job was
  loaded but crashed on startup will still report "not ready" only after
  the heartbeat window lapses, not immediately.
- Slug derivation (path confinement, shell-metacharacter stripping) has to
  hold for arbitrary operator-supplied agent names; the hardening in
  `host_provisioning.py` (path-traversal confinement to
  `~/.mc/agents/`, no raw shell interpolation into `run.sh`, XML-safe
  ampersand escaping in the `.plist`, unknown-harness rejection) is now a
  security-relevant surface that must stay covered by
  `tests/test_host_provisioning.py` on every future change to that
  service.

## Referenzen

- Betroffene Dateien: `backend/app/routers/agents.py` (create_agent host
  exclusion, `preview-soul`, `health-check` endpoints),
  `backend/app/services/host_provisioning.py`,
  `backend/app/services/provisioning.py` (host no-op branch),
  `backend/tests/test_host_provisioning.py`,
  `backend/tests/test_agent_create_flow.py`,
  `frontend-v2/src/app/agents/wizard/` (AgentWizard.tsx + steps/*.tsx)
- Commits: `cb308935` — fix(agents): stop no-op background provisioning for
  host agents; `2533d510` — fix(wizard): show rotated token from host
  provisioning
- Verwandte ADRs: ADR-006 (Template→DB→File Single Source of Truth),
  ADR-029 (Hermes host-side tmux worker — the runtime-specific bootstrap
  this generic path complements), ADR-039 (OpenClaw Gateway Sunset — why
  the trigger-based dry-run could not be revived), ADR-048 (Host Registry)
- Security hardening covered: slug sanitization + path confinement
  (`test_slug_path_traversal_is_confined`), shell-metacharacter injection
  safety (`test_slug_strips_shell_metacharacters_no_injection`), token-hash
  ordering — the persisted `agent_token_hash` is only overwritten *after*
  file staging succeeds, so a failed stage never destroys a working token
  (`test_provision_endpoint_failed_staging_does_not_destroy_existing_token_hash`)
