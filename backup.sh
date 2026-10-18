#!/bin/bash
set -euo pipefail

# ============================================================
# Mission Control — backup and restore
#
# Backs up the PostgreSQL database and the ~/.mc data directory.
#
# Usage:
#   ./backup.sh                          create a backup
#   ./backup.sh list [--full]            list backups (newest first) with a check;
#                                        --full also reads every data archive (slow)
#   ./backup.sh dir                      print the backup folder
#   ./backup.sh project                  print the docker compose project name
#                                        this run would use (debugging "service
#                                        db is not running")
#   ./backup.sh restore [file] [--yes]   restore the newest valid backup, or <file>
#                       [--no-safety-copy]
#
# Settings (environment variable, or a line in .env next to this script):
#   MC_BACKUP_DIR    where backups go. Default: $HOME/mc-backups — outside the
#                    repo (a fresh clone must not delete them) and outside ~/.mc
#                    (the data archive must not contain older backups).
#   MC_BACKUP_KEEP   how many daily backups to keep. Default: 7.
#   MC_BACKUP_MIN_FREE_MB
#                    free space that must stay on the backup disk after a
#                    backup (Postgres and Docker need room too). Default: 5120.
#   MC_INSTALL_DIR   the install folder (compose file, .env). Default: the
#                    working directory when it holds an install, else the
#                    folder of this script.
#   COMPOSE_PROJECT_NAME
#                    the docker compose project to talk to. Default: the name
#                    a running "db" container for this install already has
#                    (see compose_project_name() below), else docker compose's
#                    own default — the install folder's name. Without this, a
#                    backup run from a differently-named checkout of the repo
#                    (a worktree, a renamed clone) cannot find its containers
#                    ("service db is not running") because `docker compose`
#                    guesses the project name from the current directory.
#   MC_BACKUP_PASSPHRASE / MC_BACKUP_PASSPHRASE_FILE
#                    passphrase (or a file holding it) that encrypts .env into
#                    the backup (openssl, AES-256, PBKDF2). Without one, a
#                    backup run in a terminal asks; an unattended run skips
#                    .env and says so. Keep the passphrase outside this machine
#                    (password manager): without it the .env copy is useless.
#
# Backups written before MC_BACKUP_DIR existed live in ./backups. They are still
# listed and restorable, and count towards MC_BACKUP_KEEP: once enough newer
# backups exist, the oldest ones there are rotated away like any other (so an
# upgrade never doubles the disk use). Other files in ./backups stay.
#
# Restore order — nothing is changed before the archive has been checked:
#   1. check the archive pair (gzip integrity, pg_dump header and footer, tar)
#   2. save the current state as a "pre-restore" backup
#   3. stop backend and mc-worker
#   4. drop and recreate the database, load the dump in one transaction
#   5. extract the ~/.mc archive, put .env back (if one was saved and the
#      passphrase is given), start the services again
# ============================================================

umask 077

START_DIR="$PWD"
# The install folder. Older schedules run a backup.sh from another copy with
# the install as working directory (the old script worked relative to it), so
# a working directory that holds an install wins over the script's own folder.
if [[ -n "${MC_INSTALL_DIR:-}" ]]; then
    REPO_DIR="$(cd "$MC_INSTALL_DIR" && pwd)"
elif [[ -f "$PWD/docker-compose.yml" && -f "$PWD/backup.sh" ]]; then
    REPO_DIR="$PWD"
else
    REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
LEGACY_DIR="$REPO_DIR/backups"
DB_NAME="mission_control"
DB_USER="mc"
PRE_RESTORE_KEEP=3
STOP_SERVICES=(backend mc-worker)
ENV_FILE="$REPO_DIR/.env"
ENV_MARKER="MC-ENV-BACKUP v1"
ENC_ARGS=(-aes-256-cbc -pbkdf2 -iter 600000 -md sha256)
PASSPHRASE=""

