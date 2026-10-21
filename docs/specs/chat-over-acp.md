# Chat over ACP — headless chat for ACP-driven agents

Status: accepted 2026-09-13 (Mark: "alle Funktionen über die Chat-UI, kein Terminal-Tab bei ACP-Agenten"; Boss: Fehler müssen als sichtbares Ereignis im Chat landen).

## Problem

Agents with `OMP_DRIVER=acp` run their **tasks** through `omp acp` (one process per attempt, `bridge.run_acp_once`), but the Sessions **chat** still types into the native TUI in tmux window 0 (`agent_chat_input.send_text` → `tmux send-keys`). Two consequences:

1. The chat and the task path are two different brains — what the operator types never reaches the ACP session, and the Terminal view shows an idle second console.
2. Everything the composer offers (effort chip, `/model`, slash palette, Stop) is implemented as key presses against a TUI that, for an ACP agent, should not exist.

Hermes (host runtime) has no chat at all: the bridge drives a `hermes --yolo` TUI in tmux and the Sessions page shows nothing.

## Goal

For an agent whose driver is ACP, the Sessions chat is the **only** interface and it is complete: prompts, streaming preview, cancel, effort (thinking level), model switch, slash commands, and **errors** all travel over ACP. The Chat/Terminal toggle is not rendered for such agents (`headless_chat`). The same contract serves omp (container) and Hermes (host).

## Non-goals

- Multi-turn concurrency. One turn at a time per agent chat session. A second prompt while busy is rejected with `busy` — unless it asks for `mode: "queue"` (the chat does): then the daemon holds it and runs it when the running turn ends (FIFO, see the op table). Steering INTO a running turn is not possible over ACP: omp cancels the running turn when a second `session/prompt` arrives (omp 18.1.10 `AcpAgent.prompt`); steering needs omp's RPC mode (`steer`/`follow_up`).

Done, no longer a non-goal (fix omp-acp-no-tui-window, 13.09.2026): the native
TUI in window 0 was removed for `OMP_DRIVER=acp` agents — Mark saw the live
TUI as an idle "ghost" session in the Terminal view and does not want that.
Window 0 itself still exists (Boss: keep the Terminal reachable until the
live proof) but shows an `OMP_ACP_READY` banner + a plain shell instead of
launching omp; the health gate and the recycler were updated to match (see
`docker/omp-bridge/entrypoint.sh` / `omp-recycler.sh`, and
`agent_runtime_switch.OMP_READY_SIGNALS` on the backend side). `?view=terminal`
still works as a deep link.

## Architecture

```
Sessions chat (frontend)
   │  POST /agents/{id}/chat/input | keys | effort | GET history
   ▼
backend  agent_chat_input.py ── headless_chat? ──► ChatTransport
   │                                              ├─ DockerCtlTransport  (docker exec mc-agent-<slug> python3 /opt/omp-bridge/acp_chat_ctl.py <op>)
   │                                              └─ HttpCtlTransport    (POST http://host.docker.internal:18794/chat/<op>)   ← Hermes
   │
   ▼ reads (mount ~/.mc/agents/<slug>/omp-sessions/<encoded-cwd>/)
.mc/*.jsonl (transcript copy)  ·  .mc/previews/*.jsonl  ·  .mc/media/  ·  acp-chat-state.json
   ▲
   │ writes
acp_chat.py --serve   (long-lived chat daemon; one `omp acp` / `hermes acp` child, one ACP session, session/load on restart)
   ▲ Unix socket  $OMP_HOME/acp-chat.sock  (JSON lines request/response)
acp_chat_ctl.py <op>  (CLI shim used by docker exec)      ·      hermes-bridge.py /chat/<op> (HTTP shim on the host)
```

### Component: `docker/omp-bridge/acp_chat.py` (chat daemon)

