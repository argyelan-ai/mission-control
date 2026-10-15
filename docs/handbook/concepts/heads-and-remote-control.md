# Heads and remote control

This is the concept behind where Mission Control is heading since
[ADR-085](../../decisions/085-head-per-job.md),
[ADR-089](../../decisions/089-lead-agent-with-hands.md) and
[ADR-090](../../decisions/090-chat-as-remote-control.md) (22.09.–12.10.2026).
If a page elsewhere in the handbook talks about a persistent fleet of named
agents with fixed roles, this page is the correction: that model is paused,
not the current direction.

## What changed

Mission Control used to run a **persistent fleet**: several agents, each with
a fixed role (developer, reviewer, tester, …), waiting for dispatched work.
Keeping that fleet alive turned out to cost more engineering time than the
work itself did. The new model has two parts:

1. **Two fixed agents, not a fleet.** A **lead agent** (your "right hand")
   and **one local omp agent** (small jobs on your own GPU box, driven over
   the ACP protocol). The lead agent's own GitHub identity and its merge and
   deploy gate are planned in
   [ADR-089](../../decisions/089-lead-agent-with-hands.md) but **not built
   yet**. In the reference setup every other previously-registered agent is
   paused — not deleted, just not dispatched new work. A fresh install does
   not do this for you: the demo crew from the first-run wizard is not
   paused.
2. **A head, when work needs its own session.** A head is one short-lived
   coding-CLI session in its own git worktree: it plans, works, checks its
   own result, writes a run record, and ends with a pull request (or a
   documented abort). There is no agent that "owns" a head after it is
   done — the next job gets a fresh one.

## The MC chat is the remote control

Away from your desk, the MC chat (web app, phone or desktop) is how you steer
the lead agent and the local omp agent. What it means in practice today —
live on phone and desktop, with the phone acceptance check still open (see
[ROADMAP](../../ROADMAP.md)):

- **Live tool output and per-edit diffs** as the agent works, with file chips
  and a running session cost.
- A **Changes panel**: everything this chat changed, with **Revert**,
  **Commit** and **Open PR** — not a merge button. The actual merge still
  respects your repo's branch protection / merge queue, the way it always
  did.
- **Question cards and plan mode**: the agent can ask before it proceeds,
  and show you a plan to approve, instead of just doing.
- **Approval cards** for anything the autonomy levels classify as risky
  (L1 do it, L2 do it and tell you, L3 wait for a tap) — this is a guardrail
  *inside MC*, independent of whatever permission mode the underlying CLI
  itself runs with.
- **Talk to it, optionally:** call the lead agent by voice, stop it by voice,
  mute, with a daily spend cap. This goes through OpenAI's Realtime API —
  your own key, billed per audio token — not a local, zero-cloud feature.

What it deliberately does **not** do: full editing from the phone.
PRINCIPLES' own rule for this is "monitor and intervene briefly, no full
editing" — the phone is for watching the diff land and tapping approve/stop,
not for writing code.

## Heads: opt-in, preview, macOS host

Heads are the part of this direction that is still a preview:

- Off by default (`HEADS_ENABLED=false`). Turning it on, and running it
  unattended (night shift), is an operator decision — not something a fresh
  install does on its own.
- The launcher that runs a head is a **macOS launchd** job on the host; there
  is no Linux/Windows path for heads yet.
- A head must run in a **sandbox** with its own, deliberately weak GitHub
  identity — the launcher refuses to start otherwise. You create that
  identity (and the ruleset that limits it) in GitHub yourself; see
  [the head launcher spec](../../specs/head-launcher.md).
- "Night shift" (queued jobs run overnight, morning report in MC) is the same
  preview flag plus `NIGHT_SHIFT_ENABLED=false` by default.
- As of this page, one head has run a job start to finish in its own
  worktree; running a head job outside this repository is still in
  progress. Heads do not stream live output into the chat the way the lead
  agent and the local omp agent do — that is explicitly parked unless a
  measured need for it shows up.

If you install Mission Control today and never touch these flags, nothing
runs unattended. You still create the lead agent and the local omp agent
yourself — the [Quickstart](../howto/quickstart.md) shows how; the first-run
wizard only offers the classic demo board.

## Where the old pages still fit

Agents, souls, runtimes, dispatch and the scope system did not disappear —
the lead agent and the local omp agent are still rows in that same system,
and a head still runs through the harness/runtime machinery described in
[Agents and souls](agents-and-souls.md) and [Runtimes](runtimes.md). What
changed is which agents receive *new* work, and that the operator's own
view into it all moved from "watch five terminals" to "one chat, from
anywhere."

## Related

- [ADR-085](../../decisions/085-head-per-job.md) — head per job, fleet into quiet mode
- [ADR-089](../../decisions/089-lead-agent-with-hands.md) — the lead agent with hands, autonomy levels
- [ADR-090](../../decisions/090-chat-as-remote-control.md) — the chat as remote control, the omp port packages
- [docs/ROADMAP.md](../../ROADMAP.md) — current package status (P1–P7), Stop 1/2
- [docs/specs/head-launcher.md](../../specs/head-launcher.md) — the head launcher in full detail
- [Quickstart](../howto/quickstart.md) — install to your first remote-controlled agent
