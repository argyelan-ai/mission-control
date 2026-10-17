# ADR-093 — Everything the user may change is changeable in MC, and built to be extended

**Status:** Accepted
**Datum:** 2026-10-17
**Scope:** Architecture · Frontend/Settings · Backend/Config · Install

## Kontext

A fresh-install test on a Raspberry Pi and the onboarding review (October
2026) showed the same pattern several times: a part of MC works, but the
user cannot reach or change it from MC itself.

- The Claude Code token is pasted once under a secret name the user has to
  know; replacing it later means finding that name again, and nothing tests
  it on save.
- Harness images (omp) are built by hand on the command line; a new harness
  would need the same manual steps.
- Providers, runtimes and keys are spread over several pages (API keys,
  runtimes, agent forms) instead of one place where everything that belongs
  together lives together.
- The credentials form marks a field optional that the backend requires.

The operator's rule (2026-10-17): every feature and component a user can or
should adjust later is part of the architecture decision from the start —
not an afterthought — and MC is built so new providers and harnesses attach
without a rebuild.

## Entscheidung

1. **Changeable in MC, not only in files.** Every value or component a user
   may need to change after setup — tokens and keys, providers and their
   endpoints, harnesses, model runtimes, devices, voice and language, budgets,
   notification channels — has a place in the MC UI where it can be **viewed,
   tested, replaced and removed**. Editing `.env` or running a script is an
   escape hatch for operators, never the only way.
2. **Test on save.** Saving a credential or endpoint runs a cheap real check
   (a test call, a version probe) and shows the result with its age. A value
   that was never tested says so.
3. **Install from MC where the platform allows it.** Components that run in
   containers (harness images, model recipes) can be added from MC with a
   progress view. Host-level installs (Docker itself, system packages) stay in
   the installer, offered with an explicit yes — the browser never installs on
   the host.
4. **Registries, not special cases.** Providers, harnesses and recipes are
   entries in a registry (catalog + compatibility table + icon + test
   procedure + install procedure). Adding one is a new entry and an adapter,
   never a new page or a new branch in shared code. This extends
   PRINCIPLES §3.4 (the harness layer is the one place for harness details).
5. **What belongs together lives together.** One page per concept with
   consistent icons: all model providers (cloud and local) on one page, all
   harnesses on one page, all devices on one page. Settings link to these
   pages instead of duplicating them.
6. **Every ADR and PR answers "how does the user change this later?"** — the
   ADR template and the reviewer questions carry it.

## Alternativen

- **Keep configuration in `.env` and docs, add UI only on demand** →
  rejected: it is exactly what made the token, the omp image and the
  credentials form hard to use; each case was found by a stranger failing.
- **A generic key/value settings editor for everything** → rejected: it
  shows values but cannot test, explain or install anything, and it scatters
  related things again.
- **A plugin system with third-party code loaded into MC** → rejected for
  now: too much attack surface and maintenance (PRINCIPLES §3.5). Registry
  entries plus adapters in the repo cover the need.

## Konsequenzen

### Positiv
- A user can fix, rotate and extend their setup without a terminal — the
  phone-remote promise holds for configuration, too.
- New providers and harnesses become small, reviewable additions.
- Reviews have one more concrete question that catches missing "change
  later" paths before they ship.

### Negativ
- More UI per feature (view/test/replace/remove), so features cost a bit
  more up front.
- Existing scattered pages (API keys, runtimes, agent forms) need a
  consolidation pass; until then the new rule applies to new work and to
  every page touched.
- Test-on-save needs a test procedure per provider/harness; where none
  exists, the UI must say "not tested" instead of pretending.

## Referenzen

- PRINCIPLES §3 rule 11 and §7 rule 13 (added with this ADR)
- Onboarding proposal and Pi fresh-install test, 2026-10-17 (internal)
- Related: ADR-033 (secrets vs credentials), ADR-086 (harness × runtime),
  ADR-077 (recipe model), ADR-092 (release channels)
- Known gaps at the time of writing: credentials form vs backend (issue #823),
  no "reconnect Claude" flow, omp image built by hand
