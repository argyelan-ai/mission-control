# Docs — start here

This is the map of `docs/`. If you just want to install Mission Control, the
[project README](../README.md) is the front door; everything here is what
comes after `docker compose up`.

## Where the project is going

Mission Control is mid-pivot (since ADR-085, 22.09.2026): away from a
persistent fleet of named agents, towards a lead agent with hands, one local
omp agent, and the MC chat as the operator's remote control. Some of that is
live, some is an opt-in preview (macOS host), and the old fleet pages still
exist but are frozen. Read these first if anything else in `docs/` looks like
it contradicts them — they win:

- [PRINCIPLES.md](PRINCIPLES.md) — why MC exists, the build/evidence/language rules
- [ROADMAP.md](ROADMAP.md) — current stage, what is next, what is stopped
- [decisions/](decisions/) — the ADRs, [ADR-085](decisions/085-head-per-job.md) ·
  [ADR-089](decisions/089-lead-agent-with-hands.md) ·
  [ADR-090](decisions/090-chat-as-remote-control.md) are the direction
- [handbook/concepts/heads-and-remote-control.md](handbook/concepts/heads-and-remote-control.md) —
  the new concept in one page

## Get something done

- [Handbook](handbook/index.md) — the day-to-day manual: concepts, how-tos, operations
- [Quickstart: install to your first remote-controlled agent](handbook/howto/quickstart.md)
- [FAQ](handbook/faq.md) · [Troubleshooting](handbook/troubleshooting.md)
- [Setup guides](setup/) — per-integration setup (GitHub, Slack, Telegram, Windows, …)

## Technical truth

- [ARCHITECTURE.md](ARCHITECTURE.md) — the living system doc, "where do I change what"
- [decisions/](decisions/) — ADRs, one per real decision, never rewritten
- [flows/](flows/) — dispatch, task lifecycle, watchdogs, provisioning in detail
- [specs/](specs/) — specifications for work currently in progress
- [design/](design/) — frontend design rules (`ui-craft.md`)
- [produkt/](produkt/) — machine-readable input for the internal coherence tool
  (`kz`); not reading material
- [i18n.md](i18n.md) · [adapters.md](adapters.md) ·
  [agent-configuration-standard.md](agent-configuration-standard.md) ·
  [dispatch-path-parity.md](dispatch-path-parity.md) ·
  [harness-dev-ring.md](harness-dev-ring.md) — developer references

## Operating it

- [handbook/operations/](handbook/operations/) — backup/restore, updating,
  hardware, reverse proxy, platforms
- [metrics/2026-09-baseline.md](metrics/2026-09-baseline.md) — token/cost
  baseline measured for the pivot above, referenced from the roadmap
- [privacy-sweep-backlog.md](privacy-sweep-backlog.md) — the public-repo
  name-leak backlog, enforced by `scripts/privacy-scan.py` in CI
- [credential-scope-notes.md](credential-scope-notes.md) — tracked basis for a
  CI guard; do not move or delete, a test reads this file by path

## History

- [archive/](archive/) — historical, not maintained: closed investigations,
  one-off checklists. If it contradicts the sections above, the sections
  above win.
