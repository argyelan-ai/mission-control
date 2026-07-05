# Connecting GitHub

MC's agent git workflow — one repo per project, one branch per task,
automatic PRs and squash-merges — needs a GitHub **owner** (user or org) and
a **token**. This is entirely optional: MC runs fine without it, you just
lose version control for task work (no repo/branch/PR flow, agents can
still code and report back via comments).

## 1. Get a token

Two options:

- **Classic personal access token** with the **`repo`** scope. Simplest —
  works for both personal repos and orgs you belong to.
- **Fine-grained PAT**, scoped to the owner (your user or the org), with
  **Contents**, **Pull requests**, and **Administration** permissions set
  to read/write. Administration is needed because MC creates new
  repositories, not just pushes to existing ones.

Quickest way to get a token if you're already logged in to the `gh` CLI
locally:

```bash
gh auth token
```

Note that a token starting with `gho_` is a GitHub **OAuth** token (e.g.
from `gh auth login`) — these can expire or get revoked when the
authorizing session ends. A classic (`ghp_`) or fine-grained PAT with an
explicit (or no) expiry is more durable for a long-running agent fleet.

## 2. Pick an owner

The owner is whichever GitHub **user or org** MC should create project
repos under (`https://github.com/<owner>`). Any repos MC creates land
there as private repos; existing repos you import must also belong to this
owner.

## 3. Connect

Pick whichever fits your workflow — they all write to the same place and
take effect immediately:

- **Settings → GitHub** (in the running app) — paste owner + token, save.
  Takes effect immediately, no restart. This is also where the first-run
  setup wizard's optional "Connect GitHub" step writes to.
- **`install.sh`** — asks for both interactively during setup. Skip either
  by leaving it empty; the token prompt is silent (no terminal echo) and
  optional even if you set an owner.
- **`.env`** — set `GITHUB_OWNER` and `GH_TOKEN` directly and
  `docker compose up -d backend`.

If both an in-app value and `.env` are set, **the in-app (vault) value
wins** — that's what makes token rotation and owner changes possible
without a restart. Clearing a field in Settings → GitHub falls back to
whatever is in `.env`, if anything.

## 4. Verify

Open **Settings → GitHub** and use **Test connection**. It calls the
GitHub API live and reports:

- **login** — the account the token authenticates as
- **owner type** — whether the configured owner resolves to a `User` or an
  `Organization`
- **rate limit** — remaining/total API calls, so you can spot a
  soon-to-be-throttled token before it blocks agents mid-task
- **owner/token source** — whether each value currently comes from the
  vault (Settings) or from `.env`, useful when the two disagree

The same data is available without the UI at
`GET /api/v1/repos/github-status?probe=true` (add `?probe=true` for the
live check; omit it for a config-only read).

## 5. What happens next

Once connected:

- **`/repos`** stops showing its onboarding banner. Import existing repos
  or create new ones from there, and write per-repo work rules (test
  commands, branch policy, house style) that get injected into every
  dispatch for that codebase.
- New projects get their own private repo; ad-hoc tasks (no project) share
  a single `mc-workspace` repo instead.
- Agents clone/branch/push/PR automatically as they pick up tasks; a
  reviewer (agent or human) merges via squash on approval.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `401 Bad credentials` on repo operations | Token expired or revoked — generate a new one (step 1) and update it in Settings → GitHub or `.env` |
| `"GITHUB_OWNER is not configured"` | No owner set anywhere (vault or `.env`) — set one in Settings → GitHub |
| `404`/`502` importing a repo | Connection not verified yet, repo doesn't exist under the configured owner, or the token can't read it — run Test connection first |
| Rate limit low / calls start failing | Check **rate limit** in Test connection; classic PATs share your account-wide GitHub API quota |
| Can't access a private org repo | Fine-grained PATs need **org approval** before they can touch org resources — check the org's PAT policy, or use a classic PAT if you're an org member with access |
