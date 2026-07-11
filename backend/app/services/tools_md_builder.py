"""
ToolsMdBuilder — generates TOOLS.md for agents.

Extracted from agents.py for better testability and shorter files.
Pure function (no DB dependency, pure string generation).
"""
from app.constants import REFLECTION_MIN_CHARS, REFLECTION_REQUIRED_FIELDS


def generate_tools_md(
    name: str,
    emoji: str,
    raw_token: str,
    board_id: str | None,
    is_board_lead: bool = False,
    scopes: list[str] | None = None,
    runtime: str = "docker",
) -> str:
    """Generates a pre-filled TOOLS.md for a newly created agent.

    If scopes is given, only sections for allowed scopes are generated.
    scopes=None or scopes=[] → all sections (backward compat).

    runtime: "docker" (cli-bridge, default) or "host" (Boss). Affects
    only the vault section, because host agents access the filesystem
    ~/.mc/vault directly instead of the container mount /vault.
    """
    from app.scopes import Scope

    def _has(scope: str) -> bool:
        """True if no scopes are set (backward compat) or the scope is present."""
        if not scopes:
            return True
        return scope in scopes

    # `mc finish` reflection skeleton — single-sourced from
    # app.constants.REFLECTION_REQUIRED_FIELDS so every example in this file
    # renders the exact 4 German headers the backend validator requires.
    # Used both in the "Close a task" section and the Flow 1 worked example.
    _reflection_skeleton = "\\n".join(
        f"## {field}\\n..." for field in REFLECTION_REQUIRED_FIELDS
    )

    # ── Board sections ───────────────────────────────────────────────────
    board_section = ""
    if board_id:
        parts = []

        if _has(Scope.TASKS_READ):
            parts.append(f"""## Read board snapshot (all tasks + memory)
GET $MC_API_URL/api/v1/agent/boards/{board_id}
Authorization: Bearer $MC_AGENT_TOKEN""")

            parts.append(f"""## Get next task (pull dispatch)
GET $MC_API_URL/api/v1/agent/boards/{board_id}/tasks/next
Authorization: Bearer $MC_AGENT_TOKEN

HTTP 200 → task + context returned (task automatically set to in_progress)
HTTP 204 → no task available (agent idle or all dependencies blocked)""")

            # Read deliverables
            parts.append(f"""## Read a task's deliverables
```
curl -s "$MC_API_URL/api/v1/agent/boards/{board_id}/tasks/{{task_id}}/deliverables" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```""")

        if _has(Scope.TASKS_READ):
            parts.append(f"""## List board agents (for assigned_agent_id)

If you want to create subtasks with assigned_agent_id, fetch the agent UUIDs via API:

```
curl -s "$MC_API_URL/api/v1/agent/boards/{board_id}/agents" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

Response: list with id, name, role, is_board_lead per agent.""")

        if _has(Scope.TASKS_CREATE):
            if is_board_lead:
                # Board Lead gets project management + orchestrator section
                parts.append(f"""## List projects

BEFORE creating a new project, check whether it already exists.
Projects are also included in the board-read response (GET /agent/boards/{{board_id}} → "projects" array).

```
curl -s "$MC_API_URL/api/v1/agent/boards/{board_id}/projects" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

Response: list of all projects with id, name, status, project_type, progress_pct, etc.

## Create a project (when the task describes an entire project)

If the task describes a self-contained project (website, app, feature with multiple parts),
create a project FIRST and use its project_id on the subtasks.

Signs a task is a project:
- "Build me a website/app/tool"
- Multiple components (frontend + backend, design + implementation)
- Needs its own deployment or its own repository

```
curl -s -X POST "$MC_API_URL/api/v1/agent/boards/{board_id}/projects" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "name": "Project name",
    "description": "What is to be built",
    "project_type": "website",
    "priority": "medium"
  }}'
```

project_type: feature | website | content | research | automation | design | free
The response contains the project_id — use it on all subtasks.

## Create and delegate a subtask (orchestrator)

IMPORTANT: You are the orchestrator. ALWAYS create subtasks with parent_task_id and assigned_agent_id.
NEVER create tasks without these fields — otherwise the task gets assigned to yourself.

```
curl -s -X POST "$MC_API_URL/api/v1/agent/boards/{board_id}/tasks" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "title": "Concrete task for the agent",
    "description": "## Goal\\nConcretely what should be achieved.\\n\\n## Context\\n- Path: ~/Workspace/Projects/mission-control/\\n- URL: http://localhost\\n\\n## Guardrails\\n- Do not change the DB schema\\n\\n## Expected output\\n- PR with changes\\n\\n## Definition of Done\\n- Tests green",
    "credentials": "email: admin@mc.local / password: xxx",
    "parent_task_id": "YOUR-TASK-ID-HERE",
    "assigned_agent_id": "AGENT-UUID-HERE",
    "project_id": "PROJECT-UUID-HERE-IF-ANY",
    "priority": "medium",
    "tags": ["backend", "api"]
  }}'
```

REQUIRED fields in the description (the agent has NO chat context!):
1. **Goal** — What exactly should be achieved?
2. **Context** — Paths, URLs, stack info
3. **Guardrails** — What NOT to do
4. **Expected output** — Screenshots, PRs, files
5. **Definition of Done** — Measurable completion criteria

**Credentials** — If a login/API key is needed: put it in the `credentials` field (NOT in the description!). If unknown: ask the operator.

Fields:
- parent_task_id: The task ID YOU received (your main task)
- assigned_agent_id: UUID of the agent that should execute the subtask
- project_id: UUID of the project (if you created one). Automatically inherited from the parent if not set.
- depends_on: ["task-uuid", ...] — optional dependencies (subtask waits on these tasks)
- tags: list of tag names (optional). Shown as colored labels in the pipeline.

**delegation_type** (optional — enables contract check, 422 on a wrong value):
| Value | When | Required fields |
|------|------|---------------|
| `code_change` | Agent should change code | `branch_name`, `acceptance_criteria` |
| `visual_proof` | Screenshots/visual verification | `target_url`, `acceptance_criteria`, `expected_content` |
| `credential_bound` | Needs a login/API key | `credentials`, `target_url`, `acceptance_criteria` |
| `review` | Agent reviews another task | `source_task_id` |
| `planning` | Planning subtask | — |
For research tasks: OMIT delegation_type (no contract check). Invalid values (e.g. "research") result in 422.

**autonomy_level** (optional — controls whether operator approval is needed):
- `execute_low_risk` — execute immediately (sandbox, no browser, no credentials, no DB)
- Omit → conservative default → operator approval needed

IMPORTANT: When you create a subtask with parent_task_id, your parent task
is automatically set to in_progress (= ACK). You do NOT need to confirm the parent manually.

Status: inbox | in_progress | review | done | blocked
Priority: low | medium | high | critical""")
            else:
                parts.append(f"""## Create task
POST $MC_API_URL/api/v1/agent/boards/{board_id}/tasks
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{{
  "title": "Task title",
  "description": "MUST be Markdown. Minimum structure:\n## Goal\n...\n## Context\n...\n## Definition of Done\n...",
  "status": "inbox",
  "priority": "medium",
  "tags": ["bugfix"],
  "assigned_agent_id": "UUID-OF-TARGET-AGENT",
  "parent_task_id": "YOUR-TASK-ID-HERE"
}}

Fields:
- assigned_agent_id: UUID of the agent that should execute the task (REQUIRED when delegating!)
- parent_task_id: your own task ID (REQUIRED when creating subtasks!)
- tags: list of tag names (optional). Examples: "backend", "frontend", "bugfix", "refactor"
- depends_on: ["task-uuid", ...] — optional dependencies

**delegation_type** (optional — enables contract check, 422 on a wrong value):
| Value | When | Required fields |
|------|------|---------------|
| `code_change` | Agent should change code | `branch_name`, `acceptance_criteria` |
| `visual_proof` | Screenshots/visual verification | `target_url`, `acceptance_criteria`, `expected_content` |
| `credential_bound` | Needs a login/API key | `credentials`, `target_url`, `acceptance_criteria` |
| `review` | Agent reviews another task | `source_task_id` |
| `planning` | Planning subtask | — |
For research tasks: OMIT delegation_type. Invalid values (e.g. "research") result in 422!

**autonomy_level** (optional):
- `execute_low_risk` — execute immediately (sandbox, no browser, no credentials, no DB)
- Omit → operator approval needed

IMPORTANT: When you create a subtask with parent_task_id, your parent task
is automatically set to in_progress (= ACK). You do NOT need to confirm the parent manually.

SKILLS/TOOLS PASSTHROUGH: If the main task references a specific skill or tool
(e.g. "use Stitch", "use /website", "use the FreeCode researcher"), then
this reference MUST be explicitly included in the subtask's description.
The target agent has no chat context — only what's in the description.

Status: inbox | in_progress | review | done | blocked
Priority: low | medium | high | critical""")

        if _has(Scope.TASKS_WRITE):
            parts.append(f"""## Update task
**IMPORTANT: For status changes (ack/review/done/blocked/failed) ALWAYS use the `mc` CLI — never raw PATCH.**
The raw PATCH requires the `X-Dispatch-Attempt-Id` header (value from $X_DISPATCH_ATTEMPT_ID) — missing it makes the server return 409.
The mc CLI reads the header automatically from /tmp/mc-context.env. Use raw PATCH only for fields like priority/title/project_id.

PATCH $MC_API_URL/api/v1/agent/boards/{board_id}/tasks/{{task_id}}
Authorization: Bearer $MC_AGENT_TOKEN
X-Dispatch-Attempt-Id: $X_DISPATCH_ATTEMPT_ID
Content-Type: application/json

{{
  "priority": "high",
  "project_id": "PROJECT-UUID-OR-NULL"
}}

Fields changeable via raw PATCH: priority, title, description, project_id.
Status changes: use `mc ack` (start) / `mc finish [--review]` (close, see below) / `mc blocked` / `mc failed`.
`mc done` and `mc review` still exist as raw status-only commands, but `mc finish` is the
canonical close — it posts the mandatory reflection and sets the status in one atomic call.

If you're blocked, use `mc blocked` (not raw PATCH):
`mc blocked --blocker-type missing_info --question "What exactly is missing"`
`mc blocked --blocker-type technical_problem --description "Description"`

blocker_type: missing_info | technical_problem | decision_needed | permission_needed | dependency_blocked | other

## Add comment to task
POST $MC_API_URL/api/v1/agent/boards/{board_id}/tasks/{{task_id}}/comments
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{{
  "content": "Progress report or note about the task",
  "comment_type": "message"
}}

comment_type: message | handoff | blocker | progress | resolution [terminal — auto-promotes task to review/done] | feedback""")

            # `mc finish` is the atomic close verb — always show it with the
            # exact 4 German headers agents must produce, single-sourced from
            # app.constants.REFLECTION_REQUIRED_FIELDS (not hardcoded here) so
            # this section can never drift from the backend's validator.
            # (_reflection_skeleton computed once, function-scope, above.)
            parts.append(f"""## Close a task (mc finish)
`mc finish` is the canonical close — it posts the mandatory self-reflection AND sets the
status in one atomic call. Plain `mc done`/`mc review` without a prior reflection comment
is rejected by the backend with 400.

```bash
mc finish "{_reflection_skeleton}"
# → status: done (subtasks / research-only tasks)

mc finish --review "{_reflection_skeleton}"
# → status: review (code/API/security tasks — a reviewer looks at it before done)
```

The reflection needs all {len(REFLECTION_REQUIRED_FIELDS)} headers verbatim (exact German
text, `##` prefix) and at least {REFLECTION_MIN_CHARS} characters total — a one-line
"done." is rejected. `mc finish --force` closes any still-open checklist items first
(auto-marks them done) instead of blocking the pre-flight check; use `mc checklist skip
<id> --reason "..."` beforehand instead if an item is genuinely out of your role rather
than actually finished.""")

            # Register deliverable
            parts.append(f"""## Register deliverable (result artifact)
Register results as a deliverable — visible in the MC UI.

> **Note:** `mc deliverable`, `mc pdf`, and `mc telegram` resolve your current task automatically.
> You don't need to provide a task ID — the backend finds it via spawn_session_key.

```bash
# Simplest way (preferred):
mc deliverable --type document --title "Research result" --path /deliverables/$TASK_ID/report.md

# Or via curl (task ID is resolved automatically — no board_id/task_id in the URL):
curl -s -X POST "$MC_API_URL/api/v1/agent/me/deliverable" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{"deliverable_type": "document", "title": "Research result", "content": "# Title\\n\\nFull Markdown content here...", "path": "/deliverables/$TASK_ID/report.md"}}'
```

deliverable_type: `screenshot` | `file` | `url` | `artifact` | `document` | `data`
Required fields: `deliverable_type`, `title`.
**IMPORTANT: ALWAYS include `content`** for document/artifact/file — the `path` points into the container filesystem and isn't readable by the frontend. Without `content`, the deliverable is empty in the UI.""")

            # Crash-recovery / progress-tracking is now TaskChecklistItem
            # (Workstream A4, ADR-020). POST /checkpoint returns HTTP 410
            # and the TaskCheckpoint table is a read-only archive being
            # dropped in a follow-up migration. Agents use `mc checklist`
            # for progress and the reflection comment for lessons.
            #
            # `mc checklist` is the ONE way to manage checklists — no raw
            # curl variant is documented anymore (removed 2026-07, it
            # taught agents a second, parallel path that drifted from the
            # CLI's validation/recovery behavior). One capability is lost
            # versus the old curl example: bulk-create in a single call.
            # `mc checklist add` only accepts one title per call — call it
            # once per checklist item.
            parts.append(f"""## Manage checklist (task progress, replaces the old /checkpoint)
Create the checklist as your VERY FIRST step after ACK — it gives the operator and
the recovery path a concrete list of steps. Checklist + progress comments replace
the old /checkpoint endpoint entirely.

```bash
# Add item (one call per item — no bulk-create, call once per step)
mc checklist add "Step 1 — write models"
mc checklist add "Step 2 — wire up endpoints"
# Mark item as done
mc checklist done <item_id>
# Item you can't do in your role (e.g. a live deploy only Deployer can run)
mc checklist skip <item_id> --reason "Needs Deployer — no npm/node in this container"
# Show list
mc checklist list
# Progress comment (Update/Evidence/Next)
mc comment progress "Update — models created
Evidence — backend/app/models/foo.py:1-40, tests green
Next — wire up endpoints"
```

status values: `pending` | `in_progress` | `done` | `blocked` | `skipped`

On crash/timeout/re-dispatch, the recovery context renders your checklist
with a `← RESUME HERE` marker on the first open item. No manual
checkpoints needed anymore.""")

        if _has(Scope.MEMORY_READ):
            parts.append("""## Memory search (own + team lessons retrievable)
You can search your own earlier lessons and team memory via CLI —
automatic embeddings via Qdrant, retrieval via semantic search.

```bash
mc memory search "<query phrase>" --limit 5
```

Or directly via HTTP:
```
POST $MC_API_URL/api/v1/agent/memory/query
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{"query": "<search text>", "layers": ["semantic", "agent"], "top_k": 5}
```

Using this consistently lets you find your own lessons again, learn from
team experience, and avoid duplicate work.""")

        if _has(Scope.MEMORY_WRITE):
            parts.append(f"""## Write board memory (board-scoped, visible to all agents on the board)
POST $MC_API_URL/api/v1/agent/boards/{board_id}/memory
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{{
  "content": "Important finding or decision",
  "title": "Optional title",
  "memory_type": "knowledge",
  "tags": ["tag1", "tag2"],
  "is_pinned": false
}}""")

        if _has(Scope.APPROVALS_CREATE):
            parts.append(f"""## Request approval (human-in-the-loop — blocking action)
POST $MC_API_URL/api/v1/agent/boards/{board_id}/approvals
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{{
  "action_type": "deployment",
  "description": "What exactly needs approval",
  "confidence": 0.85
}}""")

        if _has(Scope.CHAT_WRITE):
            chat_hint = ""
            if is_board_lead:
                chat_hint = """

NOTE: Board chat is for direct conversation with the operator.
NOT for task-related communication. Use task comments for that instead."""
            else:
                chat_hint = """

NOTE: Board chat is for urgent help requests to Henry (board lead).
Use board chat when you are BLOCKED and need quick help.
For normal task updates → use task comments instead."""
            parts.append(f"""## Send chat message to board
POST $MC_API_URL/api/v1/agent/boards/{board_id}/chat
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{{
  "content": "Message to the board channel"
}}{chat_hint}""")

        # ── Project section ────────────────────────────────────────────
        if _has(Scope.PROJECT_READ):
            parts.append(f"""## Fetch project context

IF you receive a task with project_id, read the project briefing first:

```
curl -s "$MC_API_URL/api/v1/agent/projects/{{project_id}}" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

Response contains:
- briefing_doc (Markdown — always read first!)
- phases (list of all phases with status)
- last_active_phase_id (current phase)

## Search a project's deliverables

```
curl -s "$MC_API_URL/api/v1/agent/projects/{{project_id}}/deliverables?scope=phase&is_pinned=true" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

Query parameters: scope=task|phase|project  is_pinned=true  tags=research,design""")

        if _has(Scope.PROJECT_WRITE):
            parts.append(f"""## Register deliverable with project context (V2)

```bash
# Task ID is resolved automatically (no board_id/task_id in the URL):
curl -s -X POST "$MC_API_URL/api/v1/agent/me/deliverable" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{{{
    "deliverable_type": "artifact",
    "title": "Competitor Analysis",
    "content": "Full Markdown content of the deliverable",
    "scope": "phase",
    "tags": ["research", "analysis"],
    "is_pinned": false,
    "is_reusable": true,
    "git_commit": true
  }}}}'
```

scope: task | phase | project
is_pinned: true = injected into agent context (use sparingly!)
git_commit: true = committed to the phase branch (recommended for scope=phase/project)
deliverable_type: `screenshot` | `file` | `url` | `artifact` | `document` | `data`

## Create sub-task

```
curl -s -X POST "$MC_API_URL/api/v1/agent/boards/{board_id}/tasks" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{{{
    "title": "Research: OKLCH Color Spaces",
    "description": "## Goal\\n...\\n## Context\\n...\\n## Definition of Done\\n...",
    "project_id": "{{project_id}}",
    "phase_id": "{{phase_id}}",
    "triggered_by_deliverable_id": "{{deliverable_id}}",
    "depends_on": ["{{current_task_id}}"]
  }}}}'
```

triggered_by_deliverable_id: ALWAYS set when created from a deliverable (provenance!)

## Complete phase

```
curl -s -X POST "$MC_API_URL/api/v1/agent/phases/{{phase_id}}/complete" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

Only call this once ALL tasks of the phase are `done`.""")

        if parts:
            board_section = "\n\n".join(parts) + "\n"

    # ── Board Lead Sektion ───────────────────────────────────────────────
    board_lead_section = ""
    if is_board_lead and _has(Scope.AGENTS_MANAGE):
        board_id_placeholder = board_id or "{board_id}"
        board_lead_section = f"""
---

## Create a new agent (only you as board lead)

Procedure:
1. Ask the operator: use a template or set up the agent from scratch?

2a. Template route:
    GET $MC_API_URL/api/v1/agent/templates
    Authorization: Bearer $MC_AGENT_TOKEN
    → pick a template from the list

    POST $MC_API_URL/api/v1/agent/templates/{{template_id}}/instantiate
    Authorization: Bearer $MC_AGENT_TOKEN
    Content-Type: application/json

    {{
      "board_id": "{board_id_placeholder}",
      "name": "Optional name"
    }}

2b. Custom route (without a template):
    POST $MC_API_URL/api/v1/agent/agents
    Authorization: Bearer $MC_AGENT_TOKEN
    Content-Type: application/json

    {{
      "name": "Agent name",
      "emoji": "🤖",
      "role": "Description of the role",
      "model": null,
      "skills": [],
      "board_id": "{board_id_placeholder}"
    }}

Response of both endpoints: {{ "agent": {{...}}, "token": "..." }}
→ the agent is provisioned automatically (ready to use in ~10 seconds)
→ the token is shown ONLY ONCE — hand it to the operator immediately!

Progress appears in the activity feed.

## Read and change your own SOUL.md

Read:
```
curl -s "$MC_API_URL/api/v1/agent/config/soul_md" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

Change (CAUTION — changes your own behavior!):
```
curl -s -X PUT "$MC_API_URL/api/v1/agent/config/soul_md" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{"content": "New SOUL.md content...", "reason": "Short justification for the change"}}'
```

**Rules:**
- ALWAYS provide `reason` — the operator sees the change in the activity feed
- Only fix errors or add missing rules — do not change the basic structure
- When in doubt, ask the operator before changing your SOUL
- The change syncs to the gateway/disk automatically
"""

    # ── Vertical sections (e.g. News Studio content pipeline) ────────────
    # Verticals register (scope, builder) in app.verticals.hooks —
    # stripped public release: empty list, no section.
    from app.verticals import hooks as vertical_hooks

    content_section = ""
    for _scope_str, _builder in vertical_hooks.tools_md_sections:
        if _has(Scope(_scope_str)):
            content_section += _builder({})

    # ── Credentials section ───────────────────────────────────────────────
    credentials_section = ""
    if board_id and _has(Scope.CREDENTIALS_READ):
        credentials_section = f"""
---

## Credentials Vault (vs. system secrets) — ADR-033

MC has **two** separate secret stores. You use only one of them directly.

|  | `secrets` (system token wallet) | `credentials` (task vault) — **your store** |
|---|---|---|
| **What** | 1 entry per provider/service | N entries per use case, typed login/token/custom |
| **Examples** | openai_api_key, anthropic_api_key, github_token, discord_bot_token, xai_api_key, livekit_api_key | client-login, twitter-bearer, external API token, trading account |
| **Who writes** | Only the operator (admin) | Any logged-in user |
| **Agent access** | **None** — backend services use them on the operator's behalf | Read via API (see below) |

**Rule of thumb:**
- LLM provider / GitHub / Discord / OpenClaw token → `secrets`. The backend uses them. Never ask for them, never search for them.
- Login/token for a task-specific action (website, external API, trading) → `credentials`. You fetch these yourself from the vault.

If a dispatch brief references a `credential_id` (UUID): fetch it via the vault API (below). If a brief suddenly shows `openai_api_key` or similar: that's a system secret — ask the operator back instead of searching for an API yourself.

### List all credentials (masked)
```
curl -s "$MC_API_URL/api/v1/agent/boards/{board_id}/credentials" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

Response: list with id, name, credential_type, url, notes, data_masked (password/token partially hidden).
Use this endpoint to find the correct credential ID.

### Fetch a single credential (fully decrypted)
```
curl -s "$MC_API_URL/api/v1/agent/boards/{board_id}/credentials/{{credential_id}}" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

Response: id, name, credential_type, url, notes, data (fully decrypted dict).

**When to use:**
- Dispatch brief references a `credential_id` (UUID) — primary route
- Task mentions an external site/service you need to log into
- Dispatch context has no inline `credentials` field but you need a login

**Security note:** Never store credentials in comments, commits, or logs.
"""

    # ── Deploy section ────────────────────────────────────────────────────
    deploy_section = ""
    if _has(Scope.DEPLOY_EXECUTE):
        deploy_section = f"""
---

## Deploy the Docker environment

You run Docker commands DIRECTLY in the terminal (not via API).
Allowed services: backend, frontend, caddy.
NEVER: db, redis.

### Shell commands (run directly)

Restart (fast, no rebuild):
  cd ~/Workspace/Projects/mission-control && docker compose restart backend

Rebuild (after code changes):
  cd ~/Workspace/Projects/mission-control && docker compose up --build -d backend

Backup before larger deployments:
  cd ~/Workspace/Projects/mission-control && ./backup.sh

Check logs:
  docker compose logs backend --tail=50

### Monitoring API (MC backend)

Health check of all services:
GET $MC_API_URL/api/v1/agent/deploy/services
Authorization: Bearer $MC_AGENT_TOKEN

Health check of a single service:
GET $MC_API_URL/api/v1/agent/deploy/services/{{service_name}}/health
Authorization: Bearer $MC_AGENT_TOKEN

Fetch deploy history:
GET $MC_API_URL/api/v1/agent/deploy/history
Authorization: Bearer $MC_AGENT_TOKEN

### Record deploy (AFTER every deploy)
POST $MC_API_URL/api/v1/agent/deploy/record
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{{
  "service": "backend",
  "action": "rebuild",
  "success": true,
  "health_status": "healthy",
  "duration_seconds": 45.2
}}

action: rebuild | restart | rollback | backup
service: backend | frontend | caddy

### Workflow
1. Create backup (on rebuild)
2. Run Docker command
3. Wait 30 seconds
4. Health check via API
5. Record deploy via API
6. On failure: rollback (docker compose restart) + record with rolled_back=true

### ABSOLUTE LIMITS
- NO docker compose down without operator approval
- NO changes to .env
- NEVER touch db and redis

---

## External app deployments

### Step 0: Fetch credentials
GET $MC_API_URL/api/v1/agent/deploy/credentials
Authorization: Bearer $MC_AGENT_TOKEN

Store the values as variables: VERCEL_TOKEN, CF_TOKEN, CF_ZONE_ID, SB_TOKEN

### Step 1: Install Vercel CLI (one-time)
```bash
npm install -g vercel
```

### Step 2: Deploy frontend to Vercel
```bash
cd /path/to/project
vercel deploy --prod --token=$VERCEL_TOKEN --yes
```
The return value contains the deployment URL (e.g. https://project-abc.vercel.app)

### Step 3: Create Cloudflare subdomain
```bash
curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/dns_records" \\
  -H "Authorization: Bearer $CF_TOKEN" \\
  -H "Content-Type: application/json" \\
  --data '{{"type":"CNAME","name":"APP_NAME","content":"cname.vercel-dns.com","proxied":true}}'
```
Replace APP_NAME with the desired subdomain name (e.g. "shop" → shop.your-domain.com)

### Step 4: Add the domain to the Vercel project
```bash
vercel domains add APP_NAME.your-domain.com --token=$VERCEL_TOKEN
```

### Step 5: Create Supabase project (optional, if the app needs a DB)
```bash
npx supabase projects create APP_NAME --org-id ORG_ID --db-password PASS --token $SB_TOKEN
```

### Step 6: Security check
Run these checks after EVERY external deploy:

```bash
# Check HTTPS redirect
curl -sI http://APP_NAME.your-domain.com | grep -i "location"

# Check security headers
curl -sI https://APP_NAME.your-domain.com

# Test sensitive paths (must be 404)
curl -s -o /dev/null -w "%{{http_code}}" https://APP_NAME.your-domain.com/.env
curl -s -o /dev/null -w "%{{http_code}}" https://APP_NAME.your-domain.com/.git/config
```

Checklist:
- strict-transport-security (HSTS) — MUST be present
- x-content-type-options: nosniff — SHOULD be present
- x-frame-options — SHOULD be present
- .env and .git NOT reachable (404)
- No secrets in the HTML (curl -s URL | grep -iE "api.key|token|secret")

### Step 7: Visual check + screenshot to the operator
```bash
# Open browser and take a screenshot (dev-browser)
dev-browser <<'EOF'
const page = await browser.getPage("deploy");
await page.goto("https://APP_NAME.your-domain.com");
await page.waitForLoadState("networkidle");
const buf = await page.screenshot({{ fullPage: true }});
const path = await saveScreenshot(buf, "deploy-check.png");
console.log(path);
EOF
# Read the path from the output (~/.dev-browser/tmp/deploy-check.png)

# Send the screenshot to the operator via Telegram (via mc verify for URLs, or mc deliverable+telegram)
# For live URLs: the visual verification service takes a screenshot + metrics + posts automatically
mc verify https://APP_NAME.your-domain.com --caption "Deploy check: APP_NAME.your-domain.com — [OK/issues]"

# Or for a local screenshot: register as a deliverable (type=screenshot) first,
# then attach to Telegram with --photo
mc deliverable --type screenshot --title "Deploy check" --path "~/.dev-browser/tmp/deploy-check.png"
mc telegram "Deploy check: APP_NAME.your-domain.com — [OK/issues]" --photo <deliverable-id>
```

### Workflow summary
1. Fetch credentials (GET /api/v1/agent/deploy/credentials)
2. vercel deploy → deployment URL
3. Cloudflare DNS → create subdomain
4. vercel domains add → link domain
5. Security check (HTTPS, headers, sensitive paths)
6. Visual check (screenshot + vision analysis)
7. Screenshot + report to the operator via Telegram
8. Record deploy (POST /api/v1/agent/deploy/record)
"""

    # ── Install request section ───────────────────────────────────────────
    install_request_section = ""
    if _has(Scope.AGENTS_MANAGE):
        install_request_section = f"""
---

## Plugin management for workers

You may assign or remove already-installed CLI plugins on worker agents (same board)
without operator approval. Use case: Davinci needs
`higgsfield-mcp`, Sparky should only get `superpowers`, Tester needs
no plugin overhead, etc.

**Procedure:**
1. Show what's available (`mc plugin-list` / `GET /plugins`)
2. Optional: check what the worker currently has (`mc plugin-show <agent>`)
3. Set the new allowlist (`mc plugin-assign <agent> [...]`)
4. Optional: restart the worker so plugins take effect immediately (`--restart` or `mc worker-restart <agent>`)

If the desired plugin is NOT in the shared cache → request a new installation
via `Installation Requests` (section below). Operator approval is
mandatory for supply-chain protection.

### Quick form via mc CLI (recommended)

    mc plugin-list                                                  # shared cache
    mc plugin-show Davinci                                          # Davinci's allowlist
    mc plugin-assign Davinci higgsfield-mcp@anthropic-agent-skills --restart
    mc plugin-unassign Davinci superpowers@claude-plugins-official
    mc worker-restart Davinci                                       # if set without --restart

Agent name is case-insensitive and resolved within the current board.
There's also a raw-curl form below for every command.

### List available plugins

    curl http://mc-backend:8000/api/v1/agent/plugins \\
      -H "Authorization: Bearer $MC_AGENT_TOKEN"

Response: `{{"plugins": [{{"key": "...", "name": "...", "source": "...", "version": "..."}}, ...], "total": N}}`

The `key` (e.g. `frontend-design@claude-plugins-official`) is what you need for
assignment.

### Read a worker's current plugin assignment

    curl http://mc-backend:8000/api/v1/agent/agents/<target-agent-id>/plugins \\
      -H "Authorization: Bearer $MC_AGENT_TOKEN"

Response: `{{"agent_id": "...", "agent_name": "Davinci", "cli_plugins": [...] or null}}`
- `null` = all installed plugins (default)
- `[]` = no plugins
- `[...]` = explicit allowlist

### Set plugin assignment

    curl -X PATCH http://mc-backend:8000/api/v1/agent/agents/<target-agent-id>/plugins \\
      -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
      -H "Content-Type: application/json" \\
      -d '{{
        "cli_plugins": ["superpowers@claude-plugins-official", "higgsfield-mcp@anthropic-agent-skills"],
        "restart_worker": true
      }}'

**cli_plugins semantics**:
- `null` (JSON: `null`) → the worker gets all installed plugins
- `[]` (empty list) → the worker gets NOTHING
- `["a", "b"]` → only these (allowlist)

**Additive assignment**: the backend sets the field completely fresh, no merge logic.
If you want to add ONE plugin, first GET → copy the list → append → PATCH.

**restart_worker** (default false):
- `false` → new plugins only take effect after a manual worker restart or the next
  container restart. The running task context is preserved.
- `true` → the worker session (claude in tmux window 0) is killed and restarted.
  New plugins are active immediately, but the running task context is GONE.
  Only for CLI-bridge agents — host runtime (Boss itself) has no worker.

**Rule of thumb:** When reconfiguring a worker that is currently idle
(current_task_id=null, idle) → `restart_worker: true` so plugins are live immediately.
If the worker is working on a task → `false`, restart once the task is done.

### Manually restart a worker session

If you set plugins without `restart_worker`, or the worker needs to reload for
other reasons:

    curl -X POST http://mc-backend:8000/api/v1/agent/agents/<target-agent-id>/worker/restart \\
      -H "Authorization: Bearer $MC_AGENT_TOKEN"

WARNING: running task context is lost. Check beforehand via
`GET /agent/agents/<id>/detail` that `current_task_id` is null.

### Guards
- Only board leads may assign plugins + restart workers (you are one)
- Target must belong to the same board
- Board leads cannot set plugins/restarts on each other (self only)
- DB is source of truth, disk sync runs automatically (settings.json + plugins/cache)
- Worker restart only for `agent_runtime=cli-bridge` — Boss (host runtime) is not affected

---

## Installation Requests

File install or uninstall requests for **skills, plugins, and MCP servers**
that are NOT YET in the shared cache. The operator approves or rejects them in
their inbox. A successful approval triggers automatic installation via the
InstallExecutor (including rollback on smoke-test failure for MCP).

**Important:** For MCP installations on other agents, do NOT manually pip install /
edit containers — always use this system.

Endpoint: POST /api/v1/agent/install-requests

### Callback coupling (important)

File the request with `"task_id": "<your-current-task-uuid>"` — after
a successful install, the backend automatically posts an `install_completed`
comment on that task. Mirrors the `subtask_completed` pattern:
next poll cycle → you see the callback in the task context, know
the item is live, and can continue with the next action.

Without `task_id` → only an `install.*` activity_event, no auto-comment.
You would have to actively poll `GET /approvals` or check the agent feed.

**Important — while you wait**: stay `in_progress`, NOT `blocked`. The
callback arrives automatically via poll; no human needs to intervene. `blocked`
would be wrong, and the operator couldn't resolve the blocker anyway
(nobody knows what to do — the callback handles it itself).

### What happens after success — no additional assign call needed

After success, the InstallExecutor AUTOMATICALLY records the installed item in
the matching agent field:

- **install_skill** → the name is appended to `target_agent.cli_skills`
- **install_plugin** → the name is appended to `target_agent.cli_plugins`
- **install_mcp** → the name is appended to `target_agent.mcp_servers` + MCP smoke test

The executor then triggers `sync_config` so the change lands in claude-config
for CLI-bridge agents. You do NOT need to additionally call `mc plugin-assign`
or `mc worker-restart` — the install flow is fully autonomous.

`mc plugin-assign` is ONLY meant for assigning ALREADY-installed
plugins to another worker (when the shared cache already
has the plugin and you want to give it to a second worker). Skills have no
separate CLI command — they're automatically assigned after install success.

### Skill install

    curl -X POST http://mc-backend:8000/api/v1/agent/install-requests \\
      -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
      -H "Content-Type: application/json" \\
      -d '{{
        "type": "skill",
        "operation": "install",
        "source": "github:anthropic/skill-web-performance",
        "name": "web-performance",
        "target_agent_id": "<target-uuid>",
        "reason": "Agent failed 3 perf-debug tasks — this skill has checklists for that",
        "autonomy_level": "L2",
        "task_id": "'"$TASK_ID"'"
      }}'

### MCP install

    curl -X POST http://mc-backend:8000/api/v1/agent/install-requests \\
      -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
      -H "Content-Type: application/json" \\
      -d '{{
        "type": "mcp",
        "operation": "install",
        "source": "github:geopopos/higgsfield_ai_mcp",
        "name": "higgsfield-ai",
        "target_agent_id": "<davinci-uuid>",
        "reason": "Davinci needs MCP tools for Higgsfield image/video generation in the marketing project",
        "autonomy_level": "L2"
      }}'

After approval, the InstallExecutor installs the package, creates the manifest
under ~/.mc/mcp-servers/<name>/, and syncs Davinci's .mcp.json.
If the smoke test fails → automatic rollback.

Response: 201 with approval_id + existing=false (or 200 + existing=true on duplicate).

### Uninstall

    curl -X POST http://mc-backend:8000/api/v1/agent/install-requests \\
      -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
      -H "Content-Type: application/json" \\
      -d '{{
        "type": "mcp",
        "operation": "uninstall",
        "name": "higgsfield-ai",
        "target_agent_id": "<target-uuid>",
        "reason": "no longer needed"
      }}'

### Allowlist sources
- **skill**: github:anthropic/*, github:obra/*, github:getcursor/*, ~/.mc/skills/*
- **plugin**: claude-plugins-official, github:claude-plugins/*, github:anthropic/*
- **mcp**:
  - npm:@modelcontextprotocol/server-*
  - npm:@supabase/*, npm:@vercel/*, npm:@cloudflare/mcp-*
  - github:<any-org>/<repo-with-mcp-in-the-name> (e.g. `geopopos/higgsfield_ai_mcp`)

### Important
- Write a **concrete reason**: which task failed, why THIS skill/plugin/MCP,
  is there an alternative? The operator approves faster when context is clear.
- Duplicates are detected automatically — same request 2× → same approval_id.
- Already-installed check: HTTP 409 if the agent already has the item.
- Requests expire after 7 days.
"""

    # ── Knowledge sections ────────────────────────────────────────────────
    knowledge_parts = []
    if _has(Scope.KNOWLEDGE_READ):
        knowledge_parts.append(f"""## Read knowledge base
GET $MC_API_URL/api/v1/agent/knowledge
Authorization: Bearer $MC_AGENT_TOKEN

Optional parameters:
  ?memory_type=knowledge|lesson|reference|research|journal|weekly_review|insight
  ?search=search term
  ?limit=50

Returns all relevant entries: own knowledge, board memory, global knowledge.""")

    if _has(Scope.KNOWLEDGE_WRITE):
        knowledge_parts.append(f"""## Write your own knowledge entry
POST $MC_API_URL/api/v1/agent/knowledge
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{{
  "content": "Entry content",
  "title": "Optional title",
  "memory_type": "knowledge",
  "tags": ["tag1"],
  "scope": "agent"
}}

scope: "agent" (only I see it) | "board" (all board agents) | "global" (everyone)
memory_type: knowledge | lesson | reference | research | journal | weekly_review | insight""")

    knowledge_section = "\n\n".join(knowledge_parts)

    # ── Vault section (Karpathy Wiki) ─────────────────────────────────────
    vault_section = ""
    if _has(Scope.VAULT_WRITE):
        if runtime == "host":
            _vault_location = (
                f"**Your folder:** `$AGENT_VAULT_PATH` "
                f"(host path `~/.mc/vault/agents/{name.lower()}/`)"
            )
        else:
            _vault_location = (
                f"**Your folder:** `$AGENT_VAULT_PATH` "
                f"(mapped to `/vault/agents/{name.lower()}/` in the container)"
            )
        vault_section = f"""## Vault — long-term memory (Karpathy Wiki)

Mission Control's collective memory lives in a Markdown vault under `~/.mc/vault/`.
You can write directly to the filesystem AND via the backend API.

{_vault_location}
**Shared inbox:** `$AGENT_VAULT_INBOX` (for cross-agent writes)

### Write your own lessons (direct filesystem)

```bash
cat > $AGENT_VAULT_PATH/lessons/$(date +%Y-%m-%d)-rate-limit-xai.md <<'EOF'
---
id: $(uuidgen)
type: lesson
agent: {name.lower()}
date: $(date -Iseconds)
tags: [api, rate-limiting]
---
# Rate Limit on xAI API

**Context:** Task #1234
**Observation:** xAI returns 429 above 10 req/s
**Lesson:** Add exponential backoff with base=2, max_delay=60s
EOF
```

**Important:** Every file MUST have `id`, `type`, `agent`, `date` in the frontmatter.
The watcher moves invalid files to `_rejected/`.

### Cross-agent decisions (via backend API)

For files that affect other agents (e.g. `global/decisions/...`), use the inbox API.
The backend compactor merges your envelope into the canonical path.

**write_note schema (REQUIRED FIELDS):**

```json
{{
  "title": "5-7 word readable title",
  "content": "Markdown body with [[wikilink]]s inline",
  "type": "knowledge | lesson | reference | journal | note",
  "tags": ["tag1", "tag2"],
  "related_notes": ["[[note-slug-1]]", "[[note-slug-2]]"],
  "relations": {{"note-slug-1": "supersedes"}}
}}
```

`related_notes` is optional but **recommended**: first `search_notes()`, then
link 2-4 thematically relevant hits (also inline in `content`). An empty
list is allowed — the nightly wikilink backfill links orphan notes
automatically later via Qdrant similarity + Spark LLM.
Allowed relation types: `supersedes | contradicts | refines | example-of | depends-on | related-to`

```bash
# Search first, then link
curl "$MC_API_URL/api/v1/agent/vault/search?q=auth+migration" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"

# Then write the note (related_notes optional but recommended)
curl -X POST "$MC_API_URL/api/v1/agent/vault/note" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "title": "Auth Migration Learnings",
    "content": "During OAuth2 migration: refresh tokens must NOT go in localStorage. See also [[jwt-auth-overview]] and [[security-baseline]].",
    "type": "lesson",
    "target": "global/lessons/auth-migration.md",
    "tags": ["auth", "security"],
    "related_notes": ["[[jwt-auth-overview]]", "[[security-baseline]]"],
    "relations": {{"jwt-auth-overview": "supersedes"}},
    "task_id": "$TASK_ID",
    "idempotency_key": "lesson-auth-migration"
  }}'
```

`idempotency_key` prevents duplicates on timeout + retry. `task_id`
is optional but **recommended when you're currently working on a task** —
it links your note with all other notes + deliverable wrappers
of the same task (see "task bracket" below).

### Search the vault

```bash
# Full-text search across all notes + deliverable wrappers + extracted PDF text
curl "$MC_API_URL/api/v1/agent/vault/search?q=rate+limit" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"

# Filter by type=deliverable (only wrappers for files/screenshots/docs)
curl "$MC_API_URL/api/v1/agent/vault/search?q=weather&type=deliverable" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"

# Read a single note
curl "$MC_API_URL/api/v1/agent/vault/note/agents/{name.lower()}/lessons/2026-05-14-rate-limit-xai.md" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

### Vault files — deliverables as wrapper + attachment

Every task deliverable has a Markdown wrapper in the vault under
`/vault/agents/<slug>/deliverables/*.md`. The wrapper contains
frontmatter (`task`, `deliverable_id`, `attachment_path`, `attachment_mime`)
and embeds the actual file from `/vault/attachments/`. Search hits with
`type:"deliverable"` point exactly to these wrappers.

```bash
# Read the wrapper Markdown (e.g. after a vault-search hit)
Read /vault/agents/researcher/deliverables/weather-staufen-2026-05-15.md

# Read a PDF natively (with a pages parameter for long documents)
Read /vault/attachments/files/<deliverable-id>.pdf  (pages: "1-5")

# Read an image natively — interpreted as vision input, you SEE the image
Read /vault/attachments/images/<deliverable-id>.png
```

**For PDF wrappers, extracted text is under `## Auto-extracted`** —
often the wrapper Markdown is enough without opening the PDF yourself.

**NEVER edit binary files in `/vault/attachments/` in place.** If you
need a new version of a PDF/image:
1. Create a new wrapper (`<topic>-v2.md`) — the existing deliverable flow
   (`mc deliverable --type ...` or PATCH /api/v1/agent/me/deliverable)
   creates the wrapper automatically
2. In the frontmatter, `supersedes: [[<old-wrapper-id>]]`
3. Body section `## Predecessor` with a wikilink to the old version

### Task bracket — related notes + files

If you write wrappers, lessons, and memories with the same `task_id`,
you can find them all together later:

```bash
# All notes + wrappers + lessons of a task
curl "$MC_API_URL/api/v1/agent/vault/related/$TASK_ID" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

Use case: you open an old weather-report wrapper via search —
with `related` you immediately find the research lessons and memories
the previous agent wrote during that task.

### Folder discipline

- Own lessons/notes → `$AGENT_VAULT_PATH/lessons/` or `notes/`
- NEVER write directly into another agent's folder — the watcher rejects path-ownership violations
- Shared knowledge (decisions, project notes) → always use the inbox API with an explicit `target`"""

    # ── Memory section ─────────────────────────────────────────────────────
    memory_section = ""
    if _has(Scope.MEMORY_WRITE):
        memory_section = f"""## Update your own memory (persists between sessions)
PATCH $MC_API_URL/api/v1/agent/me/memory
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{{
  "content": "# {name} Memory\\n\\n## Learned\\n- ...\\n\\n## Conventions\\n- ..."
}}

Note: full content — not an append. GET /api/v1/agent/me/memory to read."""

    # ── mc remember (vault shortcut) ────────────────────────────────────
    vault_remember_section = ""
    if _has("vault:write"):
        vault_remember_section = """## mc remember — quickly note something

```bash
mc remember "What you learned"
mc remember "Title" --content "Body" --type knowledge
mc remember "Lesson" --tags "docker,restart" --type lesson
```

Shortcut for `mc vault-write`. Defaults: type=lesson,
auto-title from text, auto idempotency key, $TASK_ID from env."""

    # ── Heartbeat section ───────────────────────────────────────────────────
    heartbeat_section = ""
    if _has(Scope.HEARTBEAT):
        heartbeat_section = f"""## Report your own status
POST $MC_API_URL/api/v1/agent/heartbeat
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{{
  "status": "busy",
  "context_tokens": 45000
}}

Status: online | busy | idle | offline"""

    # ── Help request section ──────────────────────────────────────────────
    help_request_section = ""
    if _has(Scope.TASKS_HELP):
        help_request_section = f"""## Help request — ask other agents for help

If you need support for your task that's outside your
competence, you can file a help request. Your task
is paused automatically until the result comes back.

```
curl -s -X POST "$MC_API_URL/api/v1/agent/boards/{board_id}/help-request" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "needed_role": "researcher",
    "title": "Short description of what you need",
    "context": "Detailed context: exactly what, for what purpose, which format"
  }}'
```

Available roles: researcher, developer, writer, reviewer, deployer, planner, tester.
You get the result as a message and then continue.
IMPORTANT: Only use help requests when you're truly stuck.
Try yourself first before involving other agents.

## Ask a clarification question — ask the operator directly

If you need a decision or clarification from the operator, ask a
structured question. Your task is paused until the operator answers.

```
curl -s -X POST "$MC_API_URL/api/v1/agent/boards/{board_id}/clarification" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "question": "Your concrete question for the operator",
    "options": ["Option A", "Option B"]
  }}'
```

The `options` field is optional. Use it if you have suggested answers.
You get the operator's answer as a message and then continue."""

    # ── Browser reference (for all agents) ───────────────────────────────
    browser_section = """
---

## Browser reference

### dev-browser (primary — Playwright-based, sandboxed)

```bash
# Open a URL + check status
dev-browser <<'EOF'
const page = await browser.getPage("main");
await page.goto("URL");
console.log(JSON.stringify({ url: page.url(), title: await page.title() }));
EOF

# Analyze the page (element discovery)
dev-browser <<'EOF'
const page = await browser.getPage("main");
const result = await page.snapshotForAI();
console.log(result.full);
EOF

# Click an element
dev-browser <<'EOF'
const page = await browser.getPage("main");
await page.getByRole("button", { name: "Submit" }).click();
EOF

# Fill a field
dev-browser <<'EOF'
const page = await browser.getPage("main");
await page.fill("#email", "user@example.com");
EOF

# Viewport screenshot
dev-browser <<'EOF'
const page = await browser.getPage("main");
const buf = await page.screenshot();
const path = await saveScreenshot(buf, "screenshot.png");
console.log(path);
EOF

# Full-page screenshot
dev-browser <<'EOF'
const page = await browser.getPage("main");
const buf = await page.screenshot({{ fullPage: true }});
const path = await saveScreenshot(buf, "full-page.png");
console.log(path);
EOF
```

Screenshots are saved to `~/.dev-browser/tmp/`. Path comes from `console.log(path)`.

### Persistent browser for external sites (login sessions)

For sites that require login (X/Twitter, GitHub, etc.) use the Chrome instance on port 18800:
```bash
dev-browser --connect http://localhost:18800 <<'EOF'
const page = await browser.getPage("x");
await page.goto("https://x.com");
console.log(JSON.stringify({ url: page.url(), title: await page.title() }));
EOF
```

This browser runs permanently (LaunchAgent) and keeps sessions between runs.
Log in manually once → Henry then uses the saved session.

Rules:
- NO `openclaw browser` (pairing problem)
- External sites with login → `--connect http://localhost:18800`
- Local tools / public sites → regular `dev-browser` (own session)
- On 2x timeout: `dev-browser stop` → retry → then BLOCKED
- Named pages (`browser.getPage("name")`) persist between script runs
"""

    # ── Typical flows (role-aware worked examples) ───────────────────────
    # Goal: agents learn through concrete scenarios, not command dumps.
    # Each flow is a copy-paste-ready end-to-end sequence with real
    # example inputs and shows which commands in which order are
    # correct for a typical task type.
    #
    # Design rule: worked example = executed flow, not command list.
    # Comments in the block explain the purpose of each line.
    flow_blocks: list[str] = []

    # Universal: task lifecycle with concrete examples
    flow_blocks.append(
        "### Flow 1 — receive and complete a task (every role)\n\n"
        "```bash\n"
        "# 1. Get oriented: who am I, what is my active task\n"
        "mc me\n"
        "# → {\"id\": \"bc81...\", \"name\": \"...\", \"current_task\": {\"id\": \"c5e2...\", \"status\": \"inbox\"}, \"cli_skills\": [...]}\n"
        "\n"
        "# 2. Send ACK (task → in_progress). If the response is 409 \"In Progress → In Progress\":\n"
        "#    you were already ACKed (via poll.sh direct-dispatch) — just continue.\n"
        "mc ack\n"
        "\n"
        "# 3. Do the work (see role-specific flows below)\n"
        "\n"
        "# 4. Document progress (optional, for multi-step tasks)\n"
        "mc comment progress \"Update — phase 1 done. Starting phase 2.\"\n"
        "\n"
        "# 5. Close atomically: reflection + status in one call — `mc finish` is the ONLY\n"
        "#    correct close verb. `mc comment reflection ...` followed by `mc done` is the\n"
        "#    OLD two-step flow and no longer works: `mc done`/`mc review` alone are rejected\n"
        "#    with 400 unless a reflection was already posted in this same call.\n"
        f"mc finish \"{_reflection_skeleton}\"\n"
        "# → status: done. Use `mc finish --review \"...\"` instead for code/API/security tasks\n"
        "#   that need a reviewer to look before done.\n"
        "```\n"
        "\n"
        "**If you're unsure** → `mc blocked --blocker-type <type> --question \"What do you need?\"`:\n"
        "```bash\n"
        "mc blocked --blocker-type missing_info --question \"Which tone of voice? formal or casual?\"\n"
        "# Task status → blocked, the operator gets a Telegram question, you wait for an answer\n"
        "```\n"
        "Valid blocker_type: `missing_info` | `technical_problem` | `decision_needed` | "
        "`permission_needed` | `dependency_blocked` | `other`."
    )

    # Chat-write: reporting flow with concrete file examples
    if _has(Scope.CHAT_WRITE):
        flow_blocks.append(
            "### Flow 2 — report to the operator via Telegram\n\n"
            "```bash\n"
            "# Simple text report (Markdown supported)\n"
            "mc telegram \"**Status** — weather research done. 3 sources cross-validated, details in the deliverable.\"\n"
            "\n"
            "# With an image (e.g. screenshot, chart, mockup) — max 10 MB\n"
            "mc telegram \"Frontend mockup v2\" --photo /deliverables/$TASK_ID/mockup-v2.png\n"
            "\n"
            "# With a document (PDF, Word, Excel, ZIP) — max 50 MB\n"
            "mc telegram \"Weather report week 17\" --file /shared-deliverables/$TASK_ID/report.pdf\n"
            "\n"
            "# Visual verification (screenshot + metrics of a live URL)\n"
            "mc verify https://example.your-domain.com --caption \"Landing page deploy verified\"\n"
            "# → sidecar takes a Playwright screenshot + LCP/CLS and posts to Telegram automatically\n"
            "```"
        )

    # Tasks-write: deliverable + PDF flow with a real example
    if _has(Scope.TASKS_WRITE):
        flow_blocks.append(
            "### Flow 3 — register a result as a deliverable + generate a PDF\n\n"
            "```bash\n"
            "# Write a Markdown report (example: research deliverable)\n"
            "mkdir -p /deliverables/$TASK_ID\n"
            "cat > /deliverables/$TASK_ID/report.md <<'EOF'\n"
            "# Weather Report Week 17 Zurich\n"
            "\n"
            "## Summary\n"
            "It rains on Wednesday this week, dry otherwise.\n"
            "\n"
            "## Sources\n"
            "- wetter.com (fetched 2026-04-24)\n"
            "- meteoblue.com\n"
            "EOF\n"
            "\n"
            "# Register the deliverable in the DB (the operator + other agents see it in the UI)\n"
            "mc deliverable --type document --title \"Weather Report Week 17\" --path /deliverables/$TASK_ID/report.md\n"
            "\n"
            "# If the operator wants a PDF instead of Markdown: render it via the mc-playwright sidecar\n"
            "mc pdf /deliverables/$TASK_ID/report.md --title \"Weather Report Week 17\"\n"
            "# → /shared-deliverables/$TASK_ID/weather-report-week-17.pdf (for the Telegram attachment)\n"
            "\n"
            "# Save intermediate progress (so a container restart → task recovery has context)\n"
            "# `mc checkpoint` no longer exists — the checklist IS the recovery state now.\n"
            "mc comment progress \"Update — research done, PDF generated, starting Telegram send\"\n"
            "\n"
            "# Maintain the checklist for multi-step tasks\n"
            "mc checklist add \"Research complete\"\n"
            "mc checklist done <item-id>\n"
            "```"
        )

    # Tasks-create (orchestrator): delegation flow with parent/subtask
    if _has(Scope.TASKS_CREATE):
        flow_blocks.append(
            "### Flow 4 — orchestrate a multi-phase task (delegate + wait for callback)\n\n"
            "```bash\n"
            "# Parent task is \"Create a weather report with Telegram delivery\" → three phases:\n"
            "#   1. Research (Researcher)\n"
            "#   2. Content writing + brand skill (Shakespeare)\n"
            "#   3. PDF + Telegram (FreeCode)\n"
            "\n"
            "# Delegate phase 1 — atomic: creates a subtask + no longer blocks the parent\n"
            "mc delegate \"Research: 7-day weather Zurich\" \\\n"
            "  --to Researcher \\\n"
            "  --description \"Cross-validate at least 3 sources. Register a Markdown deliverable with min/max temperature + precipitation per day.\"\n"
            "# → subtask created with callback_agent_id=you\n"
            "\n"
            "# WAIT: you stay 'in_progress' (NOT blocked!). The subtask_completed comment arrives on the next poll.\n"
            "# If you set blocked: the operator can't meaningfully resolve the blocker (nothing for them to do),\n"
            "# the task hangs until manually unblocked. Callback waits are always in_progress.\n"
            "\n"
            "# After the subtask_completed comment: delegate phase 2 with a reference to the phase-1 deliverable\n"
            "mc delegate \"Content: formatted weather report with brand voice\" \\\n"
            "  --to Shakespeare \\\n"
            "  --description \"Use research deliverable <uuid> from phase 1. Skill: client-brand-skill. Formal address, primary color #005850.\"\n"
            "\n"
            "# Once all phases are done: status → review, final report to the operator\n"
            "mc telegram \"Multi-phase weather report complete. See deliverables.\" --file /shared-deliverables/$TASK_ID/final.pdf\n"
            "mc done\n"
            "```"
        )

    # Plugin management (Board Lead): complete discovery + install + assign flow
    if is_board_lead and _has(Scope.AGENTS_MANAGE):
        flow_blocks.append(
            "### Flow 5 — equip a worker with a new tool/skill (board lead)\n\n"
            "```bash\n"
            "# Scenario: Davinci fails a video task twice — likely a missing tool.\n"
            "\n"
            "# 1. Check: what does Davinci have today?\n"
            "mc plugin-show Davinci\n"
            "# → {\"agent_name\": \"Davinci\", \"cli_plugins\": null}  (null = all installed)\n"
            "\n"
            "# 2. Check: which plugins exist in the shared cache?\n"
            "mc plugin-list\n"
            "# → {\"plugins\": [{\"key\": \"higgsfield-mcp@anthropic-agent-skills\", ...}, ...]}\n"
            "\n"
            "# If the desired plugin is already there: assign it directly\n"
            "mc plugin-assign Davinci higgsfield-mcp@anthropic-agent-skills --restart\n"
            "# → plugin set in Davinci's cli_plugins, claude session reloaded in tmux\n"
            "\n"
            "# If NOT there: file an install request with operator approval\n"
            "curl -sf -X POST \"$MC_API_URL/api/v1/agent/install-requests\" \\\n"
            "  -H \"Authorization: Bearer $MC_AGENT_TOKEN\" \\\n"
            "  -H \"Content-Type: application/json\" \\\n"
            "  -d '{{\n"
            "    \"type\": \"mcp\",\n"
            "    \"operation\": \"install\",\n"
            "    \"source\": \"github:geopopos/higgsfield_ai_mcp\",\n"
            "    \"name\": \"higgsfield-ai\",\n"
            "    \"target_agent_id\": \"<davinci-uuid>\",\n"
            "    \"reason\": \"Davinci failed 2 video-tasks — needs Higgsfield MCP\",\n"
            "    \"task_id\": \"'\"$TASK_ID\"'\"\n"
            "  }}'\n"
            "# task_id coupling → on approval, the backend posts an install_completed comment on YOUR task.\n"
            "# You wait in_progress until the comment arrives. The InstallExecutor sets cli_plugins automatically.\n"
            "\n"
            "# Manually reload the worker (if the plugin was assigned without --restart)\n"
            "mc worker-restart Davinci\n"
            "```"
        )

    # Knowledge/Memory: Semantic Search + Write Flow
    if _has(Scope.KNOWLEDGE_READ):
        flow_blocks.append(
            "### Flow 6 — find context from earlier tasks (knowledge base)\n\n"
            "```bash\n"
            "# Semantic search across Qdrant + board memory\n"
            "mc memory \"client brand guidelines primary color\"\n"
            "# → top-K similar entries with content + score + memory_type\n"
            "\n"
            "# If you learned something important: write it back\n"
            "curl -sf -X POST \"$MC_API_URL/api/v1/agent/knowledge\" \\\n"
            "  -H \"Authorization: Bearer $MC_AGENT_TOKEN\" \\\n"
            "  -H \"Content-Type: application/json\" \\\n"
            "  -d '{{\n"
            "    \"content\": \"Dispatch messages >8000 chars are counterproductive (lost-in-middle)\",\n"
            "    \"memory_type\": \"lesson\",\n"
            "    \"scope\": \"board\"\n"
            "  }}'\n"
            "# scope: \"agent\" = only me | \"board\" = everyone on the board | \"global\" = all agents\n"
            "```"
        )

    quick_ref = "\n\n".join(flow_blocks)

    # ── Assemble ───────────────────────────────────────────────────────────
    sections = [s for s in [knowledge_section, vault_section, vault_remember_section, memory_section, heartbeat_section, help_request_section, install_request_section, credentials_section, deploy_section] if s]
    main_body = "\n\n".join(sections)

    return f"""# {emoji} {name} — Mission Control Tools

## Authentication

All requests need the Authorization header:
  Authorization: Bearer $MC_AGENT_TOKEN

API base: http://localhost

---

## Typical flows — copy-paste-ready tool call examples

Concrete scenarios with real inputs. Each flow is an end-to-end sequence:
which commands in which order for which situation. The raw-curl
form of every endpoint is further below in the detail sections.
Overview of all `mc` commands: `mc --help` or `mc <cmd> --help`.

{quick_ref}

---

{main_body}
{board_section}{content_section}{board_lead_section}{browser_section}
---
Generated automatically when the agent is created.
"""
