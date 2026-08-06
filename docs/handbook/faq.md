# FAQ

Questions a self-hoster asks before and shortly after the first install.

### Do I need a Claude subscription?

No. An agent runs on whatever runtime you bind it to — Claude Code, a
self-hosted vLLM or LM Studio box, Ollama, or any endpoint that speaks the
OpenAI `/v1` API.

If you *do* want the Claude Code runtime, an Anthropic **Pro or Max
subscription works** — the Docker image needs a long-lived OAuth token, not a
raw API key:

```bash
claude setup-token        # opens a browser login, prints sk-ant-oat01-…
```

Paste it into **Settings → API Keys → "Claude Code OAuth Token"**; the key
must be stored under exactly `claude_code_oauth_token`. Walkthrough:
[Your first agent](howto/first-agent.md); runtime background:
[Runtimes](concepts/runtimes.md).

### Can I try it without any LLM key at all?

Yes. The core stack — boards, tasks, projects, vault, knowledge base, API —
needs no LLM. The first-run wizard's provider-key step is skippable, and it can
seed a demo board so the pipeline view has something to show. From a checkout
you can also run:

```bash
python3 scripts/demo-seed.py            # demo board: 4-agent demo crew + 8 tasks
python3 scripts/demo-seed.py --cleanup  # removes crew and board again
```

You only need credentials once you provision an agent that should actually do
work.

### Does my code leave my machine?

That depends entirely on the runtime you pick, and nothing else phones home.

| Runtime | Where your code goes |
|---|---|
| vLLM / LM Studio / Ollama on your own hardware | nowhere — fully local |
| Claude Code (Anthropic) | to Anthropic, like any Claude Code session |
| Ollama Cloud / other hosted `/v1` endpoints | to that provider |

For the fully local setup, see [Run a local LLM](howto/local-llm.md).

MC itself runs on your hardware. The outbound calls it makes on its own are
version checks: a daily one against GitHub Releases for MC itself (silent when
offline) and a CLI-tool check against npm/GitHub every 6 hours
(`settings.cli_update_check_interval`, `0` disables it). GitHub, Slack,
Discord and Telegram integrations are opt-in and off by default.

### What hardware do I need?

See [hardware requirements](operations/hardware-requirements.md). The core stack
(Postgres, Redis, Qdrant, backend, frontend, Caddy) is modest; agent
containers and — if you self-host models — GPU inference are what actually
cost resources.

### Does it run on Windows?

Yes, via **WSL2** — that is the supported Windows path, and it is
community-tested rather than CI-tested. See
[docs/setup/windows.md](../setup/windows.md) and
[Platforms](operations/platforms.md). On Windows Server or a company
hypervisor, run a small Linux VM next to your Windows VMs instead.

### Can I run it on my NAS?

If the NAS runs Docker Compose v2, yes. Prepared catalog packages:
**Runtipi** (custom app store `github.com/argyelan-ai/tipi-store`),
**Portainer** (app template in `deploy/catalogs/`), plus prepared CasaOS and
Umbrel manifests. Catalog installs give you the core stack; the host-coupled
agent fleet needs a manual install. See
[Platforms](operations/platforms.md).

### Where are my secrets stored?

- **Provider keys and tokens** live encrypted (Fernet) in the `secrets` table
  in PostgreSQL. The encryption key is `SECRETS_ENCRYPTION_KEY` in `.env` —
  lose it and the stored secrets are unreadable.
- **Agent tokens** are stored as PBKDF2 hashes, not plaintext.
- **Agent containers** fetch their credentials at startup from a
  Docker-network-only endpoint (`GET /api/v1/internal/bootstrap`) instead of
  reading plaintext files from disk.
- Some integrations (Slack) deliberately keep their tokens out of `.env`
  entirely and only in the encrypted store.

`.env` still holds the infrastructure secrets (DB password, JWT secret,
encryption key). It is gitignored — keep it that way, and back it up
separately. More on the permission model:
[Scopes & security](concepts/scopes-and-security.md).

### Do I need GitHub?

No. MC runs fine without it — you just lose version control for agent work.
Connect it whenever you like under **Settings → GitHub** (applies live, no
restart), via `install.sh`, or with `GH_TOKEN` + `GITHUB_OWNER` in `.env`.
See [The GitHub workflow](howto/github-workflow.md).

### How do I update?

```bash
./install.sh --update      # or: make update
```

Migrations run automatically when the backend starts, and they are
forward-only — take a backup first. Pin a version with `MC_IMAGE_TAG` in
`.env`. See [Updating](operations/updating.md).

### How do I back up, and what is not covered?

```bash
./backup.sh              # or: make backup
make backup-schedule     # daily 03:00 (launchd on macOS, cron on Linux)
```

Each run dumps the database and archives `~/.mc` into `./backups/`, keeping
the last 10. **Not** included: `.env` (back it up separately — without
`SECRETS_ENCRYPTION_KEY` the restored secrets are useless), the Qdrant vector
index and other Docker volumes, and `.git` directories inside agent
workspaces. Restore with `./backup.sh restore`. Full detail:
[Backup & restore](operations/backup-restore.md).

### Do I have to expose it to the internet?

No, and you shouldn't. Everything binds to `127.0.0.1` by default. For phone
or laptop access, put the host on a [Tailscale](https://tailscale.com) tailnet
and set `MC_BIND_ADDRESS=0.0.0.0` plus `PUBLIC_HOST=<your-host>.ts.net`. The
backend can control Docker containers and agents run real shells — run it only
on hosts you trust. See [Reverse proxy & remote
access](operations/reverse-proxy.md).

### Can agents merge code without a human review?

Yes, by default — that is a deliberate decision (ADR-023, "trust by default").
New boards have `require_review_before_done = false`, so a worker agent
decides itself whether a task goes through `review` or straight to `done`; its
SOUL policy says review is mandatory for code on `main`, new API/schema
changes and anything security-relevant, and "when in doubt, review". A
reviewer agent or a human squash-merges the PR.

If you want a hard gate, set `require_review_before_done = true` on that board
— the enforcement code is still there. Independently of that flag, a
**self-reflection is always required** before an agent may close a task
(`enforce_reflection`, four mandatory fields), so the learning loop survives
either setting. Approvals add human sign-off gates for risky actions. See
[The dispatch lifecycle](concepts/dispatch-lifecycle.md).

### Is it production-ready, and what license is it under?

**AGPL-3.0** ([LICENSE](../../LICENSE)): use it, self-host it and modify it
freely; if you distribute a modified version or offer it as a network service,
publish your changes under the same license. Commercial licensing beyond AGPL
is a conversation with the maintainer.

On readiness: versions are `0.x` with an explicit "expect movement" caveat,
and the changelog has already carried breaking changes. It is used daily in
its home lab, CI runs a fresh-boot end-to-end install on every push, and there
are over 5,000 tests — but treat it as a self-hosted power tool, not a hardened
multi-tenant SaaS. Do not put it on the public internet.

### Why are parts of the code and UI in German?

The project grew in a German-speaking home lab. Many ADRs
(`docs/decisions/`), inline comments and some UI strings are German; the
README, setup flow and API are English. Full i18n is on the roadmap and
contributions are welcome — see the README's language note.
