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
  foreign             ["proj|/dir"]            other MC stacks on the machine
  project_label       "name"                   label of the running stack
"""

import json
import os
import sys

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
        if s.get("pull_fail"):
            print("net/http: TLS handshake timeout", file=sys.stderr)
            return 1
        tag = env_tag()
        for svc in args[1:]:
            if svc in IMG:
                s.setdefault("images", {})[f"{REG}/{IMG[svc]}:{tag}"] = f"sha256:{IMG[svc]}-{tag}"
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
        sql = args[-1]
        if "alembic_version" in sql:
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
            s["drain"] = None
            s.setdefault("drain_history", []).append("cleared")
            save(s)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
