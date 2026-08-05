#!/usr/bin/env python3
"""Demo seed — populate a fresh Mission Control with a showcase board.

Five minutes to wow: after `docker compose up`, run

    python3 scripts/demo-seed.py            # create demo board + tasks
    python3 scripts/demo-seed.py --cleanup  # remove it again

Reads LOCAL_AUTH_TOKEN from ./.env (written by setup.sh), talks to the
backend on localhost:8000. Stdlib only — no dependencies.

The board shows the full task lifecycle (inbox → in_progress → review →
done, plus a blocked lane) so the pipeline view has something to say
before the first real agent is provisioned. The seed also registers a
small demo crew (registry entries only — nothing is provisioned, no
provider keys needed) and assigns the active tasks to them, so boards,
cards and the agent registry look alive from the first minute.
Provisioning real agents still needs provider keys; see
docs/setup/first-agent.md.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("MC_API_URL", "http://localhost:8000").rstrip("/")
SLUG = "demo-product-launch"

DEMO_AGENTS = [
    # (name, emoji, role, is_board_lead)
    ("Atlas", "🧭", "Board lead — plans phases, dispatches subtasks to the crew",
     True),
    ("Nova", "⚡", "Builder — implements tasks on their own branches", False),
    ("Bolt", "🔧", "Builder — infrastructure and performance work", False),
    ("Vega", "🔍", "Reviewer — gates every merge before it lands", False),
]

DEMO_TASKS = [
    # (title, status, priority, description, assignee — None = still in inbox)
    ("Draft launch announcement blog post", "done", "high",
     "Write the v1.0 announcement: what it does, who it's for, quickstart.",
     "Nova"),
    ("Set up staging environment", "done", "medium",
     "Compose stack on the staging host, TLS via Caddy, smoke tests green.",
     "Bolt"),
    ("Landing page hero section", "review", "high",
     "Hero copy + screenshot carousel. Awaiting review before merge.",
     "Nova"),
    ("Load-test the API gateway", "in_progress", "high",
     "k6 scenario: 200 RPS sustained, p95 < 250ms. Report as deliverable.",
     "Bolt"),
    ("Write onboarding e-mail sequence", "in_progress", "medium",
     "3-mail drip: welcome, first agent, power features.",
     "Atlas"),
    ("Legal review of the license FAQ", "blocked", "medium",
     "Waiting on external counsel — unblock when the draft comes back.",
     "Vega"),
    ("Social media launch thread", "inbox", "medium",
     "Thread with GIFs of the pipeline view; schedule for launch morning.",
     None),
    ("Post-launch retro board", "inbox", "low",
     "Collect metrics + lessons in week 1 after launch.",
     None),
]


def _token() -> str:
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    try:
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("LOCAL_AUTH_TOKEN="):
                    tok = line.split("=", 1)[1].strip()
                    if tok:
                        return tok
    except OSError:
        pass
    tok = os.environ.get("LOCAL_AUTH_TOKEN", "")
    if not tok:
        sys.exit("LOCAL_AUTH_TOKEN not found — run ./setup.sh first (writes .env).")
    return tok


def _call(method: str, path: str, body: dict | None = None,
          tolerate_http_errors: bool = False) -> dict | list | None:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if tolerate_http_errors:
            print(f"  warning: {method} {path} failed ({e.code}): {detail}")
            return None
        sys.exit(f"{method} {path} failed ({e.code}): {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"Backend not reachable at {API} ({e.reason}) — is the stack up?")


def _find_demo_board() -> dict | None:
    boards = _call("GET", "/api/v1/boards") or []
    for b in boards:
        if b.get("slug") == SLUG:
            return b
    return None


def _demo_agents_on(board_id: str) -> list[dict]:
    agents = _call("GET", "/api/v1/agents") or []
    demo_names = {name for name, _, _, _ in DEMO_AGENTS}
    return [a for a in agents
            if a.get("board_id") == board_id and a.get("name") in demo_names]


def cleanup() -> None:
    board = _find_demo_board()
    if not board:
        print("No demo board found — nothing to clean up.")
        return
    # Demo agents first (archive → delete, the two-stage lifecycle), so the
    # board delete never trips over agent FKs.
    for agent in _demo_agents_on(board["id"]):
        _call("POST", f"/api/v1/agents/{agent['id']}/archive",
              tolerate_http_errors=True)
        _call("DELETE", f"/api/v1/agents/{agent['id']}",
              tolerate_http_errors=True)
        print(f"Demo agent '{agent['name']}' removed.")
    _call("DELETE", f"/api/v1/boards/{board['id']}")
    print(f"Demo board '{board['name']}' deleted.")


def seed() -> None:
    if _find_demo_board():
        sys.exit(f"Demo board already exists (slug '{SLUG}') — "
                 "run with --cleanup first if you want a fresh one.")
    board = _call("POST", "/api/v1/boards", {
        "name": "🚀 Demo: Product Launch",
        "slug": SLUG,
        "description": "Seeded demo board — safe to delete "
                       "(python3 scripts/demo-seed.py --cleanup).",
        "objective": "Ship v1.0 publicly: site live, docs done, launch thread out.",
        "color": "#0FA3A3",
    })
    agent_ids: dict[str, str] = {}
    for name, emoji, role, is_lead in DEMO_AGENTS:
        agent = _call("POST", "/api/v1/agents", {
            "name": name,
            "emoji": emoji,
            "role": role,
            "board_id": board["id"],
            "is_board_lead": is_lead,
            "agent_runtime": "cli-bridge",
        })
        agent_ids[name] = agent["id"]
    created = 0
    for title, task_status, priority, description, assignee in DEMO_TASKS:
        body = {
            "title": title,
            "description": description,
            "status": task_status,
            "priority": priority,
        }
        if assignee:
            body["assigned_agent_id"] = agent_ids[assignee]
        _call("POST", f"/api/v1/boards/{board['id']}/tasks", body)
        created += 1
    print(f"Demo board '🚀 Demo: Product Launch' created with "
          f"{len(agent_ids)} demo agents and {created} tasks across the "
          f"pipeline.")
    print("The demo crew are registry entries only — nothing runs until you "
          "provision a real agent: docs/setup/first-agent.md")


if __name__ == "__main__":
    cleanup() if "--cleanup" in sys.argv else seed()