# Read a setting: environment first, then a NAME=value line in .env.
cfg() {
    local name="$1" default="$2" value=""
    if [[ -n "${!name:-}" ]]; then
        value="${!name}"
    elif [[ -f "$REPO_DIR/.env" ]]; then
        value="$(grep -E "^${name}=" "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- || true)"
        value="${value%\"}"; value="${value#\"}"
        value="${value%\'}"; value="${value#\'}"
    fi
    [[ -z "$value" ]] && value="$default"
    [[ "$value" == "~/"* ]] && value="$HOME/${value#\~/}"
    printf '%s' "$value"
}

BACKUP_DIR="$(cfg MC_BACKUP_DIR "$HOME/mc-backups")"
KEEP_LAST="$(cfg MC_BACKUP_KEEP 7)"
MIN_FREE_MB="$(cfg MC_BACKUP_MIN_FREE_MB 5120)"
MC_DATA_DIR="$HOME/.mc"

if ! [[ "$KEEP_LAST" =~ ^[1-9][0-9]*$ ]]; then
    echo "MC_BACKUP_KEEP must be a whole number of at least 1 (got '$KEEP_LAST')." >&2
    exit 2
fi
if ! [[ "$MIN_FREE_MB" =~ ^[0-9]+$ ]]; then
    echo "MC_BACKUP_MIN_FREE_MB must be a whole number of megabytes (got '$MIN_FREE_MB')." >&2
    exit 2
fi

cd "$REPO_DIR"   # docker compose needs the repo's compose file

# The compose project name docker commands below are pinned to.
# COMPOSE_PROJECT_NAME (env or .env, same precedence as any other setting
# here) wins — same as plain `docker compose` would read it. Otherwise ask
# Docker directly: a running "db" container whose compose working_dir label
# matches this install tells us the project name compose actually used to
# start it, which is what lets a backup started from a differently-named
# checkout (a worktree, a renamed clone) still find its containers. Last
# resort: docker compose's own default when nothing is running yet — the
# install folder's name, lowercased and stripped of characters compose does
# not allow in a project name.
compose_project_name() {
    local name
    name="$(cfg COMPOSE_PROJECT_NAME "")"
    if [[ -n "$name" ]]; then
        printf '%s' "$name"
        return 0
    fi
    name="$(docker ps -a \
        --filter 'label=com.docker.compose.service=db' \
        --filter "label=com.docker.compose.project.working_dir=$REPO_DIR" \
        --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null | head -n 1)" || true
    if [[ -n "$name" ]]; then
        printf '%s' "$name"
        return 0
    fi
    name="$(basename "$REPO_DIR" | tr '[:upper:]' '[:lower:]' | tr -dc 'a-z0-9_-')"
    [[ -z "$name" ]] && name="mission-control"
    printf '%s' "$name"
}

PROJECT_NAME="$(compose_project_name)"

# Every docker compose call goes through here, pinned to $PROJECT_NAME
# instead of letting compose guess again from the current directory.
dc() { docker compose -p "$PROJECT_NAME" "$@"; }

die() { echo "ERROR: $*" >&2; exit 1; }

# Half-written files are removed on any exit, so a failed run never leaves
# something that looks like a finished backup.
PARTIALS=()
cleanup_partials() { local f; for f in "${PARTIALS[@]:-}"; do [[ -n "$f" ]] && rm -f -- "$f"; done; return 0; }
trap cleanup_partials EXIT

# Absolute path of a (possibly not yet existing) directory, without creating it.
abs_dir() {
    local d="$1"
    [[ "$d" != /* ]] && d="$REPO_DIR/$d"
    while [[ "$d" == */ && "$d" != "/" ]]; do d="${d%/}"; done
    printf '%s' "$d"
}

BACKUP_DIR="$(abs_dir "$BACKUP_DIR")"
case "$BACKUP_DIR/" in
    "$MC_DATA_DIR/"*)
        die "MC_BACKUP_DIR ($BACKUP_DIR) is inside $MC_DATA_DIR — every data archive would contain the older ones. Choose a folder outside it."
        ;;
esac

ensure_backup_dir() {
    if [[ ! -d "$BACKUP_DIR" ]]; then
        mkdir -p "$BACKUP_DIR"
        chmod 700 "$BACKUP_DIR"
    fi
}

