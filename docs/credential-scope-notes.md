# Agent credential scope notes — tracked marker for the dev-ring guard

This file is the **tracked source of truth** for what the agent GitHub
credential can do, so that `backend/tests/test_harness_dev_ring.py::test_stale_exceptions_ssh_scope_claim`
derives its stale-exception check from a file that exists in every
checkout/CI run (the guard FAILS if this marker goes missing — no silent
vacuous pass).

## Finding (2026-09-15, verified on the lead machine)

- Agent credentials (GH_TOKEN injected via agent env) carry **no `workflow`
  scope**: pushing `.github/workflows/*` over HTTPS is rejected (fork probe
  identical). The workaround is rule 15: commit locally, push via the SSH
  host remote.
- Consequence recorded in the dev ring: `ssh-push` = `fehlt-bewusst` for
  every (harness, place) cell, with this file as the recheck basis.

## Recheck procedure (operator, when this claim might be stale)

Run `gh auth status` **with an agent credential** and check the token
scope list for `workflow`. If it appears: update this file, flip the
`ssh-push` declarations in `backend/tests/test_harness_dev_ring.py` from
`fehlt-bewusst` to `da` with a proof, and delete the rule-15 workaround
note if it is no longer needed.

Last verified: 2026-09-15 (hermes, review of PR #607 push path).
