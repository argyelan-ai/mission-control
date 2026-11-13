# A worker over MCP

A harness that can load an MCP server can work cards from the MC task board —
without a bridge written for it and without the `mc` command in a shell. It
gets the tools to fetch a card, report progress, finish it and use the vault,
and it is told how MC works at the moment it connects.

Decision and limits: [ADR-101](../decisions/101-mcp-worker-flow.md).

Use this for a harness that has **no** bridge in MC. An agent that already
runs through a bridge (Docker agents, the host bridges) polls and sends
heartbeats itself — do not give it the worker switch as well.

## What you need

- MC running and reachable from the worker's machine (`MC_API_URL`).
- On the worker's machine: this repository checked out (the server is
  `scripts/mc-mcp.py`; it needs `scripts/mc-cli` next to it to finish cards)
  and Python 3.10 or newer with `fastmcp` and `httpx`
  (`pip install fastmcp httpx`).
- A harness that loads MCP servers over stdio.

## 1. Create the worker in MC

A worker is a normal agent row with its own token, an explicit scope list and
one board. Create it with the API — one call, and nothing is installed or
started on the MC machine:

```bash
curl -s -X POST "$MC_API_URL/api/v1/agents" \
  -H "Authorization: Bearer $OPERATOR_JWT" -H "Content-Type: application/json" \
  -d '{
    "name": "External worker",
    "role": "Works cards over MCP",
    "agent_runtime": "host",
    "runtime_id": "<runtime uuid or slug>",
    "board_id": "<board uuid>",
    "scopes": ["tasks:read", "tasks:write", "vault:read", "vault:write"]
  }'
```

- **`scopes` must be an explicit list.** An empty list means *all* scopes.
  The four above are what the flow needs; add more only when the job needs
  them.
- **`agent_runtime: host`** — MC starts nothing; the worker's own poll
  fetches the cards assigned to it.
- **`runtime_id`** names the model runtime the worker uses. MC uses it for its
  records and cost numbers, and as a gate: while that runtime is not ready,
  the poll hands out no fresh card. The harness itself decides which model it
  calls.
- The answer contains the plain **`token`**. Put it straight into a file only
  the worker's user can read:

  ```bash
  umask 077 && printf 'MC_AGENT_TOKEN=%s\n' "<token>" > ~/.mc-worker.env
  ```

  MC keeps the token too: in the agent's TOOLS.md (readable on the agent
  page, tab *TOOLS.md*) and as an agent secret — the same as for every agent.
  Anyone who can open the agent page can read it.

The agent wizard in the UI also creates the row, but for a host agent it then
runs "provision": that step issues a **new** token and stages service files
that a worker does not use. If you used the wizard, take the current token
from the agent's TOOLS.md tab.

## 2. Give the worker a card

Assign a card to the worker on its board and leave it in the inbox. The
worker's own poll claims it — no dispatch step, and the board's auto-dispatch
setting does not matter.

## 3. The MCP entry

Add one server to the harness' MCP config. The common JSON shape
(`.mcp.json` in the folder the harness starts in):

```json
{
  "mcpServers": {
    "mc": {
      "command": "python3",
      "args": ["/path/to/mission-control/scripts/mc-mcp.py"],
      "env": {
        "MC_API_URL": "http://mc-host",
        "MC_AGENT_ENV_FILE": "/home/worker/.mc-worker.env",
        "MC_MCP_WORKER": "1"
      }
    }
  }
}
```

| Variable | Meaning |
|---|---|
| `MC_API_URL` | Base address of MC, without `/api/v1`. Default `http://localhost`. |
| `MC_AGENT_ENV_FILE` | File with a line `MC_AGENT_TOKEN=…`. Keeps the token out of the harness config. `MC_AGENT_TOKEN` directly works too. |
| `MC_MCP_WORKER` | `1` switches the worker tools on. Without it the server offers no card flow. |
| `MC_MCP_HEARTBEAT_SECONDS` | Seconds between heartbeats while a card is held. Default `30`, at most `60`, `0` = off. |
| `MC_MCP_HEARTBEAT_QUIET_MINUTES` | The heartbeat stops when the model has called no MC tool for this long. Default `20`, `0` = no limit. |