data_archive_for() {
    local f="${1/mc_backup_/mc_data_}"
    printf '%s' "${f%.sql.gz}.tar.gz"
}

env_archive_for() {
    local f="${1/mc_backup_/mc_env_}"
    printf '%s' "${f%.sql.gz}.env.enc"
}

# Value of NAME in a .env text (last assignment wins, quotes stripped).
env_value() {
    local v
    v="$(grep -E "^$1=" <<<"$2" | tail -n 1 | cut -d= -f2- || true)"
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    printf '%s' "$v"
}

# Sets PASSPHRASE: environment, then a passphrase file, then — only in a
# terminal — a prompt. Stays empty when none is available.
get_passphrase() {
    local purpose="$1" pf again=""
    PASSPHRASE=""
    if [[ -n "${MC_BACKUP_PASSPHRASE:-}" ]]; then
        PASSPHRASE="$MC_BACKUP_PASSPHRASE"
        return 0
    fi
    pf="$(cfg MC_BACKUP_PASSPHRASE_FILE "")"
    if [[ -n "$pf" ]]; then
        [[ -r "$pf" ]] || die "MC_BACKUP_PASSPHRASE_FILE is set but cannot be read: $pf"
        IFS= read -r PASSPHRASE < "$pf" || true
        return 0
    fi
    [[ -t 0 ]] || return 0
    if [[ "$purpose" == "backup" ]]; then
        read -r -s -p "Passphrase to encrypt .env into this backup (Enter = leave .env out): " PASSPHRASE || true
        echo
        if [[ -n "$PASSPHRASE" ]]; then
            read -r -s -p "Same passphrase again: " again || true
            echo
            [[ "$again" == "$PASSPHRASE" ]] || die "the two passphrases differ — nothing written."
        fi
    else
        read -r -s -p "Passphrase of the .env in this backup (Enter = keep the current .env): " PASSPHRASE || true
        echo
    fi
}

# Prints the .env stored in an encrypted archive, or fails (wrong passphrase,
# damaged file). The passphrase goes to openssl on a file descriptor, never
# on the command line.
decrypt_env() {
    local out
    out="$(openssl enc -d "${ENC_ARGS[@]}" -pass fd:3 -in "$1" 3<<<"$PASSPHRASE" 2>/dev/null)" || return 1
    [[ "${out%%$'\n'*}" == "$ENV_MARKER" ]] || return 1
    printf '%s\n' "${out#*$'\n'}"
}

# A database archive is valid when gzip is intact and the content is a
# complete pg_dump: header at the top, completion marker at the end.
check_dump() {
    local f="$1" head tail
    [[ -s "$f" ]] || return 1
    gzip -t "$f" 2>/dev/null || return 1
    head="$( (gzip -dc "$f" 2>/dev/null || true) | head -n 20)"
    grep -q -- '-- PostgreSQL database dump' <<<"$head" || return 1
    tail="$(gzip -dc "$f" 2>/dev/null | tail -n 20)" || return 1
    grep -q -- '-- PostgreSQL database dump complete' <<<"$tail" || return 1
}

# A data archive is valid when gzip and the tar structure read to the end.
check_data() {
    local f="$1"
    [[ -s "$f" ]] || return 1
    tar -tzf "$f" >/dev/null 2>&1
}

# The folders backups are read from: the backup folder, plus ./backups when
# it is a different folder.
backup_dirs() {
    printf '%s\n' "$BACKUP_DIR"
    [[ "$LEGACY_DIR" != "$BACKUP_DIR" ]] && printf '%s\n' "$LEGACY_DIR"
    return 0
}

# Files matching <pattern> in all backup folders, newest first (by timestamp
# in the name, so a copied file with a fresh mtime does not jump the queue).
find_backups() {
    local d
    while IFS= read -r d; do
        [[ -d "$d" ]] || continue
        find -H "$d" -maxdepth 1 -name "$1" -type f 2>/dev/null
    done < <(backup_dirs) | awk -F/ '{print $NF "\t" $0}' | sort -r | cut -f2-
}

