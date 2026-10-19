#!/usr/bin/env python3
"""A fake ``docker`` for the update-engine tests (scripts/tests/test_mc_update.py).

It keeps a tiny world in ``$STUB_STATE`` (JSON): images on the machine,
the MC containers with their image and health, the database schema, running
work and the drain row. Every call is appended to ``$STUB_LOG`` as one JSON
line, so the tests can assert *what* the engine asked Docker to do and in
which order. No real Docker is touched.

Knobs in the state file (set by the tests):
  health_for_tag      {"0.2.0": "unhealthy"}   health of containers on that tag
  unhealthy_inspects  N                        first N inspections say "unhealthy"
  schema_for_tag      {"0.2.0": "0002"}        schema after `up` of that tag
  in_progress         [2, 1, 0]                successive answers to the work count
  drain_table         true|false               does system_state exist
  db_down             true|false
  pull_fail           true|false
  pull_sleep          seconds                  a slow download (cancel tests)
  up_sleep_for_tag    {"0.2.0": 5}             `up` of that tag hangs this long first
  foreign             ["proj|/dir"]            other MC stacks on the machine
  project_label       "name"                   label of the running stack
  dump_fail           true|false               pg_dump fails (the backup fails)
  dump_sleep          seconds                  a slow pg_dump (cancel tests)
  schema_after_dump   "0002"                   the schema moves right after the dump
  lock_env_after_dump true|false               .env becomes unreadable after the dump
  restore_fail        true|false               loading a dump fails
  info_fail           N                        the next N `docker info` calls fail (daemon still booting)

The real backup.sh runs against this fake: ``exec -T db pg_dump`` prints a
complete-looking dump that carries the current schema, ``exec -T db psql …
-c DROP … -c CREATE …`` empties the database, and ``exec -T db psql …
--single-transaction`` reads a dump from stdin and puts its schema back.
``db_history`` records "dropped" / "restored" in order.
"""

import json
import os
import subprocess
import sys
import time

STATE = os.environ["STUB_STATE"]
LOG = os.environ["STUB_LOG"]
API = os.environ.get("STUB_API", "")
REG = "ghcr.io/argyelan-ai"
IMG = {"backend": "mc-backend", "mc-worker": "mc-backend", "frontend": "mc-frontend"}


def load():
    with open(STATE) as f:
        return json.load(f)


def save(s):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE)


def env_tag():
    if os.environ.get("MC_IMAGE_TAG"):
        return os.environ["MC_IMAGE_TAG"]
    tag = ""
    if os.path.exists(".env"):
        for line in open(".env"):
            if line.startswith("MC_IMAGE_TAG="):
                tag = line.strip().split("=", 1)[1]
    return tag