- `ChatSession(client_factory, cwd, state_dir, sessions_dir)` — pure-Python core, testable with a fake ACP process (`tests/fake_acp_server.py`) or an in-memory fake client.
- Startup: move MC copies an older image left next to omp's files into `.mc/` (see "Storage" below); find the session to resume — the pointer `acp-chat.json` (`{"sessionId", "transcript"}`), else the state mirror (see "What survives what" below) — and follow it to a moved file (`sink_migration.remap`); `initialize`; try `session/load` with the stored id → on success append to the stored transcript file; when the load fails or the transcript is gone, `session/new`, a new transcript file and a visible `chat_error{code:"session_reset"}` — after a failed load (the old copy is still on disk) preceded by `chat_notice{code:"new_conversation"}` (see "A new conversation says so" below). No pointer at all, but an earlier chat copy in the tree → `chat_notice{code:"chat_started_fresh"}`. When the move did not run (lock held by a live run, unreadable list, error) the chat shows a `chat_notice` with `code: "sink_migration_skipped"` and the chat starts anyway. Before anything rewrites the state mirror, the start also reads what the previous daemon was doing: held jobs (`queue_lost`) and a reply it was running (`turn_interrupted`, see "Interrupted turns" below).
- Uses `acp_chat_events.ACPEventMapper` + `ChatEventSink` + `PreviewEventSink` exactly like `run_acp_once` (user line via `map_user_prompt`, streaming previews, ONE final assistant line with usage).
- Thinking (20.10.2026): a thought's preview line carries `data: {"block": "thinking", "chars": <full size>, "startedAt": <iso>}` and only the END of the thought as `content` (≤ 2 000 chars — the file grows linearly). The backend passes these on the `preview` SSE event; the chat shows "Thinking · ≈N tokens · m:ss" instead of an answer preview. When the thought ends (next answer chunk, tool call, plan, the turn's final line or a chat error) ONE assistant line with a `{"type": "thinking", "thinking", "durationMs"}` block lands in the transcript. Usage/config updates do not end a thought.
- Running task in the agent chat (20.10.2026): the agent chat shows the chat daemon's session only (#898); a task runs in a session of its own (bridge header `taskId`). `GET /agents/{id}/chat/task-activity` reads that session through the same adapter (`find_task_session`, `tasks_in_own_session`) and the chat renders it as a separate "From the task" block — delivery, the newest steps, the running thought, more inbox tasks. Harnesses whose task runs inside the chat answer `applies: false` — Claude Code, omp under the native driver, and Hermes (over ACP the hermes-bridge hands a task to the chat daemon itself).
- `configOptions` (from `session/new`/`session/load` result and `config_option_update` notifications) and `available_commands_update` are kept in memory and mirrored to `sessions_dir/acp-chat-state.json` after every change.
- Permissions: `OMP_ACP_PERMISSIONS` policy (yolo → allow), same helper as the bridge.
- Errors → transcript `custom_message` line, `customType: "chat_error"`, content = human text, plus `data: {"code": ..., "detail": ...}`. Codes: `rpc_error` (JSON-RPC error on prompt/config), `provider_error` (`401`/`403`/`429`/quota text detected in the error or in the final agent text when the turn produced no text), `empty_turn` (turn ended with stopReason `end_turn` and zero agent text), `process_exit` (child died; daemon restarts it and re-loads the session — `data.retryText` carries the operator's text of the turn that died, when its reply never came), `turn_interrupted` (the daemon restarted while a reply ran; `detail` is `turn <n>` or `turn unknown`, `data.retryText` the operator's text — see "Interrupted turns"), `model_unreachable` (the model's endpoint does not answer, or the engine stopped mid-request — omp's own error line says so, or the prompt failed with it; `detail` is omp's line, `data.retryText` the operator's text; decided before `provider_error`, `rpc_error` and `empty_turn` — see "The model is not answering"), `busy`, `session_reset` (the conversation to resume could not be opened; `detail` is `transcript_missing` when its transcript is gone, `model_missing` when omp cannot restore the session's model — the content then names the model, and `data.model` carries the name for the card in the UI language —, otherwise the exception), `queue_full` (20 messages already held), `new_session_failed` (`session/new` refused; the current session continues), `queue_lost` (a restart dropped held jobs, and a `/new` that was running), `cancelled` is NOT an error (it produces a normal end-of-turn), and neither is a turn omp's own line calls aborted. Extra fields of a card go through `ACPEventMapper.map_chat_error(code, detail, text, data=None)`: they never replace `code` or `detail`, a `None` value is left out, and without `data` the line is exactly the one before S1-R5.
- Model pin (incident 19.10.2026): every `session/new` and `session/load` pins the selector read from the CURRENT `omp.env` (`acp_model.current_selector` — a runtime switch re-renders that file in the running container; the daemon's boot env goes stale). A selector the session does not offer is replaced by the offered model of the same provider and the chat gets a `custom_message` line `customType: "chat_notice"`, `data: {"code": "model_fallback", "previous", "current"}` (frontend `ChatNoticeCard`, `chat.notice.*`). The task path (`bridge.run_acp_once`) does the same, writes a refused pin into the task's chat as `chat_error`, and marks its prompts `attribution: "mc"` + `origin: {kind: "task"|"nudge"}` so the chat shows them as task events, not as operator bubbles.
- Socket server: `$OMP_HOME/acp-chat.sock` (path configurable via `--socket`), newline-delimited JSON; each connection = one request/one response.

### Storage: MC's copy lives in `<folder>/.mc/` (S1-R3, 20.10.2026)

A session folder (`<sessions root>/<encoded cwd>/`, e.g. `--workspace--`) holds
omp's own session files and MC's state mirror `acp-chat-state.json`. Everything
else MC writes for the chat lives one level deeper:

| What | Where |
|---|---|
| transcript copy (`ChatEventSink`, the file the chat view reads) | `<folder>/.mc/<ts>_<sessionId>.jsonl` — header unchanged (`type`, `version`, `id`, `timestamp`, `cwd`, `bridge: "acp"`, `taskId`) |
| preview channel (`PreviewEventSink`) | `<folder>/.mc/previews/` — always next to the transcript copy in use (`PreviewEventSink.beside`): an old copy the daemon resumed in place because the move did not run keeps its preview in `<folder>/previews/`, where the backend looks for it (`omp_chat.preview_channel`) |
| tool pictures (`write_media`) | `<folder>/.mc/media/` (next to the transcript copy) |
| state mirror | `<folder>/acp-chat-state.json` (unchanged) |
| pointer to the open conversation | `<folder>/acp-chat.json` in the container since S1-R4 (before: the unmounted `$OMP_HOME`); Hermes: its workspace |

**Why a folder, and why this one.** omp 18.10.1 treats every `*.jsonl` whose
first line (or second, after a title line) is `{"type": "session", "id":
<string>}` as one of its sessions. It lists them with `Bun.Glob("*.jsonl")`
per folder (`SessionManager.list`) and `Bun.Glob("*/*.jsonl")` over the root
(`listAll`), and load / resume / fork take the **younger** of two files with
one id. MC's copy carries exactly that header, so before S1-R3 omp listed
every chat session twice and often loaded MC's copy instead of its own file
(live 20.10.2026: "ACP session load was cancelled" for the current chat; the
daemon then started a new session without a word). `<folder>/.mc/x.jsonl`
lies two levels below the root, outside both globs — that alone keeps omp
18.10.1 away (checked with omp 18.10.1 on a copy of a session tree: with MC's
copies in `.mc/`, `session/list` with and without `cwd` showed only omp's
files). The leading dot is a second guard in case a later omp searches
recursively: Bun's docs say `Bun.Glob` skips dot names unless `dot: true`,
and a local Bun 1.3 does so — not checked at the Bun build inside omp's
binary, so it is an assumption, not a proof.
Changing the header would mean rewriting old files, and a new extension would
change every `*.jsonl` reader — moving the file changes neither.

**Contract.** `acp_chat_events.MC_SINK_DIRNAME` and the backend's
`omp_chat._MC_SINK_DIRNAME` are both `".mc"` (checked as text by
`backend/tests/test_mc_sink_dirname_contract.py` — backend and bridge share no
code). `acp_chat_events.find_sink(folder, sessionId)` returns MC's copy:
`.mc/` first, then the old place, never omp's own file (only a header with
`bridge: "acp"` counts; never blocks on a FIFO named like a session). The
backend reads both places, so it must be deployed before a bridge that
writes to `.mc/`. `.mc` must be a real folder: a symlink there is refused
(the sinks write nothing), never followed. The chat daemon looks up
tool names in omp's own file in the session folder and never takes a file
with MC's header for it.

**Moving old copies** — `docker/omp-bridge/sink_migration.py` (standard
library only, Python 3.9+, the same file in the image and in a checkout):

- In each folder directly under the sessions root (no dot folders, no
  symlinks) every `*.jsonl` directly in the folder with `bridge: "acp"` in its
  header moves to `<folder>/.mc/<same name>`; `<folder>/media/*` moves to
  `<folder>/.mc/media/` — only in a folder that holds an MC copy (moved now
  or before), so a wrong `--root` moves no unrelated pictures. One rename
  each, on the same file system, so the modification time stays (the
  backend ranks sessions by it).
- It never leaves the root. The session folders are the agent's own mount,
  so the agent can plant a symlink there, and the operator runs the move on
  the host with the host's rights. Every folder between the root and a
  source or target must be a real folder — checked when planning and again
  at each rename; a `.mc` or `.mc/media` that is a symlink or a file skips
  that folder with reason `unsafe_target`. Only regular files move. The list
  is written through a fresh `O_EXCL` temp file; a list or lock that is a
  symlink or a FIFO is never followed or waited on.
- Never: delete, overwrite (an existing target is skipped with a reason),
  touch a subfolder (`previews/`, omp's artifact folders — also those with
  MC's name pattern), a dot file (omp's `.<name>.jsonl.lock.os`),
  `acp-chat-state.json`, `acp-chat.json` or omp's files, or create a `.bak`
  name (omp renames `*.jsonl.<x>.bak` back into sessions).
- A copy omp also wrote into ("mixed file", three on the fleet from
  16.09.2026) moves like any other and is marked `mixed` with the count and
  types of omp's lines.
- The list `<root>/.mc-sink-migration.json` (never `.jsonl` — the backend's
  `*.jsonl` search would read it as a session) records a run that moved
  something; a command-line run also when it only skipped something (a
  refused move stays on record). A run with nothing to do is not recorded,
  nor a daemon run that only skipped (it would repeat at every start). Per
  file `{from, to, sessionId, task, mixed}`, paths relative to the root (host
  and container see different absolute paths). It is rewritten atomically,
  written before the first move and again after the last, so a crash in
  between stays traceable. A list that cannot be read stops `--apply` and a
  plain `--rollback`.
- The lock `<root>/.mc-sink-migration.lock` (`O_EXCL`) holds pid, time and
  host. Older than 10 minutes, or of a dead process on the same host → taken
  over; otherwise the run stops with `locked`. A lock time more than a
  minute in the future is garbage: the file's own time counts instead. A
  lock that cannot be created or removed (permissions) is `error`, not
  `locked`. The chat daemon plans read-only first and takes the lock only
  when something is to move.
- `rollback` moves each listed file back when its old place is free;
  `--all` also moves back copies written into `.mc/` after the move, and
  `.mc/media/*`. `--all` needs no list (it finds MC's copies by their
  header): with a damaged list it still runs, leaves that list untouched and
  records its run in a fresh `.mc-sink-migration.<time>.json` beside it.
  `remap(path)` sends an old pointer to the moved file (and a pointer into
  `.mc/` back after a rollback).
- Command line: `python3 sink_migration.py --root <sessions root>
  (--dry-run | --apply | --rollback [--all]) [--json]`; exit 0 ok, 1 error,
  3 locked.

**When it runs.** The real move runs on the host in a deploy window, with the
agent stopped and no task in progress: `--dry-run` (counts only), then
`--apply`, numbers checked, only then the new image and the recreate. The
chat daemon runs the same move at every start, before it reads its pointer —
only a safety net. Switched-off agents move on their next **recreate** with
the new image, not on `docker start` (that starts the old image's code); the
backend reads both places until then. Hermes moves its copies at the next
start of its chat daemon. Way back: stop the agent, `--rollback --all` on the
host, then the previous image.

### What survives what (S1-R4, 20.10.2026)

The daemon remembers the open conversation in the pointer `acp-chat.json`
(`{"sessionId", "transcript"}`). In the container it lives in the session
folder next to `acp-chat-state.json`, on the mount
`~/.mc/agents/<slug>/omp-sessions` (`build_session` sets
`state_dir = sessions_dir`). Before S1-R4 it lived in `$OMP_HOME`, which is
not mounted: every recreate (each new image) threw it away and the chat
started over without a word. Hermes keeps its pointer in its workspace.

**Which conversation a start resumes** (`ChatSession._read_persisted`) — the
first source that names a session id **and** a transcript wins:

1. the pointer `<folder>/acp-chat.json`;
2. the state mirror `<folder>/acp-chat-state.json` — the way over the deploy
   of S1-R4: the old image kept its pointer in `$OMP_HOME`, but already wrote
   this file on the mount.

One exception: a state mirror that is **strictly newer** than the pointer and
names **another** session wins over the pointer (logged as "following the
state mirror"). Every session switch (start, `/new`, reset) writes the pointer
**before** the state mirror names the new session — in the same locked step as
the switch, so no socket thread can write the mirror in between. A newer
mirror naming another session therefore means the pointer stopped being
written: a rollback to an image before S1-R4 (it writes only the mirror) and a
forward deploy after it, or a pointer write that failed (only a log line).
Reopening the pointer's session then would silently undo the conversation the
operator had open last. A newer mirror naming the same session is normal
operation and changes nothing.

`$OMP_HOME/acp-chat.json` is never read: a new image always means a recreate,
so it is empty there, and a stale one must never win over the mirror. The
transcript is then followed to `.mc/` (`sink_migration.remap`); a path the
file system refuses (too long, a folder the daemon may not enter) counts as a
missing transcript, never as a crash at boot. Fields of the pointer the daemon
does not know (P4 adds `homeSessionId`) stay when it is rewritten.

| What happens | The open conversation |
|---|---|
| restart in the same container (`docker restart`, or the chat daemon started again) | goes on: `session/load` from the pointer, same transcript file |
| recreate (new image, `compose up --force-recreate`) | goes on: the pointer is on the mount (the first start after the S1-R4 deploy takes the state mirror) |
| the omp child dies (`process_exit`) | goes on: the daemon respawns it and loads the session it had in memory; the replay stays out of the chat (`_muted_replay`) |
| rollback to the image before S1-R4, then forward again | goes on with the conversation that was open last: the newer state mirror wins over the pointer from before the rollback |
| any of the three after a **model switch** since the last turn | a new conversation and the card `session_reset` / `model_missing`, naming the old model (in the text and as `data.model`) |
| the pointer's transcript is gone (also not in `.mc/`), or the file system refuses its path | a new conversation and the card `session_reset` / `transcript_missing` |
| no pointer and no state mirror, but an earlier chat copy (`bridge: "acp"`, no `taskId`) in the tree | a new conversation and the notice `chat_started_fresh` |
| a brand-new agent, or only task copies / omp's own files in the tree | a new conversation, no card |
| any restart or recreate **while a reply runs** | the conversation goes on, the running reply is lost: card `turn_interrupted` with the operator's text to send again (a crash of the omp child: `process_exit` with the text) — never resent on its own (see "Interrupted turns") |

In one sentence: **the conversation goes on as long as the model is the same
as at its last turn; otherwise there is a visible card.** omp (since 18.6.3)
refuses `session/load` under ACP with "Could not restore model
`<provider>/<id>`" when the session's last model is not in `models.yml` —
without a UI there is no fallback to another model. A runtime switch renders
only the new model into `models.yml`, so every model switch makes the open
conversation unloadable after a restart, recreate or crash (ADR-086, harness
and runtime switchable at any time). How a conversation continues on another
model is decided in P4; until then the card says what happened and the old
files stay on disk.

The pictures of a conversation (omp's `blobs/` folder) survive a recreate
only with the mount from S1-R8 (`docker/omp-bridge/README.md`, "What lives
where").

### Interrupted turns (S1-R5, 20.10.2026)

A restart of the chat daemon (`docker restart`, a recreate, a bridge reload,
Hermes' `ChatDaemon.restart`) ends a reply that is running. omp gets no
second chance to finish it, and the new daemon never sends it again on its
own: a message the model already half-answered — maybe with tool calls that
changed files — must not run twice without the operator deciding it.

- Who wrote a prompt is part of the request: `origin` (control protocol
  below). `"operator"` is the operator's own message — the composer.
  `"task"` / `"nudge"` mean Mission Control wrote it (the names of the task
  path); the chat line is then an MC event (`attribution: "mc"`), not an
  operator bubble. Anything else, or no `origin` at all, is unknown and
  shown as before. **Only the operator's text is ever offered to send
  again** — fail-closed: hermes-bridge's dispatch and comment prompts and
  the omp bridge's wake-up go through the same session, and hermes-bridge
  delivers a task again on its own after a restart. One tap on its dispatch
  text (with an attempt id that may be stale by then) could run the task a
  second time. A caller that forgets to say who wrote a text costs a
  button, never a second run. State 20.10.2026: no caller sends `origin`
  yet — the backend's composer path (`acp_chat_transport.prompt`) and
  Hermes' `ChatDaemon.request` have to pass `"operator"` on before any card
  offers "Send again"; hermes-bridge's `ChatDaemon.prompt` and the omp
  bridge's wake-up may then say `"task"` / `"nudge"` (follow-up).
- While a job runs, the state mirror carries
  `current: {"kind": "prompt" | "new_session", "turn": n | null, "text": "…" | null, "truncated": bool, "startedAt": "…", "origin": "operator" | "task" | "nudge" | null}`,
  set when the job is claimed (`_claim_locked`), `null` when the job ends;
  the next job from the queue becomes `current` (a held job keeps its
  `origin`). `text` is the whole prompt — the same protection class as the
  held texts in `queue` — or `null` with `truncated: true` when it has more
  than 8000 characters.
- A daemon that stops mid-turn leaves `busy: true` and `current` on disk. A
  closed session writes nothing more — no state mirror, transcript, preview
  or pointer — from any thread: its ACP reader still hands over buffered
  updates while `close()` waits for the child, and one of them rewriting
  the mirror with `busy: false` would hide the cut-off reply. The next
  `start()` reads the mirror before anything rewrites it:

| previous mirror | the chat shows |
|---|---|
| `busy: true`, `current.kind == "prompt"`, `current.origin == "operator"` | ONE `chat_error` card `turn_interrupted` ("The chat restarted while a reply was running — nothing was resent automatically."), `detail: "turn <n>"`, `data.retryText` = the operator's text |
| `busy: true`, `current.kind == "prompt"`, any other `origin` | the `turn_interrupted` card ("The chat restarted while a reply was running.") — **no** `retryText`, no claim about resending |
| `busy: true`, `current.kind == "new_session"` | no card of its own: `/new` is listed first on the `queue_lost` card |
| `busy: true`, no `current` (an image before S1-R5) | the `turn_interrupted` card with `detail: "turn unknown"`, the sentence without the claim, and **no** `retryText` |
| `interrupted` set (a start before died before its card) | the same as for `current` |
| `busy` not `true` and no `interrupted` (idle, missing, unreadable) | nothing |

- The interrupted job is never used up before its card is written: from
  the moment `start()` reads it until the card is on disk, the mirror keeps
  it as `interrupted` (the same shape as `current`), although `busy` is
  already `false` again. A stop in between shows the card on the next
  start; a stop right after the card and before the mirror is rewritten
  shows it once more — twice, never not at all. The held queue (`queue_lost`)
  and open questions do not have this yet: a stop in that window loses
  their card, as before S1-R5.
- The omp child dying mid-turn is the other way a reply is cut off: the
  `process_exit` card carries `data.retryText` too — only for the
  operator's text, and only when the reply never came (a child that exits
  right after a complete reply leaves nothing to send again). The respawn
  only reloads the session.
- `retryText` is only ever the **whole** text, never a shortened one: an
  empty text or one longer than 8000 characters gets no `retryText` (the
  backend applies the same rule, `_RETRY_TEXT_MAX_CHARS`). The frontend's
  "Send again" button sends it through the normal send path, once, and only
  on the newest such card (S1-R7).
- No code path sends a prompt from `start()` or from the child restart.

### The model is not answering (S1-R6, 20.10.2026)

When the model's endpoint is gone (the engine stopped, the box is off),
omp 18.10.1 retries, gives up and sends its error line as ordinary text
(`agent_message_chunk`). The ACP stop reason is then `refusal` (omp's
pattern `refus(al|ed)` matches "ECONNREFUSED") or `end_turn` ("Unable to
connect …" without ECONNREFUSED) — never `error`. Before S1-R6 the chat
showed that line as the agent's answer. Now it is a card.

What the ACP text carries is not omp's final line word for word (live,
13 of 13 MC copies of such a turn, 20.10.2026): after retries omp's file
ends with `Retry budget exhausted after 10 retries: <line>`, while the
text carries only `<line>` — the message of the attempts it retried —
and in 3 of the 13 it is glued onto the model's last sentence without a
separator (`… the CLI.Unable to connect. …`).

- **The signal is omp's own session file** (the file named after the
  session, next to `.mc/`). omp writes every failed turn there as an
  assistant line with `stopReason: "error"` and `errorMessage`; an attempt
  it retried carries `retryRecovery.status` `superseded` or `recovered`, an
  abort is `stopReason: "aborted"`. After the turn the daemon reads what
  omp wrote since the job was claimed and takes the turn's final line: the
  last message line, when it is an assistant line that ends a turn (not a
  tool call, not a retried attempt, not followed by a user line). Whether
  omp's line is on disk when the prompt reply arrives is not proven, so the
  read tries 5 × 100 ms. It reads only for a turn whose text names a lost
  model or a stopped engine, or that has no text: an ordinary answer never
  waits.
- **omp's line is cut off the END of the reply**, in any form it takes:
  the final `errorMessage`, the same without the `Retry budget exhausted
  after N retries:` wrapper, or the message of an attempt omp retried in
  this turn. Whitespace does not count, the cut repeats while a form is
  still at the end, and the same words earlier in the text stay. What is
  left is the reply bubble; nothing left = no bubble.
- **One pattern** for the chat and the task path: `MODEL_UNREACHABLE_RE`
  and `UNREACHABLE_TAIL_CHARS = 300` live in `docker/omp-bridge/model_errors.py`
  (standard library, Python 3.9+); `bridge.py` (#897, tasks) imports them.
  The chat adds ONE narrower pattern, `MODEL_STOPPED_RE` ("stream timed
  out", "stream stalled", "socket connection was closed" — the engine took
  the request and stopped answering; live 19 final error lines, all with
  0 output tokens), read **only** from omp's own `error` line: in a model's
  text these words are far too common for the text rule, and the task path
  keeps its own handling of them (a transient error, the completion
  contract).

| the turn | the chat shows |
|---|---|
| omp's final line is `error`, its `errorMessage` matches `MODEL_UNREACHABLE_RE` or `MODEL_STOPPED_RE`, the reply text is only omp's line (in any of its forms) | **no** reply bubble, ONE `chat_error` card `model_unreachable` |
| the same after the model's own words (with or without a break before omp's line) | the bubble shows the model's words without omp's line, plus the card |
| the prompt fails with a matching error ("Internal error: Connection error.") | the card `model_unreachable` (before S1-R6: `rpc_error`) |
| omp's final line is `aborted` ("Interrupted by user", "Operation aborted") | no card at all, like Stop (no `empty_turn` either) |
| omp's final line is `error` with any other message (401, 429, an `error` line that merely says "Request was aborted") | as before S1-R6 (no text: `empty_turn`) — 401/429 get their own card in a later step |
| omp's final line is a real answer | as before: a model that talks about a refused connection or a timed-out stream stays an answer |
| no final line from omp (no file, unreadable, nothing written in time; Hermes has no omp file) | the text rule, `MODEL_UNREACHABLE_RE` only: the END of the text (300 characters) matches AND the stop reason is `refusal` OR the reply carries no `usage` at all (omp sends none for a turn without tokens). The bubble is hidden only without `usage` (tokens mean the model spoke) and only when the card's `detail` (240 characters) holds the whole text — a short reply the rule misreads (a Hermes slash command sends no `usage`) is moved into the card, never lost |

- The card: content "The model is not answering — check the Runtimes
  page. Your message is not lost.", `data: {"code": "model_unreachable",
  "detail": <omp's line, at most 240 characters>, "retryText": <the
  operator's whole text>}`. `retryText` follows the rules of
  `turn_interrupted`: only for `origin: "operator"`, only the whole text
  (at most 8000 characters). For any other origin the card has no
  `retryText` and says only "The model is not answering — check the
  Runtimes page." The frontend links the card to the Runtimes page (S1-R7).
  Today no caller sends `origin: "operator"` yet (the backend's composer
  path and the hermes-bridge send none), so every card is the neutral one
  without "Send again" until that wiring exists — the same holds for
  `turn_interrupted` (S1-R5).
- No chat state "waiting for the model": the turn is over and the card
  says it. `waiting_model` stays a phase of the task channel (#897).
- **Deliberate difference to the task path:** the task path has no omp line
  to ask; it reads the END of the turn's text, guarded by the completion
  sentinel. A model's own answer whose end talks about a refused
  connection is "unreachable" there and an answer in the chat; an engine
  that stopped mid-request (`MODEL_STOPPED_RE`) is the card in the chat and
  the task path's own handling there. For every #897 template with omp's
  error line, and for the live shapes (the attempt's line, glued onto the
  model's words), both say the same — the parity table in
  `docker/omp-bridge/tests/test_acp_chat_model_unreachable.py`.

### A new conversation says so, and who asked is on record (21.10.2026)

Incident: an operator's chat got a new, empty session (`new_session` job via
`/new` over chat input). Nothing in the chat said a new conversation had
begun, nothing durable named who sent the command, and the old session sat
untouched on disk.

- **Card in the new conversation** (`acp_chat.py`,
  `_note_new_conversation`): every conversation the daemon opens in place of
  another one starts with ONE `chat_notice` line, `data: {"code":
  "new_conversation", "previous": <old session id>, "current": <new session
  id>, "via", "reason"?}` — its FIRST line, never in the old transcript.
  `via`: `new_session` (the control op: `/new`, `/clear`), `start` (a daemon
  start that could not resume: daemon restart or recreate), `child_restart`
  (the omp child died and the respawn could not resume). `reason` for a
  failed resume: `load_failed`, `model_missing`. The red `session_reset`
  card still follows and says why.
- The card promises "the previous one is saved", so it is written only while
  the previous conversation's copy is on disk: `transcript_missing` keeps its
  red card alone; a start without a pointer keeps `chat_started_fresh`.
- No reset, no card: a normal turn, a successful resume, a brand-new agent
  and a refused `/new` (`new_session_failed`) write none.
- **State mirror `lastReset`**: `{"at", "via", "reason"?,
  "previousSessionId"?, "sessionId"}` of the last reset (also
  `transcript_missing` and `no_pointer`), taken over from the previous mirror
  at start so a restart does not hide it.
- **Frontend** (`ChatNoticeCard`): `new_conversation` and
  `chat_started_fresh` share the label "New conversation started" / "Neues
  Gespräch begonnen" and show the line's clock time (with the day when it was
  not today); `new_conversation`'s sentence is "The previous conversation is
  saved." The card has an `action` slot for P4's "show previous" link; the
  session ids are data for it, never text.
- **Who asked** (backend): `send_text` returns a `ChatReset` (`command`,
  `previous_session_id`, `queued`) when it turned `/new`/`/clear` into
  `new_session`; `POST /agents/{id}/chat/input` records it as the activity
  event `chat.session_reset` (`agent_chat_input.record_session_reset`):
  `detail: {"via": "chat_input", "command", "actor": {"kind": "user" |
  "token" | "service", "name", "user_id", "acting_as"?, "auth"},
  "previous_session_id", "new_session_id", "queued"}`. A token that acts as
  the first admin (`LOCAL_AUTH_TOKEN`, the MCP service JWT) is named as the
  token, with `acting_as` — never passed off as the operator. The new session
  id is added by a bounded follow-up (120 s) that reads `lastReset` from the
  state mirror; without it, the event keeps `new_session_id: null`. A refused
  reset writes no event.
- Not here: resets the daemon does on its own (`start`, `child_restart`) are
  on its card, in `lastReset` and in its log, not yet in the activity feed.

### Control protocol (socket and HTTP share the schema)

| op | request | response |
|----|---------|----------|
| `prompt` | `{"op":"prompt","text":"...","mode"?:"now"\|"queue","origin"?:"operator"\|"task"\|"nudge"}` — `origin`: who wrote the text (see "Interrupted turns"); only `"operator"` makes it offerable again | `{"ok":true,"turn":n}` immediately (turn runs async) · busy + `mode:"queue"`: `{"ok":true,"queued":true,"queueId":"…","position":n,"at":"…"}` · busy otherwise: `{"ok":false,"error":"busy"}` · `{"ok":false,"error":"queue_full"}` |
| `new_session` | `{"op":"new_session","mode"?:"now"\|"queue"}` | `{"ok":true,"previousSessionId":"…"}` — `session/new` runs in the worker; a new transcript file starts (the chat view follows it as a rollover), the old file stays, model + thinking carry over, the old ACP session gets `session/close`, the new transcript starts with a `new_conversation` card. Busy: `{"ok":false,"error":"busy"}`, or held in the queue with `mode:"queue"` |
| `queue_clear` | `{"op":"queue_clear"}` | `{"ok":true,"dropped":[{"id","kind":"prompt"\|"new_session","text","origin"?,"at"}, …]}` oldest first |

The queue lives in the daemon's memory only. A daemon restart (container recreate, bridge reload) drops held jobs; the restarted daemon reads the old `queue` from the state mirror and posts one `queue_lost` card listing them. The composer shows held messages from its own optimistic echo, so after a page reload or on another device they are not shown until they run (follow-up: render them from the state file's `queue`).
| `cancel` | `{"op":"cancel"}` | `{"ok":true}` (no-op when idle) |
| `config` | `{"op":"config","id":"thinking","value":"high"}` | `{"ok":true,"configOptions":[...]}` / `{"ok":false,"error":"rpc_error","detail":"..."}` |
| `state` | `{"op":"state"}` | `{"ok":true, ...state}` |
| `answer` (P3) | `{"op":"answer","id":"el-…","action":"accept"\|"decline"\|"cancel","content"?:{…}}` | `{"ok":true,"status":"answered"\|"declined"\|"cancelled"}` · `{"ok":false,"error":"not_pending"}` (answered elsewhere, turn over, daemon restarted) · `{"ok":false,"error":"bad_content"\|"bad_action","detail":"…"}` (checked against the question's schema before omp sees it) |

**Question cards and plan mode (chat package P3).** Only the omp container's
chat daemon advertises `clientCapabilities.elicitation.form` (`ChatSession(...,
elicitation=True)` in `build_session`); task runs (`bridge.run_acp_once`) and
the host-side chat bridge do not, so an unattended run never stops on a question.
omp then asks through `elicitation/create` (form mode): the daemon keeps the
JSON-RPC request open under its own id `el-<12 hex>`, writes a
`custom_message`/`acp-elicitation` line (`content` = question text, `data =
{elicitationId, message, schema}` with the schema cut down to the rendered
fields) and lists the id in the state file's `elicitations`. The `answer` op
replies to omp and writes a `custom`/`acp-elicitation-result` line (`data =
{elicitationId, status, content?}`). A question still open when its turn ends
(omp's own timeout — form mode has no `elicitation/complete` — or Stop), on
`/new` or a child restart is answered `cancel` and marked `expired`; ids a
previous daemon still listed are marked `expired` at start. URL-mode requests
and unusable schemas are declined at once. Plan mode needs no op of its own:
`config` with `{"id":"mode","value":"plan"|"default"}`; `current_mode_update`
updates the `mode` row of `configOptions` and writes a quiet `custom`/`acp-mode`
line (`data = {modeId}`); `session_info_update` is mirrored as `sessionInfo`.
Backend: `POST /agents/{id}/chat/elicitation/{elicitation_id}` (admin, 409
`elicitation_not_pending`), `POST /agents/{id}/chat/mode` (admin; only values in
`capabilities.modeOptions`, which lists modes only when the agent reports at
least two).

`state` / `acp-chat-state.json` schema:

```json
{
  "version": 1,
  "driver": "omp" | "hermes",
  "sessionId": "…",
  "busy": false,
  "turn": 12,
  "transcript": "/abs/path/…jsonl",
  "configOptions": [{"id":"thinking","category":"thought_level","currentValue":"medium","options":[{"value":"off","name":"Off"}, …]}, {"id":"model","category":"model","currentValue":"…","options":[…]}, {"id":"mode", …}],
  "commands": [{"name":"usage","description":"…","input":null}, …],
  "queue": [{"id":"…","kind":"prompt" | "new_session","text":"…","at":"…"}, …],
  "current": null | {"kind":"prompt" | "new_session","turn":12 | null,"text":"…" | null,"truncated":false,"startedAt":"…"},
  "lastReset": null | {"at":"…","via":"new_session" | "start" | "child_restart","reason"?:"…","previousSessionId"?:"…","sessionId":"…"},
  "updatedAt": "2026-09-13T12:00:00Z",
  "lastError": null | {"code":"…","detail":"…","at":"…"}
}
```

Slash commands are sent as prompt text (`/usage`, `/model X`); omp executes them inside `session/prompt`. Exception: `/new` and `/clear` — omp does not advertise them over ACP, so the backend sends them as `new_session` (queued behind a running turn) and the slash palette lists them; a harness that advertises its own `new`/`clear` keeps it. `/model` is additionally offered via `config id=model` so the composer's model picker uses the option list.

### CLI shim `acp_chat_ctl.py`

`python3 /opt/omp-bridge/acp_chat_ctl.py <op> [--json '{...}']` → prints the daemon's JSON response, exit 0 when `ok`, 2 when `ok:false`, 3 when the socket is unreachable (`{"ok":false,"error":"unreachable"}`).

### entrypoint (omp image)

When `OMP_DRIVER=acp`, `start_native` additionally opens tmux window 3 `win3`: `exec python3 /opt/omp-bridge/acp_chat.py --serve`. Window 0 still exists but no longer runs the native TUI (fix omp-acp-no-tui-window, 13.09.2026) — it prints an `OMP_ACP_READY` banner and drops to a plain shell instead.

### Backend

- `Agent.headless_chat` (computed_field, `models/agent.py`): `True` when (`agent_runtime == "cli-bridge"` and `harness == "omp"` and `omp_driver_for("omp") == "acp"` — ADR-084, harness property; the ADR-081 slug list is gone) or (`agent_runtime == "host"` and `harness == "hermes"` and `settings.hermes_driver == "acp"`). Exposed in `AgentRead`.
- `agent_chat_input._target_kind` gains `"acp-docker"` / `"acp-http"`; `send_text`, `send_keys` (`Escape` → `cancel`, everything else → `InputNotSupportedError`), `set_effort` (→ `config thinking=<level>`; `EffortSwitchRejectedError` on `ok:false`), and a new `set_model(agent, name)` (→ `config model=<name>`) route through `acp_chat_transport.py`.
- `send_text` asks the daemon for `mode: "queue"` (a mid-turn message is held, not refused) and turns an exact `/new` or `/clear` into `new_session` unless the agent advertises that command itself; an agent image without the op answers `unknown_op` (its old `acp_chat_ctl.py` rejects the op in argparse: exit 2, empty stdout, `invalid choice` — the Docker transport reads that as `unknown_op`, not as unreachable) → 409 `input_not_supported` (never a prompt the model would play along with). Refusals the daemon does not show as a card itself (`not_started` → 409 `agent_starting`, anything else → 409 `acp_refused`) are errors, never a silent 204. `clear_queue(agent)` → `queue_clear`, exposed as `POST /agents/{id}/chat/queue/clear` → `{"dropped": [text, …]}`.
- `effort_capabilities` → levels from `configOptions[id=thinking].options` in `acp-chat-state.json`; `slash_command_capabilities` → `commands`; `model_options_capabilities` → `configOptions[id=model].options` (label = name, command = `/model <value>`). Empty/missing state file → empty lists with reason `acp_state_missing` (never raises).
- `omp_chat.resolve_transcript_dir` accepts `host` + `harness == "hermes"` (dir `~/.mc/agents/hermes/omp-sessions`), so the Hermes daemon reuses the omp transcript format and the whole reader/preview stack unchanged.
- `_parse_custom_message`: `customType == "chat_error"` → `{"kind":"message","role":"teammate","source":{"kind":"error","title":"chat_error"},"error":{"code","detail"}}` so the frontend can style it.

### Frontend

- `Agent.headless_chat?: boolean` in `types.ts`.
- `ChatView`: when `agent.headless_chat` the Chat/Terminal toggle is not rendered and `effectiveView` is forced to `chat` unless `?view=terminal` is explicitly in the URL. Stop button sends `Escape` (unchanged API → cancel); for a headless agent it first calls `POST /agents/{id}/chat/queue/clear` and puts the held messages back into the composer, so the next held message does not start the moment the turn is cancelled. A mid-turn send shows as a queued row ("sent when the current reply ends") with take-back / edit, both backed by `queue/clear`.
- Error events (`source.kind === "error"`) render as a red-bordered system card with the code chip (Signal palette tokens, i18n keys `chat.error.<code>`).
- Effort chip, model picker and slash palette already come from capabilities; nothing hardcoded.

### Hermes (`scripts/hermes-bridge.py`, `HERMES_DRIVER=acp`)

- On start: import `acp_chat` from `$MC_REPO_PATH/docker/omp-bridge` and run `ChatSession` in a thread with `client_factory = ACPClient(command=["hermes","acp"], cwd=<hermes cwd>)`, `sessions_dir = ~/.mc/agents/hermes/omp-sessions/<encoded cwd>`, socket `~/.mc/agents/hermes/acp-chat.sock`.
- HTTP: `POST /chat/<op>` (JSON body) → socket → JSON response (status 200 on `ok`, 409 on `busy`, 502 unreachable).
- Task dispatch under `HERMES_DRIVER=acp`: `dispatch_poll_loop` sends the task prompt through the same `ChatSession.prompt` (so the chat shows the work) and waits for the turn to end; native TUI path remains the fallback when the driver is unset.

## Proofs (live, each PR before merge/deploy)

1. Message typed to the ACP agent appears as user line + streamed preview + final assistant line in the Sessions chat; `docker exec … acp_chat_ctl.py state` shows `turn` incremented.
2. `/usage` from the palette answers in the chat; `/model <other>` changes `configOptions.model.currentValue`.
3. Effort chip `high` → state `thinking=high`.
4. Stop during a long turn → turn ends with `cancelled`, no error card.
5. Sabotage: `config model=does-not-exist` → red `chat_error` card with `rpc_error`.
6. The ACP agent's Sessions page: no Chat/Terminal toggle; `?view=terminal` still opens the pane.
7. Hermes: chat message answered over `hermes acp`; history survives a bridge restart.

## Rollout

PR A (bridge daemon + ctl + entrypoint + tests) → PR B (backend) → PR C (frontend) → PR D (Hermes). Container recreate of the ACP agent only after Boss reports the #148 follow-up task finished; hermes-bridge reload only after Hermes' task G5 — both announced to Boss first.