# All daily DB archives from both locations, newest first.
list_dumps() { find_backups 'mc_backup_[0-9]*.sql.gz'; }

rotate() {
    local pattern="$1" keep="$2" label="$3" old
    old="$(find_backups "$pattern" | tail -n +$((keep + 1)))"
    if [[ -n "$old" ]]; then
        while IFS= read -r f; do rm -f -- "$f"; done <<<"$old"
        echo "Removed old $label (keeping the last $keep)."
    fi
}

# Refuse to start when the backup folder cannot hold another backup of the
# size of the newest one (+10 %) and still keep MC_BACKUP_MIN_FREE_MB free.
# A full disk otherwise ends in a half archive — and stalls Postgres.
check_free_space() {
    local newest need_kb=0 free_kb data reserve_kb=$((MIN_FREE_MB * 1024))
    newest="$(list_dumps | head -n 1)"
    if [[ -n "$newest" ]]; then
        need_kb=$(du -k "$newest" | cut -f1)
        data="$(data_archive_for "$newest")"
        [[ -f "$data" ]] && need_kb=$((need_kb + $(du -k "$data" | cut -f1)))
        need_kb=$((need_kb + need_kb / 10))
    fi
    free_kb="$(df -Pk "$BACKUP_DIR" | awk 'NR==2 {print $4}')"
    if [[ -n "$free_kb" && "$free_kb" -lt $((need_kb + reserve_kb)) ]]; then
        die "not enough free space in $BACKUP_DIR: $((free_kb / 1024)) MB free, about $((need_kb / 1024)) MB needed for the backup plus $MIN_FREE_MB MB that must stay free (MC_BACKUP_MIN_FREE_MB). Free space (old backups in $BACKUP_DIR or $LEGACY_DIR) or lower MC_BACKUP_KEEP."
    fi
}

# make_backup <prefix>  — writes mc_backup_<prefix><ts>.sql.gz (+ mc_data_…)
# to temporary names, checks them, then renames. Sets LAST_BACKUP.
make_backup() {
    local prefix="$1" ts db tmp_db data tmp_data size
    ensure_backup_dir
    ts="$(date +%Y-%m-%d_%H-%M-%S)"
    db="$BACKUP_DIR/mc_backup_${prefix}${ts}.sql.gz"
    tmp_db="$db.partial"
    data="$(data_archive_for "$db")"
    tmp_data="$data.partial"
    PARTIALS+=("$tmp_db" "$tmp_data")
    check_free_space

    echo "Backing up the database..."
    if ! dc exec -T db pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$tmp_db"; then
        die "database dump failed — no backup written. Is the db container running?"
    fi
    check_dump "$tmp_db" || die "the new database dump failed its check — no backup written."
    mv -f -- "$tmp_db" "$db"
    size="$(du -h "$db" | cut -f1)"
    echo "Database backup: $db ($size)"

    # ~/.mc is the real data home (ADR-022): agent configs, vault, skills,
    # MCP servers, deliverables. Errors here are not hidden.
    if [[ -d "$MC_DATA_DIR" ]]; then
        echo "Backing up MC data ($MC_DATA_DIR)..."
        local tar_err="$tmp_data.err" rc=0
        PARTIALS+=("$tar_err")
        tar -czf "$tmp_data" -C "$HOME" \
            --exclude='*/node_modules' \
            --exclude='*/.venv' \
            --exclude='*/__pycache__' \
            --exclude='*/.git' \
            .mc/ 2>"$tar_err" || rc=$?
        if [[ -s "$tar_err" ]]; then sed 's/^/  /' "$tar_err" >&2; fi
        if [[ "$rc" != 0 ]]; then
            # Agents keep writing while the backup runs. A file that changed
            # or vanished mid-read is normal (GNU tar exits 1 for it); any
            # other message — an unreadable file, a full disk — is a failure.
            # bsdtar (macOS) says "Truncated input file" for a file that shrank.
            if [[ ! -s "$tar_err" ]] || grep -v -E 'file changed as we read it|File removed before we read it|socket ignored|Truncated input file' "$tar_err" | grep -q .; then
                die "MC data archive failed (tar errors above) — the database backup $db is fine, the ~/.mc half is missing."
            fi
            echo "  Note: some files changed while they were read (agents were working); the archive is still valid."
        fi
        check_data "$tmp_data" || die "MC data archive failed its check — the ~/.mc half is missing."
        mv -f -- "$tmp_data" "$data"
        size="$(du -h "$data" | cut -f1)"
        echo "MC data backup: $data ($size)"
    else
        echo "No $MC_DATA_DIR directory — only the database was backed up."
    fi

    # .env holds the master keys (SECRETS_ENCRYPTION_KEY, DB_PASSWORD,
    # JWT_SECRET_KEY). It is only ever stored encrypted.
    if [[ -f "$ENV_FILE" ]]; then
        local envf tmp_env
        envf="$(env_archive_for "$db")"
        tmp_env="$envf.partial"
        PARTIALS+=("$tmp_env")
        if [[ -z "$PASSPHRASE" ]]; then
            echo "NOTE: .env was not backed up — no passphrase. Without .env a restore on another"
            echo "      machine cannot read the stored API keys. Set MC_BACKUP_PASSPHRASE or"
            echo "      MC_BACKUP_PASSPHRASE_FILE, or run ./backup.sh in a terminal to be asked."
        elif ! command -v openssl >/dev/null 2>&1; then
            echo "NOTE: .env was not backed up — openssl is not installed."
        else
            { printf '%s\n' "$ENV_MARKER"; cat "$ENV_FILE"; } \
                | openssl enc -e "${ENC_ARGS[@]}" -salt -pass fd:3 -out "$tmp_env" 3<<<"$PASSPHRASE" \
                || die "encrypting .env failed."
            decrypt_env "$tmp_env" >/dev/null || die "the encrypted .env failed its check."
            mv -f -- "$tmp_env" "$envf"
            echo "Encrypted .env: $envf"
        fi
    fi
    LAST_BACKUP="$db"
}