def main(argv):
    with open(LOG, "a") as f:
        f.write(json.dumps({"argv": argv, "cwd": os.getcwd(),
                            "env_tag": os.environ.get("MC_IMAGE_TAG", "")}) + "\n")
    s = load()
    if not argv:
        return 1
    cmd = argv[0]
    if cmd == "info":
        if s.get("info_fail", 0) > 0:
            s["info_fail"] -= 1
            save(s)
            print("Cannot connect to the Docker daemon", file=sys.stderr)
            return 1
        print("27.0.0")
        return 0
    if cmd == "ps":
        proj = s.get("project_label") or "mission-control"
        if s.get("containers", {}).get("backend"):
            print(f"{proj}|{s['repo']}|{REG}/mc-backend:{s['containers']['backend']['tag']}")
        for item in s.get("foreign", []):
            print(f"{item}|{REG}/mc-backend:9.9.9")
        return 0
    if cmd == "image" and argv[1] == "inspect":
        ref = argv[-1]
        if ref in s.get("images", {}):
            print(s["images"][ref])
            return 0
        print(f"Error: No such image: {ref}", file=sys.stderr)
        return 1
    if cmd == "inspect":
        svc = argv[-1].replace("cid-", "", 1)
        c = s.get("containers", {}).get(svc)
        if not c:
            return 1
        health = c["health"]
        if c["state"] != "running":
            health = c["state"]
        elif s.get("unhealthy_inspects", 0) > 0:
            s["unhealthy_inspects"] -= 1
            save(s)
            health = "unhealthy"
        print(f"{health}|{c['image']}")
        return 0
    if cmd != "compose":
        return 0
    args = argv[1:]
    if args[:1] == ["-p"]:
        args = args[2:]
    sub = args[0]
    if sub == "pull":
        if s.get("pull_sleep"):
            time.sleep(float(s["pull_sleep"]))
            s = load()
        if s.get("pull_fail"):
            print("net/http: TLS handshake timeout", file=sys.stderr)
            return 1
        tag = env_tag()
        for svc in args[1:]:
            if svc in IMG:
                s.setdefault("images", {})[f"{REG}/{IMG[svc]}:{tag}"] = f"sha256:{IMG[svc]}-{tag}"
        save(s)
        return 0
    if sub == "ps" and "--services" in args:
        # backup.sh: which of the services are running right now.
        print("db")
        for svc, c in s.get("containers", {}).items():
            if c["state"] == "running":
                print(svc)
        return 0
    if sub == "start":
        for svc in args[1:]:
            c = s.get("containers", {}).get(svc)
            if c:
                c["state"] = "running"
        save(s)
        return 0
    if sub == "ps":
        svc = args[-1]
        c = s.get("containers", {}).get(svc)
        if c and c["state"] == "running":
            print(f"cid-{svc}")
        return 0
    if sub == "stop":
        for svc in args[1:]:
            c = s.get("containers", {}).get(svc)
            if c:
                c["state"] = "exited"
        save(s)
        return 0
    if sub == "up":
        tag = env_tag()
        if s.get("up_sleep_for_tag", {}).get(tag):
            time.sleep(float(s["up_sleep_for_tag"][tag]))
            s = load()
        svcs = [a for a in args[1:] if a in IMG]
        for svc in svcs:
            ref = f"{REG}/{IMG[svc]}:{tag}"
            if ref not in s.get("images", {}):
                print(f"pull access denied / no such image {ref} (--pull never)", file=sys.stderr)
                return 1
        for svc in svcs:
            ref = f"{REG}/{IMG[svc]}:{tag}"
            s.setdefault("containers", {})[svc] = {
                "image": s["images"][ref], "tag": tag, "state": "running",
                "health": s.get("health_for_tag", {}).get(tag, "healthy")}
        if "backend" in svcs:
            s["schema"] = s.get("schema_for_tag", {}).get(tag, s.get("schema"))
            if API:
                os.makedirs(API, exist_ok=True)
                with open(os.path.join(API, "health"), "w") as f:
                    json.dump({"status": "ok", "version": f"v{tag}"}, f)
        save(s)
        return 0
    if sub == "exec":
        if s.get("db_down"):
            print("service db is not running", file=sys.stderr)
            return 1
        cmd = [a for a in args[1:] if a != "-T"][1:]   # drop "-T" and the service
        if cmd[:1] == ["pg_dump"]:
            return pg_dump(s)
        if cmd[:1] == ["psql"] and "-c" in cmd:
            # backup.sh restore: DROP DATABASE … / CREATE DATABASE …
            s["schema"] = None
            s.setdefault("db_history", []).append("dropped")
            save(s)
            return 0
        if cmd[:1] == ["psql"] and "--single-transaction" in cmd:
            dump = sys.stdin.read()
            if s.get("restore_fail"):
                print("ERROR: relation already exists", file=sys.stderr)
                return 3
            schema = None
            for line in dump.splitlines():
                if line.startswith("-- schema: "):
                    schema = line.split(": ", 1)[1].strip() or None
            s["schema"] = schema
            s.setdefault("db_history", []).append("restored")
            save(s)
            return 0
        sql = args[-1]
        if sql == "select 1":
            print("1")
        elif "alembic_version" in sql:
            print(s.get("schema") or "")
        elif "in_progress" in sql:
            seq = s.get("in_progress", [0])
            print(seq[0])
            if len(seq) > 1:
                s["in_progress"] = seq[1:]
                save(s)
        elif "to_regclass" in sql:
            print("t" if s.get("drain_table", True) else "f")
        elif sql.startswith("insert into system_state"):
            start = sql.index("'{") + 1
            end = sql.index("}'") + 1
            s["drain"] = json.loads(sql[start:end])
            s.setdefault("drain_history", []).append("set")
            save(s)
        elif sql.startswith("delete from system_state"):
            if "returning" in sql and s.get("drain"):
                print(json.dumps(s["drain"]))
                print("DELETE 1")
            s["drain"] = None
            s.setdefault("drain_history", []).append("cleared")
            save(s)
        return 0
    return 0


def pg_dump(s):
    log = os.environ.get("STUB_BACKUP_LOG")
    if log:
        tag = subprocess.run(["git", "describe", "--tags", "--exact-match", "HEAD"],
                             capture_output=True, text=True).stdout.strip() or "none"
        with open(log, "a") as f:
            f.write(f"backup on {tag}\n")
    if s.get("dump_sleep"):
        time.sleep(float(s["dump_sleep"]))
        s = load()
    if s.get("dump_fail"):
        print("pg_dump: error: connection to server failed", file=sys.stderr)
        return 1
    print("--\n-- PostgreSQL database dump\n--")
    print(f"-- schema: {s.get('schema') or ''}")
    print("--\n-- PostgreSQL database dump complete\n--")
    if s.get("schema_after_dump"):
        s["schema"] = s["schema_after_dump"]
        save(s)
    if s.get("lock_env_after_dump"):
        os.chmod(os.path.join(s["repo"], ".env"), 0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