`command` must be a Python that has `fastmcp` and `httpx`; give the full path
when the harness starts servers with a short `PATH`.

One server process serves one worker. Two workers need two agents and two
tokens — never share a token.

## 4. The prompt

The server tells the model the loop when it connects, and `mc_handbook` holds
the full rules. The harness prompt therefore stays short:

```text
You are connected to Mission Control (MC) through the MCP server "mc".
Follow that server's instructions: fetch your next card and work it to the
end. Stop when the card is finished.
```

For a worker that should keep going, let the harness repeat the run, or tell
the model to call `mc_next_task` again after each card.

## 5. Check that it works

1. Start the harness and ask the model to list its tools. `mc_next_task`,
   `mc_progress`, `mc_finish`, `mc_handbook` and the three `mc_vault_*` tools
   must be there. (A harness may prefix the names, e.g. `mcp__mc_next_task`.)
2. Put a small card on the board and assign it to the worker.
3. In MC: the card goes to *in progress*, progress notes appear as comments,
   the agent shows *working* with a fresh "last seen", and the card ends in
   *done* or *review*.

### When the tools are missing

- **The model sees only its built-in tools.** Some harnesses hide MCP tools
  behind a search tool once many tools are loaded. omp does this above 40
  tools (`tools.discoveryMode: auto`), and it also loads MCP servers from
  other tools' config files on the machine. Give the worker its own config
  with tool discovery off and the other sources disabled:

  ```yaml
  # config.yml of the worker's own omp agent folder (PI_CODING_AGENT_DIR)
  disabledProviders: [claude, cline, codex, cursor, gemini, opencode, github, vscode, windsurf]
  tools:
    discoveryMode: "off"
  ```

- **Only vault tools, no `mc_next_task`.** `MC_MCP_WORKER` is not set, or the
  process runs as a guest session.
- **"no card" for ever.** The card is not assigned to this worker, it is not
  in the inbox, its dependencies are not met, or it is held or stopped. If
  the answer names a reason ("your model runtime is not ready", "an MC update
  is running", "the system is halted", "the operator paused you"), that is
  the reason.
- **A card is shown but "NOT accepted".** It is in review or blocked. The
  worker leaves it as it is; the card moves on when the reviewer, the lead or
  the operator acts.
- **`mc_finish` says the `mc` command line is missing.** `scripts/mc-cli` is
  not next to `scripts/mc-mcp.py`.

## Cutting the access

- **For good:** reset the token. The UI has no button for it yet:

  ```bash
  curl -s -X POST "$MC_API_URL/api/v1/agents/<agent uuid>/reset-token" \
    -H "Authorization: Bearer $OPERATOR_JWT"
  ```

  The answer contains the new token; do not hand it out to lock the worker
  out. The old token is refused from the next call on (ADR-010 Nachtrag).
- **For a while:** *Pause* on the agent page. The worker gets no new card
  (`mc_next_task` says "the operator paused you"); a card it already
  accepted runs to its end. *Active* again, and the next call fetches the
  waiting card. Pause does not lock the token: the worker can still read
  and comment.
- **The tools only:** remove `MC_MCP_WORKER` from the MCP entry; they are gone
  at the next start.

Do not delete the agent row — archive or pause it.

## Limits worth knowing

- MC cannot restart a worker. When a worker goes silent, MC delivers the card
  again; that helps only while the harness is still running. MC's recovery
  logs a warning that it found no service for the worker — expected.
  `RECOVERY_TIER2_SKIP_AGENT_SLUGS` silences it for a named worker.
- The heartbeat proves that the server process lives and that the model
  talked to MC recently. It does not prove that the model is producing
  output. A model that thinks longer than the quiet window without one tool
  call falls silent.
- The brief of a card is written for agents with the `mc` command. The worker
  gets a table that maps those verbs to its tools; only `mc blocked` has no
  tool.
- A worker counts as live in MC only while it calls MC. Between cards no
  heartbeat is sent; "last seen" is its last call to MC.
- The token lives on the worker's machine. Treat that machine like one that
  holds an agent's env file.