cmd_backup() {
    [[ -f "$ENV_FILE" ]] && get_passphrase backup
    make_backup ""
    rotate 'mc_backup_[0-9]*.sql.gz' "$KEEP_LAST" "database backups"
    rotate 'mc_data_[0-9]*.tar.gz' "$KEEP_LAST" "MC data backups"
    rotate 'mc_env_[0-9]*.env.enc' "$KEEP_LAST" "encrypted .env copies"
    echo "Done."
}

cmd_list() {
    local f status data full=0
    [[ "${1:-}" == "--full" ]] && full=1
    echo "Backup folder: $BACKUP_DIR"
    [[ -d "$LEGACY_DIR" && "$LEGACY_DIR" != "$BACKUP_DIR" ]] && echo "Older folder:  $LEGACY_DIR (still read)"
    local found=0
    while IFS= read -r f; do
        [[ -n "$f" ]] || continue
        found=1
        if check_dump "$f"; then status="ok"; else status="DAMAGED"; fi
        data="$(data_archive_for "$f")"
        if [[ ! -f "$data" ]]; then
            data="(no data archive)"
        elif [[ "$full" == 1 ]]; then
            if check_data "$data"; then data="+ data $(du -h "$data" | cut -f1) ok"; else data="+ data DAMAGED"; fi
        else
            data="+ data $(du -h "$data" | cut -f1)"
        fi
        printf '%-8s %s  %s %s\n' "$status" "$f" "$(du -h "$f" | cut -f1)" "$data"
    done < <(list_dumps; find_backups 'mc_backup_pre-restore_*.sql.gz')
    [[ "$found" == 1 ]] || echo "No backups found."
}

running_services() {
    local svc running
    running="$(dc ps --services --filter status=running 2>/dev/null || true)"
    for svc in "${STOP_SERVICES[@]}"; do
        grep -qx -- "$svc" <<<"$running" && printf '%s\n' "$svc"
    done
    return 0
}

