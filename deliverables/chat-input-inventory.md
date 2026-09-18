# Chat input inventory — commands vs. messages

Task `596934e8-d64b-403b-b6e9-e3e862adf1a4`. Base `6462b9a7` on branch
`task/chat-input-inventory-commands-behave-differently-from-messages`.

Scope: **inventory only**. No fix, no change to the echo timeout, no `/new` executed.
Every line number below was printed from the file at the base commit in this worktree.

Symptoms under investigation:

- **A** — `/new` on an omp agent does nothing in the UI.
- **B** — every slash command stays `Nicht bestätigt — Terminal prüfen` forever.

---

## 1. Inventory: input kind → reaches transcript? → how the display learns about it

The chat frontend has exactly **two** send entry points (`Composer.tsx:492` `onSend(body)` for
the textarea, `:546` ``onSend(`/model ${name}`)`` for the model dropdown) plus one send that
bypasses `onSend` entirely (`:555` `api.chat.setEffort`). Nothing in between inspects the text
for a leading `/`.

| # | Input kind | Transport to agent | Reaches the transcript? | How the UI learns it arrived |
|---|---|---|---|---|
| 1 | Plain text | `POST /chat/input` → `send_text` → ACP `session/prompt` (`agent_chat_input.py:626`) or tmux `send-keys` (`:669`) | **Yes** | ACP/bridge: daemon writes a `message/user` line itself (`acp_chat.py:353`) · Claude/omp-native: CLI writes it. UI: `useChatStream.ts:726` reconciles the echo by **text match** |
| 2 | Slash command, Claude family | same as #1 (tmux path) | Yes, as a **`command`** event — either the `<command-name>` wrapper (`transcript_chat.py:450-453`) or a bare `/…` string (`:485-495`) | `useChatStream.ts:728` `reconcileEcho(ev.command)` (+ history arm `:674`). This is the one arm that works |
| 3 | Slash command, omp **native** driver | same as #1 | Yes, but as a **`message/user`** line — `omp_chat._parse_user` (`omp_chat.py:544`) has **no `/`-prefix check** | Only the text-match arm (`:726`). Since omp never emits `command`, the command-specific arm is dead here |
| 4 | Slash command, omp **ACP** driver (the standard deployment) | same as #1 → ACP `session/prompt` | Yes, as a **`message/user`** line, written by MC's own bridge: `map_user_prompt` (`acp_chat_events.py:311-330`), emitted at `acp_chat.py:353`, mirrored into the native file at `bridge.py:4363` | Only the text-match arm (`:726`) — **and only if the tailed file is the bridge file** (see §3). The command arm never fires |
| 5 | `/model <name>` (dropdown) | `Composer.tsx:546` → `onSend` → same as #1, plain text `/model X` | Same as #1–#4 — **no dedicated route exists** (`agent_chat_input.set_model` at `:877` has zero callers; no `/chat/model` in the live `openapi.json`) | Same as #1–#4. On ACP: no `command` event → echo expires |
| 6 | Effort chip | **Bypasses the composer text**: `Composer.tsx:555` → `api.ts:885` `POST /chat/effort` → `set_effort` → ACP `config("thinking", level)` (`agent_chat_input.py:870`) | **No transcript line at all** | Nothing to reconcile — the echo never exists. The `ok:true` JSON from the daemon is the *only* genuine acceptance proof in the whole input path |
| 7 | Stop (Escape) | `ChatView.tsx:554` `handleStop` → `:556` `sendKeys(agent.id, ["Escape"])` → `send_keys` | No | n/a |
| 8 | Withdraw queued (Up, C-u) | `ChatView.tsx:564` `handleWithdrawQueued` → `:568` `sendKeys(agent.id, ["Up","C-u"])` | No | n/a. **ACP-rejected**: `send_keys` allows only `["Escape"]` for `_ACP_KINDS` (`agent_chat_input.py:715`), everything else → `InputNotSupportedError` → 409 |
| 9 | Permission answer (digit / y / n) | `ChatView.tsx:572` `handleAnswer(key)` → `sendKeys(agent.id, [key])` | No | n/a. Same ACP rejection as #8. The ACP path instead writes `acp-permission` / `acp-permission-decision` cards, answered by the *next* prompt |
| 10 | Attachment | `Composer.tsx:490` composes `[Anhang: <pfad>]` into the **body text**, then `:492` `onSend(body)` → identical to #1 | Yes, as part of the plain-text message | Same as #1 |
| 11 | Mid-turn queued steer (`queued_command`) | Backend-side; parsed by `_parse_attachment_entry` (`transcript_chat.py:411-440`), which routes `attachment.prompt` through `_parse_user_entry` | Same split as #2/#3 — a steer beginning with `/` becomes a `command` for the Claude family, a `message/user` for omp | Same as the corresponding row |
| 12 | `session_changed` (rollover) | — | Only fires on a **new session file** or a **pane-marker increase** | `useChatStream.ts:754` retires only `/clear` echoes (`SESSION_CLEARING_COMMANDS`, `:76`) |

