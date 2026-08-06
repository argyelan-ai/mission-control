# How to: connect GitHub and use the agent git workflow

Mission Control runs fine without GitHub — agents still code and report back
through task comments. Connecting it adds version control for agent work: one
repo per project, one branch per task, automatic pull requests and
squash-merges, and per-repo rules that agents must follow.

This page gets you connected and explains the flow. The step-by-step reference,
including token scopes and a troubleshooting table, is
[docs/setup/github.md](../../setup/github.md).

## 1. Get a token

Two kinds work:

- **Classic personal access token** with the **`repo`** scope. Simplest, works
  for personal repos and for orgs you belong to.
- **Fine-grained PAT** scoped to the owner, with **Contents**, **Pull
  requests** and **Administration** set to read/write. Administration is
  required because MC creates repositories, not just pushes to existing ones.

If you are already logged in to the `gh` CLI, `gh auth token` prints one. Note
that a `gho_` token is an OAuth token from `gh auth login` and can be revoked
when that session ends — a classic (`ghp_`) or fine-grained PAT is more durable
for a long-running fleet.

You also need an **owner**: the GitHub user or organization MC creates project
repos under. Repos MC creates land there as private repos, and repos you import
must belong to that owner.

## 2. Connect — three equivalent ways

All three write to the same place. In order of precedence:

1. **Settings → GitHub** in the running app. Paste owner and token, save.
   Applies immediately, no restart. The first-run setup wizard's optional
   "Connect GitHub" step writes here too.
2. **`install.sh`** asks for both interactively during setup. The token prompt
   is silent, and both are skippable.
3. **`.env`** — set `GITHUB_OWNER` and `GH_TOKEN`, then
   `docker compose up -d backend`.

**The in-app value wins over `.env`.** Values saved in Settings live in the
encrypted vault; `.env` is the fallback for CLI-first setups. That precedence
is what makes token rotation and owner changes possible without restarting the
backend ([ADR-055](../../decisions/055-github-connection-config.md)). Clearing a
field in Settings falls back to `.env` again. The resolver caches for 30
seconds, so a direct database edit takes up to half a minute to bite; the API
paths invalidate the cache explicitly.

## 3. Verify

Open **Settings → GitHub** and press **Test connection**. It hits the live
GitHub API and reports the login the token authenticates as, whether the owner
resolves to a `User` or an `Organization`, the remaining rate limit, and — the
part worth reading when things disagree — whether the owner and token currently
come from the vault or from `.env`.

Headless equivalent:

```bash
curl -s "http://localhost/api/v1/repos/github-status?probe=true" \
  -H "Authorization: Bearer <your-admin-jwt>"
```

Drop `?probe=true` for a config-only read that makes no GitHub calls.

## 4. Register repos and write work rules

Repos are a first-class model at **`/repos`**
([ADR-050](../../decisions/050-repos-registry.md)), not just a string on a
project. Once connected, the onboarding banner there disappears and you can:

- **Import** an existing GitHub repo under your owner, or let MC create one.
- **Link** one repo to several projects — the repo is shared, not duplicated.
- **Write work rules** (`rules_md`) per repo: test commands, branch policy,
  house style, anything an agent should know before touching that codebase.
- **Sync** metadata from GitHub, or archive a repo you no longer use.

Work rules are the reason the registry exists. MC resolves the repo behind a
task's project and appends the rules to the git section of the dispatch as a
binding "repository work rules" block, so an agent working on repo X always
sees X's conventions instead of a generic default. The lookup is best-effort:
a missing or failing rules lookup never blocks a dispatch.

Two things to expect from the API: deleting a repo in MC **never** deletes it
on GitHub, and it returns `409` while projects are still linked to it. The
canonical identity of a repo is `owner/name` — MC normalizes it that way
because every `gh` call needs the owner prefix.

## 5. The branch-per-task flow

Once a project has a repo, an agent that picks up a task:

1. Clones the project's repo (or reuses its existing clone).
2. Works on `task/<slug>`, one branch per task.
3. Commits and pushes to that branch before moving the task to review.
4. Opens a pull request via the `gh` CLI when the task reaches review.
5. A reviewer — another agent or you — approves, and the PR is squash-merged
   and the branch deleted.

Ad-hoc tasks with no project share a single `mc-workspace` repo rather than
leaving orphaned branches scattered across project repos.

`gh` is MC's only GitHub binding; there is no second HTTP client. That means
anything `gh` can't do with your token, MC can't do either.

## 6. Optional: keep MC-owned repos private

`MC_OWNED_REPO_PREFIXES` in `.env` (default `mc-,mc-task-,t2-`) lists the
repo-name prefixes the visibility monitor treats as MC-owned and keeps private.
Adjust it if you use a different naming convention for repos MC creates.

## When something breaks

| Symptom | Likely cause |
|---|---|
| `401 Bad credentials` | Token expired or revoked — issue a new one and update it in Settings → GitHub or `.env` |
| `"GITHUB_OWNER is not configured"` | No owner set anywhere; set one in Settings → GitHub |
| `404`/`502` when importing a repo | Repo doesn't exist under the configured owner, or the token can't read it — run Test connection first |
| Calls start failing under load | Check the rate limit in Test connection; classic PATs share your account-wide quota |
| Private org repo inaccessible | Fine-grained PATs need org approval before touching org resources |

The full table lives in [docs/setup/github.md](../../setup/github.md).

## Where to go next

- [Get your first agent running](first-agent.md) — if you don't have an agent
  to do the coding yet.
- [Chat and voice integrations](integrations.md) — get PR-ready notifications
  and approvals onto your phone.