cmd_restore() {
    local file="" yes=0 safety=1 arg
    for arg in "$@"; do
        case "$arg" in
            --yes|-y) yes=1 ;;
            --no-safety-copy) safety=0 ;;
            -*) die "unknown option: $arg" ;;
            *) file="$arg" ;;
        esac
    done

    # 1. Pick and check the archive pair. Nothing has been changed yet.
    local explicit="$file"
    if [[ -n "$file" ]]; then
        [[ "$file" != /* ]] && file="$START_DIR/$file"   # relative to where you stand
        [[ -f "$file" ]] || die "no such file: $file"
        file="$(cd "$(dirname "$file")" && pwd)/$(basename "$file")"
        echo "Checking $file ..."
        check_dump "$file" || die "archive damaged or not a complete pg_dump: $file — nothing changed."
    else
        local f
        while IFS= read -r f; do
            [[ -n "$f" ]] || continue
            echo "Checking $f ..."
            if ! check_dump "$f"; then echo "  damaged, skipped."; continue; fi
            if [[ -f "$(data_archive_for "$f")" ]] && ! check_data "$(data_archive_for "$f")"; then
                echo "  its MC data archive is damaged, skipped."
                continue
            fi
            file="$f"
            break
        done < <(list_dumps)
        [[ -n "$file" ]] || die "no valid backup found in $BACKUP_DIR or $LEGACY_DIR — nothing changed."
    fi
    local data
    data="$(data_archive_for "$file")"
    if [[ -n "$explicit" && -f "$data" ]]; then
        echo "Checking $data ..."
        check_data "$data" || die "MC data archive damaged: $data — nothing changed."
    fi

    local envf new_env="" restore_env=0
    envf="$(env_archive_for "$file")"
    if [[ -f "$envf" ]]; then
        get_passphrase restore
        if [[ -n "$PASSPHRASE" ]]; then
            command -v openssl >/dev/null 2>&1 || die "this backup holds an encrypted .env, but openssl is not installed — nothing changed."
            new_env="$(decrypt_env "$envf")" \
                || die "wrong passphrase or damaged .env archive: $envf — nothing changed."
            restore_env=1
        fi
    fi

    echo
    echo "WARNING: this replaces the current database (drop + recreate)."
    echo "  Database: $file"
    if [[ -f "$data" ]]; then
        echo "  MC data:  $data  (extracted over ~/.mc)"
    else
        echo "  MC data:  no matching mc_data archive — ~/.mc is NOT restored."
    fi
    if [[ "$restore_env" == 1 ]]; then
        echo "  .env:     $envf  (the current .env is kept as .env.backup-pre-restore-<time>)"
    elif [[ -f "$envf" ]]; then
        echo "  .env:     not restored (no passphrase) — the current .env stays."
    else
        echo "  .env:     not in this backup — the current .env stays."
    fi
    if [[ "$safety" == 1 ]]; then
        echo "  The current state is saved first as a pre-restore backup in $BACKUP_DIR."
    else
        echo "  --no-safety-copy: the current state is NOT saved."
    fi
    if [[ "$yes" != 1 ]]; then
        local confirm=""
        read -r -p "Continue? (y/n): " confirm || true
        if [[ "$confirm" != "y" && "$confirm" != "j" ]]; then
            echo "Aborted. Nothing changed."
            exit 0
        fi
    fi

    # 2. Safety copy of the current state.
    local safety_file=""
    if [[ "$safety" == 1 ]]; then
        echo "Saving the current state..."
        make_backup "pre-restore_"
        safety_file="$LAST_BACKUP"
        rotate 'mc_backup_pre-restore_*.sql.gz' "$PRE_RESTORE_KEEP" "pre-restore copies"
        rotate 'mc_data_pre-restore_*.tar.gz' "$PRE_RESTORE_KEEP" "pre-restore data copies"
        rotate 'mc_env_pre-restore_*.env.enc' "$PRE_RESTORE_KEEP" "pre-restore .env copies"
    fi

    # 3. Stop everything that writes to the database.
    local stopped
    stopped="$(running_services)"
    local back_cmd="./backup.sh restore ${safety_file:-<an older backup>}"
    trap 'echo; echo "RESTORE FAILED. Services stay stopped on purpose." >&2; echo "To go back to the state before this restore: $back_cmd" >&2' ERR
    if [[ -n "$stopped" ]]; then
        echo "Stopping $(tr '\n' ' ' <<<"$stopped")..."
        # shellcheck disable=SC2086
        dc stop $stopped >/dev/null
    fi

    # 4. Recreate the database and load the dump in one transaction.
    echo "Recreating the database..."
    dc exec -T db psql -U "$DB_USER" -d postgres --quiet -v ON_ERROR_STOP=1 \
        -c "DROP DATABASE IF EXISTS $DB_NAME WITH (FORCE);" \
        -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;"
    echo "Loading the dump..."
    gzip -dc "$file" | dc exec -T db psql -U "$DB_USER" -d "$DB_NAME" \
        --quiet -v ON_ERROR_STOP=1 --single-transaction >/dev/null
    echo "Database restored from: $file"

    # 5. ~/.mc, then start again.
    if [[ -f "$data" ]]; then
        echo "Restoring MC data to ~/.mc ..."
        # Modes come from the archive, not from this script's umask 077:
        # agents and other containers read shared files in ~/.mc.
        (umask 022; tar -xpzf "$data" -C "$HOME")
        echo "MC data restored from: $data"
    fi
    local env_changed=0 current_env=""
    if [[ "$restore_env" == 1 ]]; then
        [[ -f "$ENV_FILE" ]] && current_env="$(cat "$ENV_FILE")"
        if [[ "$new_env" != "$current_env" ]]; then
            local kept old_pw new_pw
            kept="$REPO_DIR/.env.backup-pre-restore-$(date +%Y-%m-%d_%H-%M-%S)"
            if [[ -f "$ENV_FILE" ]]; then
                cp -p -- "$ENV_FILE" "$kept"
                chmod 600 "$kept"
            fi
            # Rewrite in place (same inode): containers bind-mount .env.
            printf '%s\n' "$new_env" > "$ENV_FILE"
            chmod 600 "$ENV_FILE"
            echo ".env restored from: $envf"
            [[ -f "$kept" ]] && echo "Previous .env kept in: $kept"
            # The database role keeps the password it was created with; make
            # it match the restored DB_PASSWORD so the backend can log in.
            old_pw="$(env_value DB_PASSWORD "$current_env")"
            new_pw="$(env_value DB_PASSWORD "$new_env")"
            if [[ -n "$new_pw" && "$new_pw" != "$old_pw" ]]; then
                printf "ALTER ROLE %s PASSWORD '%s';\n" "$DB_USER" "${new_pw//\'/\'\'}" \
                    | dc exec -T db psql -U "$DB_USER" -d postgres --quiet -v ON_ERROR_STOP=1 >/dev/null
                echo "Database password updated to the restored DB_PASSWORD."
            fi
            env_changed=1
        else
            echo ".env in the backup is the same as the current one — unchanged."
        fi
    fi
    trap - ERR
    if [[ "$env_changed" == 1 ]]; then
        [[ -n "$safety_file" ]] && echo "Previous state kept in: $safety_file"
        echo
        echo "The running containers still carry the old .env values."
        echo "Start everything with the restored .env:  docker compose up -d"
        exit 0
    fi
    if [[ -n "$stopped" ]]; then
        echo "Starting $(tr '\n' ' ' <<<"$stopped")..."
        # shellcheck disable=SC2086
        dc start $stopped >/dev/null
    fi
    [[ -n "$safety_file" ]] && echo "Previous state kept in: $safety_file"
    echo "Done."
}

case "${1:-}" in
    ""|backup) cmd_backup ;;
    list) shift; cmd_list "$@" ;;
    dir) printf '%s\n' "$BACKUP_DIR" ;;
    project) printf '%s\n' "$PROJECT_NAME" ;;
    restore) shift; cmd_restore "$@" ;;
    -h|--help|help) awk 'NR > 3 && /^#/ { sub(/^# ?/, ""); print; next } NR > 3 { exit }' "$0" ;;
    *) die "unknown command: $1 (use: backup, list, dir, project, restore)" ;;
esac
