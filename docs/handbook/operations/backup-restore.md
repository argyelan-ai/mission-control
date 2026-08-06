# Backup & restore

Mission Control ships one backup script, `backup.sh`. It captures the two
things that actually hold your state: the PostgreSQL database and the `~/.mc`
data directory. Everything else is either reproducible from the repo or has to
be backed up by you — the list is below, read it before you rely on this.

## Run one now

```bash
./backup.sh          # or: make backup
```

Each run writes **two** files into `./backups/`:

| File | Contains | How it is made |
|---|---|---|
| `mc_backup_<timestamp>.sql.gz` | Full dump of the `mission_control` database | `docker compose exec -T db pg_dump -U mc mission_control \| gzip` |
| `mc_data_<timestamp>.tar.gz` | The `~/.mc` directory | `tar -czf … .mc/`, excluding `node_modules`, `.venv`, `__pycache__`, `.git` |

Both kinds are pruned independently to the **last 10** runs (`KEEP_LAST` in
`backup.sh`). The `mc_data` archive is only written if `~/.mc` exists.

`~/.mc` is the real data home (ADR-022) and holds:

```
~/.mc/
├── agents/<slug>/       # SOUL.md, agent.env, claude-config/
├── workspaces/<slug>/   # agent working copies, project clones, worktrees
├── deliverables/<slug>/ # task output files
├── vault/               # the Markdown knowledge vault
├── skills/ plugins/ mcp-servers/
└── logs/
```

## What a backup does NOT contain

| Not covered | Why it matters | What to do |
|---|---|---|
| **`.env`** | Holds `SECRETS_ENCRYPTION_KEY`, `DB_PASSWORD`, `JWT_SECRET_KEY`. Without the encryption key the restored `secrets` table (LLM provider keys, tokens) cannot be decrypted. | Copy `.env` to a password manager / encrypted store. Never into git. |
| **Docker named volumes** — `mc_qdrant_data`, `mc_redis_data`, `mc_shared_deliverables`, `caddy_data`, `caddy_config` | The Qdrant vector index and Redis cache are not in the dump. Postgres content *is* captured (via `pg_dump`), but the `mc_postgres_data` volume itself is not. | Accept the loss (index/cache) or snapshot the volumes separately. |
| **`.git` directories inside `~/.mc`** | `tar` excludes `*/.git`, so agent workspace clones come back as plain files without history. | Treat GitHub (or your remote) as the source of truth for agent work. |
| **The repo checkout** — including `docker-compose.override.yml`, `caddy/Caddyfile.local`, `caddy/certs/` | Host-specific config and certificates live here, outside `~/.mc`. | Back these up with your host config; the code itself comes from git. |
| **Docker images** | Rebuilt or pulled on the next `docker compose up`. | Nothing to do. |

Verify what you actually got before you trust it:

```bash
gunzip -c backups/mc_backup_<timestamp>.sql.gz | head -5   # should be a pg_dump header
tar -tzf backups/mc_data_<timestamp>.tar.gz | head          # should list .mc/…
```

## Schedule it (daily 03:00)

```bash
make backup-schedule            # → ./scripts/schedule-backup.sh
./scripts/schedule-backup.sh --remove   # uninstall
```

The script is idempotent and picks the mechanism per OS:

| OS | Mechanism | Where |
|---|---|---|
| macOS | launchd agent, `StartCalendarInterval` 03:00, `RunAtLoad=false` | `~/Library/LaunchAgents/com.mc.backup.plist` |
| Linux | crontab line `0 3 * * *`, tagged `# mission-control-backup` | your user crontab |
| anything else | unsupported — the script exits 1 | schedule `backup.sh` yourself |

Output goes to `./backups/backup.log` (stdout+stderr).

The launchd plist pins `PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`,
because launchd does not inherit a login shell PATH — without it, the
`docker compose` call inside `backup.sh` would fail.

Check that it is installed:

```bash
launchctl list | grep com.mc.backup        # macOS
crontab -l | grep mission-control-backup   # Linux
tail -20 backups/backup.log                # did the last run succeed?
```

## Restore

```bash
./backup.sh restore                                  # newest pair
./backup.sh restore backups/mc_backup_2026-07-30_03-00-01.sql.gz
```

This is destructive and asks for confirmation first. What it does, in order:

1. Picks the newest `mc_backup_*.sql.gz` (or the file you passed) and derives
   the matching `mc_data_*.tar.gz` from the same timestamp.
2. Prints both paths — if no matching data archive exists it says so
   explicitly and restores the database only.
3. `docker compose stop backend` (so nothing writes during the restore).
4. `DROP DATABASE … WITH (FORCE)` + `CREATE DATABASE` — importing a dump into
   a non-empty database errors on every existing table.
5. Pipes the dump into `psql`.
6. Extracts the `mc_data` archive over `$HOME`.
7. `docker compose start backend`.

Notes before you run it:

- The `db` container must be running — the script uses `docker compose exec db`.
- The `~/.mc` extraction **overwrites** files from the archive but does not
  delete files created since the backup. It is not a clean-slate restore.
- Restoring onto a machine with a *different* `.env` leaves the encrypted
  secrets undecryptable. Restore `.env` (or at least
  `SECRETS_ENCRYPTION_KEY`) alongside it, or re-enter provider keys under
  **Settings → API Keys**.
- Schema version: the restored database is at the dump's migration state. A
  newer backend runs `alembic upgrade head` automatically on start — that is
  forward-only, so always take a backup *before* an update
  ([Updating](updating.md)).