Three of the twelve rows are structurally unreachable on an ACP omp agent (#8, #9, #12); two
more (#3, #4) reach the transcript but in the *wrong shape* for the echo reconciler.

---

## 2. The split point

One line decides command-vs-message for the Claude transcript format:

```
backend/app/services/transcript_chat.py:485
    elif text.startswith("/") and "\n" not in text:
```

Its context: `_parse_user_entry` (`:441`). A string `message.content` is first offered to
`_parse_local_command_wrapper` (`:450-453`); if that returns `None` the string is normalized to
a one-text-block list (`:462-466`) and falls into the block loop, where `:485` produces

```python
{"kind": "command", "uuid": msg_uuid, "ts": ts, "command": text, "result": None}   # :486-494
```

and the `else` at `:496-504` produces `{"kind": "message", "role": "user", …}`.

`_parse_attachment_entry` (`:411-440`) calls the same function, so mid-turn steers split
identically.

### Where the split does **not** happen

Two parsers bypass it completely — both are on the omp path, i.e. both on the reported bug:

- `backend/app/services/omp_chat.py:544` — `_parse_user` **always** returns
  `{"kind": "message", "uuid": …, "role": "user", "text": text, …}`. No `/`-prefix check, no
  wrapper parsing. The module docstring states it outright (`omp_chat.py:60-62`):
  *"WAS OMP NICHT HAT: … In-Session-Slash-Kommandos im Transkript (kein `command`-Ereignis)"*.
- `docker/omp-bridge/acp_chat_events.py:311-330` — `map_user_prompt` **always** emits
  `{"type":"message", … "message":{"role":"user", "content":[{"type":"text","text": text[:8000]}],
  "attribution":"user"}}`. Called from `docker/omp-bridge/acp_chat.py:353`
  (`self._emit_transcript(self._mapper.map_user_prompt(text))`) **before** `client.prompt(...)`
  in `_run_turn` (`:347`) — for every prompt, command or not. Also mirrored into the native
  transcript by `bridge.py:4363`.

So for the standard deployment (`OMP_DRIVER_DEFAULT=acp`), a slash command is written into both
transcripts as a plain user message. **A `command` event is never produced anywhere on the omp
path.** The frontend arm that exists specifically to confirm slash commands
(`useChatStream.ts:727-728`) can therefore never run for an omp agent.

Backend write path, for completeness: `POST /agents/{agent_id}/chat/input` (`agent_chat.py:453`,
`status_code=204`) validates only emptiness / length / control chars; `send_text`
(`agent_chat_input.py:595`) selects `kind` at `:618` and takes the ACP branch at `:621`
straight to `transport_for(agent).prompt(text)` at `:626`. **No line in that function inspects
`body.text` for a leading `/`.** Git history: `git log -S 'reconcileEcho(ev.command)'` returns
exactly one commit (`86b357ad`); the command arm was written once and never revisited for
ACP/omp.

---

## 3. Every ACP omp turn produces two parallel transcripts

This is the load-bearing structural finding for symptom B.

Under `/home/agent/.omp/profiles/mc-agent/agent/sessions/<encoded-cwd>/` — which is what
`/home/agent/.mc/agents/probe-zzt/omp-sessions` symlinks to — each session UUID exists as a
**pair** of files:

| | native omp format | bridge-written ACP transcript |
|---|---|---|
| File name | `2026-09-18T12-09-38-642Z_<uuid>.jsonl` | `2026-09-18T12-09-38_<uuid>.jsonl` |
| Header | `{"type":"title",…}` then `{"type":"session","version":3,"id":…,"cwd":…}` | `{"type":"session","version":3,"id":…,"timestamp":…,"cwd":"","bridge":"acp"}` |
| Entry ids | 8 hex chars | `acp`-prefixed |
| `parentId` | mostly set | `null` on every line |
| Line types | `message`, `custom`, `custom_message`, `model_change`, `compaction`, … | `message`, `custom_message` only |
| `custom_message` types | `tool_execution_start`, `mid-run-todo-nudge`, `async-result` | `acp-permission`, `acp-permission-decision`, `chat_error` |
| User line keys | `['attribution','content','role']` | `['attribution','content','role','timestamp']` |
| Assistant count (live pair, session `01a0b46c-…`) | 284 | 468 |

Classification regexes: native `^\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-\d+Z_`, bridge
`^\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d_`.

`find_active_session` (`omp_chat.py:165`) ranks both:
`rank = mtime if entry_ts is None else entry_ts` (`:192`), selection
`if (rank, mtime) > (newest_rank, newest_mtime)` (`:198`), using `last_entry_timestamp`
(`transcript_chat.py:1048`).

### Frozen measurement (32 pairs, `/tmp/census.json`)

```
pairs                    32
rank_winner              native 14 / bridge 17 / tie 1
mtime_winner             native 14 / bridge 18
rank_margins < 0.2 s     18 of 32
rank_margin max          42815.625 s
ranking disagreements    0
```

Row 0 (`uuid 01a0a6bd-…`): `rank_native == rank_bridge == 1789511231.529` (a rank tie);
`mtime_native 1789511231.5304973` vs `mtime_bridge 1789511231.5310204` — 0.5 ms apart.

**Phrasing that is supported by the data:** both files are candidates and the rank is a
tiebreak; MC does not have a stable winner. **Phrasing that is *not* supported:** "MC always
tails the bridge file" or "MC always tails the native file".

**The conclusion does not depend on which one wins.** Under *either* file the user line is a
`message/user` (§2), so no `command` event is produced and the slash-command echo is never
reconciled.

### Advertised-command measurements

- The live ACP state file `acp-chat-state.json` (profile `mc-agent`, session `01a0aba9-…`,
  `driver "omp"`, `busy false`) reports **45** commands. Mirrored there by
  `docker/omp-bridge/acp_chat.py:310` (`"commands": json.loads(json.dumps(self._commands))`),
  populated from `available_commands_update` → `_set_commands` (`:460`/`:548`). The same 45-name
  list appears in-tree in `docker/omp-bridge/rpc/acp-normal-turn.ndjson` line 5.
- `_OMP_BUILTIN_SLASH_COMMANDS` (`agent_chat_input.py:1539`) has **83** entries, registered for
  `"omp"` inside `_BUILTIN_SLASH_COMMANDS_BY_HARNESS` (`:1625`).
- **Delta: 38 builtin-only, 0 advertised-only.**

The 38 builtin-only names: `settings, setup, plan, plan-review, vibe, goal, guided-goal, loop,
queue, collab, join, leave, copy, open, hotkeys, extensions, agents, git, hub, branch, fork,
tree, login, logout, new, clear, drop, resume, btw, tan, omfg, cleanse, debug, exit, restart,
live, pause, quit`.

`/new`, `/clear`, `/drop`, `/resume`, `/fork`, `/tree`, `/branch`, `/restart`, `/exit`, `/quit`,
`/pause`, `/live` are all in that builtin-only set: **not advertised by ACP, therefore
structurally unreachable for a live omp agent.** Symptom A's core, measured rather than
inferred.

`_OMP_BUILTIN_SLASH_COMMANDS` is not merely unused — it is **unreachable for the standard
deployment**. `headless_chat_kind(agent) is not None` short-circuits at
`agent_chat_input.py:1753` (`slash_command_capabilities` returns the state file's list verbatim)
and at `:1819` (`model_options_capabilities`). `headless_chat_kind`
(`acp_chat_transport.py:93`) returns `"acp-docker"` for `cli-bridge` + `harness == "omp"` +
`omp_driver_for("omp") == "acp"`; `omp_driver_for` (`harness_compat.py:251`) defaults to
`"acp"`, and `OMP_DRIVER_DEFAULT=acp` is set in `.env.example:102` and in both compose services
(`docker-compose.yml:234`, `:472`). So the 83-entry table is dead code for every omp agent in
this deployment, and the UI palette can only ever offer the 45.

### Why `/new` can never be confirmed (symptom A, second half)

Even if `/new` were advertised, the confirmation path is closed headless. MC's entire
fresh-session detection is a substring count in a **tmux pane capture**:

- `transcript_chat.py:1985-2016`: `pane_text = await capture_pane(agent)`; the marker is
  `adapter.fresh_session_pane_marker` (`transcript_adapters.py:129`; `:192` passes
  `omp_chat.FRESH_SESSION_MARKER = "New session started"`, `omp_chat.py:112`); a **new**
  occurrence (count must *increase*) → `fresh_session.mark(agent_id)` + reset + broadcast
  `{"kind":"session_changed","aliveness":"active"}`.
- `capture_pane` (`pane_state.py:304`) shells out to
  `docker exec … tmux capture-pane -p -t <slug>:0` (`argv` at `:327-330`) and returns `None` for
  any runtime that is not `cli-bridge` (`:319-321`). An ACP agent has no native TUI
  (`docs/specs/chat-over-acp.md:47-52`), and `pane_text` must be non-`None` for the branch to run
  at all — so **the `session_changed` event can never fire for an ACP agent.**
- Compounding: `fresh_session.py`'s own docstring records the measured fact that omp's `/new`
  creates **no session file at all** (terminal shows `✔ New session started`, disk unchanged).

Note the asymmetry: the **rollover** `session_changed` at `transcript_chat.py:2079` is file-based
and does work when a new session file appears. The pane-marker one is the one that is dead
headless. Both would need to fire for `/new` to be honest.

Same bug class, two carriers: `fresh_session.py` (`_marks`, `mark(agent_id, at=None)`,
`is_stale(agent_id, session_path)` consuming the mark when `st_mtime >= marked_at`) and the
`/chat/history` short-circuit at `agent_chat.py:198`, which returns `{"events": []}` with
`sessionId f"fresh-{int(marked_at)}"` — reachable only once a mark exists, which requires the
pane probe.

---

## 4. Verdict on the pre-send existence check

Proposal evaluated: validate the typed text against `capabilities.slashCommands` (already on the
client via `historyQuery.data.capabilities`, wired at `ChatView.tsx:1120` →
`Composer.tsx:387` `resolveSlashCommands`) before submitting — gated in `ChatView.handleSend`
(`ChatView.tsx:527`) and/or `Composer.send` (`Composer.tsx:486-494`).

**Verdict: solves A; halves B.**

**It fixes A.** Today `/new` is accepted by every layer and reaches the agent as prompt text.
The UI shows a pending tile, `send_text` returns success, and the confirming `session_changed`
event is structurally impossible (§3). The operator gets "nothing happens" with no explanation.
A pre-send check against the 45 advertised commands rejects `/new` with a visible "not available
for this agent" message instead — the operator learns the truth immediately. `/clear`, `/drop`,
`/resume`, `/fork`, `/tree`, `/branch`, `/restart`, `/exit`, `/quit`, `/pause`, `/live` all get
the same treatment. That is the whole of symptom A's operator-visible complaint, addressed.

**It only halves B.** B is "every slash command stays `Nicht bestätigt` forever". Refusing the
38 non-advertised ones removes *their* unconfirmed tiles. But the 45 commands that legitimately
exist — `/usage`, `/compact`, `/model X`, `/init`, `/review`, … — are all still sent as prompt
text, and on the ACP path they still produce a `message/user` line and never a `command` event.
Their echo still runs the full `ECHO_CONFIRM_TIMEOUT_MS = 10_000` clock
(`useChatStream.ts:34`) and still flips to `unconfirmed` → `Nicht bestätigt — Terminal prüfen`
(`ChatMessage.tsx:141`, `:258-268`). **A filter cannot close B, because B's cause is on the
confirmation side, not the input side.** Closing B needs a second, different change:

- reconcile against the `user` line the daemon itself writes at `acp_chat.py:353` /
  `acp_chat_events.py:311` (text-match already happens at `useChatStream.ts:726` — but only if
  the *bridge* file is the one being tailed, and §3 shows that is a coin flip), or
- treat the ACP `prompt` `ok:true` response as delivery proof the way `set_effort` already
  does (`agent_chat_input.py:870`), or
- make `send_text`'s ACP branch note the prompt (`note_sent`, `agent_chat_input.py:637`, already
  called) so the tailer can reconcile it deliberately instead of by text coincidence.

Argument against the tempting shortcut "just add `omp` to the `/`-prefix rule at
`transcript_chat.py:485`": that line is in `_parse_user_entry`, which the **omp adapter does not
use** — `OmpLineParser._parse_user` (`omp_chat.py:544`) is a separate parser. Patching `:485`
would fix the Claude family only and change nothing for omp. The same is true of the ACP bridge:
its transcript is written by `map_user_prompt` in Python inside the container, entirely outside
`transcript_chat.py`.

**Consequence for a single fix:** the two symptoms do *not* share a root cause. They share a
*boundary* — `ChatView.tsx:540-553`, where `sendText`'s promise settles — but A's cause is
"the UI does not know which inputs are possible" and B's is "the UI cannot observe that an
accepted input arrived".

---

## 5. Overlap with the parallel investigation

A second agent is working the same input path on the finding *"operator messages lost, 204
responses"*. The overlap is exactly this boundary: `ChatView.tsx:540-553`
(`api.chat.sendText(...).catch((err) => …)`, `isAgentStartingError` → `echoAgentStarting`, else
`echoFailed` + error toast) together with `send_text`'s ACP branch
(`agent_chat_input.py:618-637`), where an `ok:false` answer that is not `"busy"` is only logged
(`logger.warning`) and the HTTP layer still returns **204**.

Not investigated here — noted so the two reports are not read as contradictory. Any fix in this
region touches both findings.

---

## 6. Plainly undeterminable

1. **Which of the two paired transcripts the live MC instance actually tails.** Both are
   candidates and the winner flips tick to tick (18 of 32 pairs separated by < 0.2 s of
   last-entry time; one rank tie). Not resolvable from the files alone, and not needed for the
   conclusion.
2. **Whether omp's ACP layer has its own internal `/`-prefix branch.** No transcript evidence in
   either direction. The runtime is a single 156 MB binary (`omp/18.1.10`,
   `/usr/local/bin/omp`) with no JS bundle to inspect and no `file(1)` on this host. What *is*
   established: no `command` event and no `kind: "command"` entry exists anywhere on disk.
3. **The exact command list MC's UI currently renders for this agent.** No agent-scoped history
   route is reachable with an agent token (agent tokens are rejected on user routes with
   `{"detail":"Agent-Token darf User-Routes nicht nutzen. …"}`). The state file's 45 is the
   source the backend reads (`read_acp_chat_state`, `acp_chat_transport.py:261`), so that is the
   best available answer, not a directly observed one.
4. **Whether a `chat_error` card is the *intended* confirmation channel.** The live timed-out
   session's bridge transcript (`--workspace--/2026-09-16T19-19-56_01a0aba9-….jsonl`, 12.7 MB)
   contains ~250 `custom_message` entries with `customType "chat_error"` and the text
   `A reply is already running — wait or press Stop.`, plus a final one
   `no response for session/prompt within 3600.0s`. `omp_chat.py:838` turns those into visible
   cards. This is genuine server→UI delivery, but it is a *failure* channel; whether it also
   carries success acknowledgements is undetermined.
5. **Whether the deployed frontend even receives the 45-name list on first paint.** The
   capability block comes from `GET /chat/history` (`agent_chat.py:163`); the composer treats a
   `null` list as "older backend" and falls back to the static 6-entry Claude list
   (`Composer.tsx:111`, `claudeCommands.ts:30`). Whether the operator's session ever saw a
   populated `slashCommands` could not be confirmed from here — no user-route access.

---

## 7. Supporting negative results

- Parsed-entry census across the host: `/home/agent/.omp` holds **118** `.jsonl`;
  `/home/agent/.mc` holds **0** (the omp sessions dir is symlinked, not copied). User lines whose
  text starts with `/`: **0**. Lines with `"kind": "command"`: **0**.
- The byte string `<command-name>` appears in **14** files, but in every case it is tooling or
  tool-output content *about* the wrapper (e.g. `_parse_local_command_wrapper`'s own source text
  echoed by a tool read) — never a parsed user entry. These are **not** command events.
- Unit tests pin the Claude-family command rule in
  `backend/tests/test_transcript_chat_parser.py`:
  `test_user_string_content_slash_command_still_recognized` (`:140`, asserts `{"kind":
  "command"` at `:146`), `test_local_command_name_message_args_becomes_command_event` (`:233`),
  a `/clear` wrapper test (`:290-302`), and an `/effort low` string test (`:316-328`). No fixture
  anywhere uses `command-name`; the fixtures are inline JSON strings in that one file. **No test
  covers the ACP/omp path** — which is why the divergence survived.
- `OmpLineParser.__call__` (`omp_chat.py:485-518`) handles only `message`,
  `thinking_level_change`, `custom_message`, `custom`, and `_SILENT_TYPES`; everything else
  returns `[]` with a debug log. There is no branch that could manufacture a `command` event even
  if the data contained one.

---

## 8. One-line summary per symptom

| Symptom | Root cause (file:line) | Why the UI is silent |
|---|---|---|
| A: `/new` does nothing | `/new` is builtin-only, not ACP-advertised (§3); confirmation needs a tmux pane marker | `transcript_chat.py:1985-2016` needs `capture_pane` non-`None`; `pane_state.py:319-321` returns `None` headless → `session_changed` can never fire |
| B: commands stay unconfirmed | `map_user_prompt` (`acp_chat_events.py:311`) and `_parse_user` (`omp_chat.py:544`) always write `message/user`; the split at `transcript_chat.py:485` never runs for omp | `useChatStream.ts:727-728` is the only slash-command confirmation arm; it never executes, so the echo expires at `:34` |
