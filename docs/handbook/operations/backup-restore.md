# Backup & restore

Mission Control ships one backup script, `backup.sh`. It saves the things
that hold your state: the PostgreSQL database, the `~/.mc` data directory and
— encrypted, if you give it a passphrase — the `.env` file with the master
keys. It is a plain Bash script on purpose: it works when the app does not.

Read [what a backup does NOT contain](#what-a-backup-does-not-contain) before
you rely on it.

## Run one now

```bash
./backup.sh          # or: make backup
./backup.sh list     # what is there, newest first, each one checked
```

Each run writes up to **three** files into the backup folder:

| File | Contains |
|---|---|
| `mc_backup_<time>.sql.gz` | Full dump of the `mission_control` database (`pg_dump`, gzip) |
| `mc_data_<time>.tar.gz` | The `~/.mc` directory, without `node_modules`, `.venv`, `__pycache__`, `.git` |
| `mc_env_<time>.env.enc` | `.env`, encrypted with your passphrase (only when one is given) |

Every file is checked before it gets its final name: the dump must be a
complete `pg_dump` (header and completion marker), the archive must read to
the end. A run that fails leaves no file that looks like a finished backup,
and it ends with an error instead of "Done". Files are readable by you only
(`600`, folder `700`) — they contain agent tokens.

If the disk cannot hold another backup of the size of the last one **and**
still keep `MC_BACKUP_MIN_FREE_MB` free afterwards, the run stops before it
starts writing — also on the very first run, when there is no earlier backup
to measure. A full disk would stall Postgres and Docker, not only the backup.

### Where backups go, and how many

| Setting | Default | Meaning |
|---|---|---|
| `MC_BACKUP_DIR` | `$HOME/mc-backups` | The backup folder. Outside the repo (a fresh clone must not delete your backups) and outside `~/.mc` (the data archive would contain the older backups — the script refuses such a folder). |
| `MC_BACKUP_KEEP` | `7` | How many daily backups are kept. Older ones are deleted after a successful run. |
| `MC_BACKUP_MIN_FREE_MB` | `5120` | Free space (MB) that must stay on the backup disk after a backup. |
| `MC_INSTALL_DIR` | see below | The install folder (`docker-compose.yml`, `.env`). Rarely needed. |

Set them in `.env` or in the environment. `./backup.sh dir` prints the folder
in use. Each daily backup is roughly *database dump + compressed `~/.mc`* —
agent workspaces make `~/.mc` the big part. Check `./backup.sh list` once and
multiply by `MC_BACKUP_KEEP` before you pick a disk.

**Installed before this change?** Your older backups stay in `./backups` in
the repo. `list` and `restore` still read them. New backups go to
`MC_BACKUP_DIR`, and the old daily backups **count towards `MC_BACKUP_KEEP`**:
after each successful run, the oldest daily backups across both folders go
first, so the upgrade never doubles the disk use. Other files in `./backups`
(a log, `pre-restore` copies) are left alone. If you had a schedule, run
`make backup-schedule` once more so the log moves to the new folder too. Any
job of your own that copies backups elsewhere should read the folder from
`./backup.sh dir` instead of a fixed path.

If your disk is already tight, free space **before** the first new run (move
older `./backups` files elsewhere): the free-space check counts the reserve on
top of the size of the last backup.

**Which install is used.** The script works on the install in the working
directory when that folder holds `docker-compose.yml` and `backup.sh` (the
old behaviour — a schedule that runs a `backup.sh` from another copy keeps
working), otherwise on the folder the script lives in. `MC_INSTALL_DIR`
overrides both.

### The `.env` and your passphrase

`.env` holds `SECRETS_ENCRYPTION_KEY`, `DB_PASSWORD` and `JWT_SECRET_KEY`.
Without it, a restore on another machine cannot read the stored API keys and
tokens. It is never stored in plain text — only encrypted with a passphrase
you choose:

| Where the passphrase comes from | When |
|---|---|
| `MC_BACKUP_PASSPHRASE` (environment) | first choice |
| `MC_BACKUP_PASSPHRASE_FILE` (environment or `.env`) — the first line of that file | for the daily schedule; make the file `chmod 600` |
| a prompt (asked twice) | when you run `./backup.sh` in a terminal; Enter = leave `.env` out |

With no passphrase the backup still runs, leaves `.env` out and says so.

**Keep the passphrase outside this machine** (a password manager). If the
machine is lost, the passphrase file is lost with it — and without the
passphrase the `.env` copy cannot be opened by anyone, including you.

The format is standard `openssl enc` (AES-256-CBC, PBKDF2 with 600 000
rounds, SHA-256). `openssl` ships with macOS, Linux distributions and
Raspberry Pi OS; nothing extra to install. To open a copy by hand:

```bash
openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -md sha256 -in mc_env_<time>.env.enc
```

The first line of the output is `MC-ENV-BACKUP v1`; the rest is the `.env`.

## What a backup does NOT contain

| Not covered | Why it matters | What to do |
|---|---|---|
| **`.env` without a passphrase** | See above — the stored secrets cannot be decrypted without `SECRETS_ENCRYPTION_KEY`. | Give a passphrase, or keep `.env` in a password manager yourself. |
| **Unpushed agent work** | `.git` folders inside `~/.mc` are left out. Agent workspaces come back as plain files: commits that were never pushed, branches and history are gone. | Treat GitHub (or your remote) as the home of agent work; push before a restore or a move. |
| **Qdrant** (`mc_qdrant_data` volume) | The vector index for semantic search is not in the dump. | Treat the index as lost after a restore or a move, or snapshot the volume yourself. |
| **Redis, Caddy, shared deliverables volumes** (`mc_redis_data`, `caddy_data`, `caddy_config`, `mc_shared_deliverables`) | Cache, queues, issued certificates. The database itself *is* covered via `pg_dump`; the `mc_postgres_data` volume is not copied as such. | Usually nothing; Caddy fetches certificates again. |
| **Files in the repo checkout** — `docker/docker-compose.agents.yml` (your agent fleet), `docker-compose.override.yml`, `caddy/Caddyfile.local`, `caddy/certs/` | Host-specific config lives next to the code, not in `~/.mc`. | Copy these by hand when you move (see below). |
| **Docker images** | Pulled again on the next start. | Nothing to do. |

## Schedule it (daily 03:00)

```bash
make backup-schedule                    # → ./scripts/schedule-backup.sh
./scripts/schedule-backup.sh --remove   # uninstall
```

| OS | Mechanism | Where |
|---|---|---|
| macOS | launchd agent, 03:00 | `~/Library/LaunchAgents/com.mc.backup.plist` |
| Linux | crontab line `0 3 * * *`, tagged `# mission-control-backup` | your user crontab |
| anything else | unsupported — the script exits 1 | schedule `backup.sh` yourself |

The log is `backup.log` in the backup folder. A scheduled run has no
terminal, so it cannot ask for a passphrase: set `MC_BACKUP_PASSPHRASE_FILE`
in `.env` if the schedule should include `.env`.

```bash
launchctl list | grep com.mc.backup                # macOS
crontab -l | grep mission-control-backup           # Linux
tail -20 "$(./backup.sh dir)/backup.log"           # did the last run succeed?
./backup.sh list --full                            # also reads every data archive (slow)
```

## Restore

```bash
./backup.sh restore                         # newest backup that passes the check
./backup.sh restore ~/mc-backups/mc_backup_2026-07-30_03-00-01.sql.gz
./backup.sh restore --yes                   # no question (for scripts)
```

What it does, in this order — **your data is not touched before step 4**:

1. **Check.** Picks the newest backup whose dump *and* data archive pass the
   check (damaged ones are skipped and named), or checks the file you gave
   (damaged → it stops: "nothing changed"). If the backup has an encrypted
   `.env` and you give the passphrase, it is decrypted now — a wrong
   passphrase stops here too.
2. **Ask.** Shows what will be restored and asks once (`--yes` skips this).
3. **Safety copy.** Saves the current state as
   `mc_backup_pre-restore_<time>` (+ data, + `.env` if a passphrase is given)
   in the backup folder. The last 3 are kept; the daily rotation never
   touches them. Skip only when this copy itself cannot be made:
   `--no-safety-copy`.
4. **Stop** `backend` and `mc-worker`, so nothing writes during the restore.
5. **Database:** drop, create, load the dump in **one transaction** — no
   half-loaded database. If loading fails, the database stays empty and the
   script points you to the safety copy.
6. **`~/.mc`:** extracts the data archive over `$HOME`. It overwrites files
   from the archive; files created since the backup stay (not a clean slate).
   File modes come from the archive, so files other containers read stay
   readable for them.
7. **`.env`** (only with the passphrase): the current one is kept as
   `.env.backup-pre-restore-<time>`, the restored one is written in place,
   and the database password is set to the restored `DB_PASSWORD`.
8. **Start** the services again. If `.env` was replaced, the script does not
   start them itself; it prints `docker compose up -d`, because running
   containers still carry the old values.

If a step after 3 fails, the script stops with the services stopped on
purpose and prints the command that brings back the state from before:
`./backup.sh restore <pre-restore file>`.

Notes:

- The `db` container must be running — the script uses `docker compose exec db`.
- Schema version: the restored database is at the dump's migration state. A
  newer backend upgrades it on start; migrations are forward-only, so take a
  backup *before* every update ([Updating](updating.md)).

## Move to another machine

1. **Old machine:** push agent work that matters (unpushed commits are not in
   the backup). Then run a backup with a passphrase:
   `MC_BACKUP_PASSPHRASE='…' ./backup.sh` — or type it when asked.
2. Check it: `./backup.sh list --full` — the newest line must say `ok` twice.
3. Copy the backup folder (all three files of that time) to the new machine,
   plus the repo files the backup does not hold: `docker/docker-compose.agents.yml`
   and, if you have them, `docker-compose.override.yml`, `caddy/Caddyfile.local`,
   `caddy/certs/`.
4. **New machine:** install Mission Control with the one-liner from the
   [README](../../../README.md#install) — at the **same version** as the old
   machine or newer (`… | bash -s -- --version vX.Y.Z` pins it; the old
   version is `MC_IMAGE_TAG` in its `.env`). Wait until it is up.
5. Put the copied repo files from step 3 back into the new checkout.
6. Restore: `./backup.sh restore /path/to/mc_backup_<time>.sql.gz` and give
   the passphrase. The `.env` of the fresh install is kept as
   `.env.backup-pre-restore-<time>`.
7. Check host-specific values in the restored `.env` (addresses, ports,
   `MC_BACKUP_DIR`), then `docker compose up -d`.
8. Log in with your **old** account. Check that an agent answers and that
   **Settings → API Keys** shows your keys.
9. Agent workspaces: clone again from your remote what was not pushed before.

Without the `.env` (no passphrase): the restore works, but stored secrets
cannot be decrypted — enter provider keys again under **Settings → API Keys**.

## Uninstall

Take a last backup first if you may want the data again. Then, in the repo
folder:

1. `./scripts/schedule-backup.sh --remove` — the daily backup.
2. `make bridge-uninstall` — the host helper service (`cli-bridge`, launchd
   on macOS / systemd user unit on Linux), if you installed it.
3. Stop agent containers: `docker ps --filter name=mc-agent- -q | xargs -r docker rm -f`.
4. `docker compose down -v` — stops the stack and **deletes its volumes**
   (database, Qdrant, Redis, Caddy). Without `-v` the data stays on disk.
5. `rm -rf ~/.mc` — agent configs, vault, skills, workspaces.
6. Delete the repo folder, and the backup folder (`./backup.sh dir` shows it)
   once you are sure you do not need it.
7. Optional: remove the images — `docker image ls | grep mc-` and
   `docker image rm …`.
