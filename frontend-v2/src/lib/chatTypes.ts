/**
 * Sessions Chat event types — mirror
 * `backend/app/services/transcript_chat.py` exactly (normalized event dicts
 * emitted by `parse_transcript_line` / `read_history` / `ChatTailerManager`)
 * plus the `state` event from `pane_state.parse_pane_state` (A6). Keep these
 * two files in sync; the backend module docstring is the source of truth.
 */
import type { ChatDiffSource } from "./types";

/** Woher eine Zeile „von aussen" stammt (Backend `_mention_source` in
 *  `omp_chat.py` bzw. `_parse_teammate_message` in `transcript_chat.py`).
 *  `task` = Auftrag (Dispatch-Datei), `nudge` = Anstoss „Posteingang lesen",
 *  `inbox` = ueber MC zugestellte Nachricht, `teammate` = Rueckmeldung eines
 *  Subagenten/einer anderen Sitzung, `system` = Meldung des Harness selbst.
 *  `title` ist nur beim Auftrag gefuellt (aus der ungekuerzten Datei). */
/*  `error` = eine Stoerung des Chat-Daemons selbst (ACP, siehe
 *  docs/specs/chat-over-acp.md). Sie steht als Transkript-Zeile im Verlauf,
 *  weil ein Fehler, den nur das Log kennt, fuer den Operator nicht
 *  stattgefunden hat — der Code liegt dann in `MessageEvent.error`. */
export type MessageSourceKind = "task" | "nudge" | "inbox" | "teammate" | "system" | "error";

export interface MessageSource {
  kind: MessageSourceKind;
  title: string | null;
}

export interface MessageEvent {
  kind: "message";
  uuid: string;
  ts: string;
  /** `teammate` = Rückmeldung eines Subagenten/einer anderen Sitzung. Claude
   *  Code schreibt die als gewöhnlichen USER-Turn ins Transkript — ohne diese
   *  dritte Rolle erschiene sie als Nachricht des Operators, die er nie
   *  getippt hat (Operator-Befund 19.08.2026). */
  role: "user" | "assistant" | "teammate";
  text: string;
  model: string | null;
  sidechain: boolean;
  /** Nur bei `role: "teammate"`: wer geschrieben hat. `null`, wenn die
   *  Nachricht keinen Absender trug — dann wird nichts behauptet. */
  teammate?: string | null;
  /** Nur bei `role: "teammate"`: die Herkunft, sofern der Parser sie sicher
   *  kennt. `null`/fehlend = keine Behauptung, die Zeile bleibt schlicht. */
  source?: MessageSource | null;
  /** Nur bei `source.kind === "error"`: was schiefging. `code` ist eine der
   *  Marken des Chat-Daemons (`rpc_error`, `provider_error`, `empty_turn`,
   *  `process_exit`, `busy`, `session_reset`) und waehlt den Text der Karte;
   *  ein unbekannter Code faellt auf den Sammelbegriff zurueck, statt die
   *  Meldung zu verschlucken. `detail` ist der Wortlaut der Quelle und darf
   *  fehlen — dann traegt `text` die Erklaerung. */
  error?: ChatErrorInfo | null;
}

export interface ChatErrorInfo {
  code: string;
  detail?: string | null;
}

export interface ToolEvent {
  kind: "tool";
  uuid: string;
  ts: string;
  name: string;
  title: string;
  detail: Record<string, unknown>;
  toolUseId: string | null;
  result: string | null;
  status: "done" | "error";
  stats: { additions: number; deletions: number } | null;
  sidechain: boolean;
  /** One diff per edited file (backend `chat_diff.py`), harness-neutral: an
   *  ACP edit's whole-file old/new text, a Claude Edit/MultiEdit's own
   *  strings, a Claude Write's content. Absent = the tool changed no file
   *  (or the harness did not say). `stats` then counts these same diffs. */
  diffs?: ToolDiff[] | null;
  /** File paths the call touched (ACP `locations`, Claude `file_path`).
   *  Container/host paths — shown as copyable chips, never opened blindly. */
  locations?: string[] | null;
  /** Pictures the tool produced (chat package P2): browser screenshots,
   *  images it read. References only — the bytes come from
   *  `api.agents.chatMediaUrl` with the chat's auth (backend `chat_media.py`). */
  images?: ToolImage[] | null;
}

/** One stored tool picture. `id` is its sha256; width/height are the
 *  original's, so a thumbnail reserves the right shape before it loads. */
export interface ToolImage {
  id: string;
  mime: "image/png" | "image/jpeg" | "image/webp" | "image/gif";
  width: number;
  height: number;
  bytes: number;
}

export interface ToolDiffLine {
  /** `hunk` = a gap between changed regions (rendered as a separator). */
  t: "hunk" | "ctx" | "add" | "del";
  text: string;
}

export interface ToolDiff {
  path: string | null;
  /** `create` = the harness said the file is new. `write` = new content
   *  whose predecessor is unknown (Claude Write) — never called "new file". */
  kind: "edit" | "create" | "write";
  /** Capped by the backend; `truncated` says when lines were dropped. */
  lines: ToolDiffLine[];
  additions: number;
  deletions: number;
  truncated: boolean;
}

/** One file this chat changed (backend `chat_changes.py`): every edit of it
 *  folded together, from the per-edit diffs in the transcript. */
export interface ChatChangeFile {
  /** Relative to the chat's folder when inside it, else as the agent wrote it. */
  path: string;
  /** Counted from edits with a known before; a Write's lines are not claimed. */
  additions: number;
  deletions: number;
  /** Tool calls that changed this file. */
  edits: number;
  /** The first edit said the file was new. */
  created: boolean;
  /** At least one Write whose predecessor the transcript does not know. */
  written: boolean;
  /** ISO time of the newest edit. */
  lastAt: string | null;
  /** All edits of the file, oldest first — rendered by `ToolDiffView`. */
  diff: ToolDiff;
  /** MC put the file back (Revert) in this session and it still holds
   *  exactly that content. Absent = not reverted. */
  reverted?: boolean;
}

/** Whether Commit / Open PR can run for the chat's repository (Changes panel, part 3). A
 *  `reason` is a code the panel translates (`sessions.diff.reason.*`). */
export interface ChatActionAvailability {
  ok: boolean;
  reason: string | null;
  files?: number;
}

/** `POST /agents/{id}/chat/revert` */
export interface ChatRevertResult {
  status: "reverted" | "already";
  path: string;
  /** "git" = restored the last committed version through git. */
  method: "chat" | "git" | null;
  deleted: boolean;
  edits: number;
}

export interface ChatPendingFile {
  path: string;
  status: "added" | "modified" | "deleted" | "renamed";
  additions: number;
  deletions: number;
}

/** `GET /agents/{id}/chat/commit/draft` */
export interface ChatCommitDraft {
  branch: string;
  files: ChatPendingFile[];
  /** Suggested conventional-commit message — the operator edits it. */
  message: string;
  /** "llm" = the local model wrote `message` from the diff + chat intent;
   *  "fallback" = the deterministic mechanical message (no model answer in
   *  time, or it answered something unusable). Optional for a moment while
   *  older cached responses are still around. */
  source?: "llm" | "fallback";
  /** Present only when `source` is "llm" — the mechanical message, for a
   *  quiet "or use the simple one" affordance. */
  mechanicalMessage?: string;
}

/** `POST /agents/{id}/chat/commit` */
export interface ChatCommitResult {
  status: "committed" | "nothing_to_commit";
  branch: string;
  sha: string | null;
  subject?: string;
}

/** `GET /agents/{id}/chat/pr/draft` */
export interface ChatPrDraft {
  branch: string;
  base: string;
  /** The newest commit's subject — editable. */
  title: string;
  ahead: number | null;
  /** Uncommitted files that the PR will NOT contain. */
  uncommitted: number;
}

/** `POST /agents/{id}/chat/pr` — `exists` = an open PR for the branch was
 *  found and returned instead of a second one. */
export interface ChatPrResult {
  status: "opened" | "exists";
  url: string;
  branch: string;
  base: string;
}

/** `GET /agents/{id}/chat/changes` — the Changes panel's "This chat" tab.
 *  `repo` is the diff endpoint's `source` block, or `null` when the chat's
 *  folder is no git repository (then only this list is shown). */
export interface ChatChanges {
  files: ChatChangeFile[];
  stats: { files: number; additions: number; deletions: number };
  repo: ChatDiffSource | null;
  /** Only with a repository. */
  actions?: { commit: ChatActionAvailability; pr: ChatActionAvailability };
}

/** A running tool's output so far (ACP `tool_call_update` in_progress, via
 *  the bridge's preview channel). VOLATILE like `preview`: no uuid, one slot
 *  per `toolUseId`, the newest replaces the older, and the tool's own result
 *  ends it. Never in the history. */
export interface ToolOutputEvent {
  kind: "tool_output";
  uuid: null;
  ts: string;
  toolUseId: string;
  text: string;
}

export interface ThinkingEvent {
  kind: "thinking";
  uuid: string;
  ts: string;
  text: string;
  sidechain: boolean;
}

export interface CommandEvent {
  kind: "command";
  uuid: string;
  ts: string;
  command: string;
  /** Local-command stdout/stderr merged onto this event by parentUuid — the
   *  TUI writes a slash command run in-session as a caveat + this command
   *  entry + a stdout/stderr entry, chained; the caveat is suppressed
   *  entirely and never reaches the frontend, the stdout/stderr text lands
   *  here. `null`/absent until (or unless) that merge happens. */
  result?: string | null;
}

export interface UsageEvent {
  kind: "usage";
  uuid: string;
  ts: string;
  inputTokens: number;
  outputTokens: number;
  model: string | null;
  effort: string | null;
  /** The model's actual context window, stamped by the backend from its own
   *  model registry — the frontend never maintains its own model→window map
   *  (that map went stale the moment a new model shipped). `null`/absent
   *  means the backend doesn't know either; consumers render no context
   *  meter rather than guess. */
  contextWindow?: number | null;
  /** 0–100, the CLI's own `context_window.used_percentage` when the backend
   *  can read it straight from the CLI's structured output — ground truth,
   *  preferred over any token-count-based estimate. `null`/absent means the
   *  CLI didn't report it; consumers fall back to `inputTokens /
   *  contextWindow`, or render nothing if that's unavailable too. */
  usedPct?: number | null;
  /** Provenance of `usedPct`: `"cli"` = read straight from the CLI, an exact
   *  figure. `"estimate"` = the backend computed it itself (no ground truth
   *  available). Only meaningful together with `usedPct`. */
  source?: "cli" | "estimate";
  /** The four token buckets kept apart, for the context breakdown view.
   *  `inputTokens` above stays their input-side SUM — every existing consumer
   *  relies on that. When fresh CLI statusline state exists these describe the
   *  whole live context window; otherwise they describe just this turn.
   *  `null`/absent means the backend had no breakdown, and consumers show only
   *  used-vs-free rather than inventing segments. */
  components?: UsageComponents | null;
  /** Prompt cache hit rate for the latest turn: `cacheRead / (cacheRead +
   *  input)`, 0–1. Computed by the backend from the SAME raw buckets as
   *  `components` — but unlike `components`, never nulled out when fresh CLI
   *  statusline state exists (an ACP agent's usage line always carries both
   *  at once; nulling this too would mean its context panel never shows a
   *  hit rate — see docker/omp-bridge/acp_chat_events.py `_usage_block`).
   *  `null`/absent means the harness reported no cache activity at all this
   *  turn, or no breakdown — never a fake 0%. */
  cacheHitRate?: number | null;
  /** What the session has cost so far, as the harness reported it (ACP
   *  `usage_update.cost`). Absent when nothing was reported — a local model
   *  reports nothing, and the panel then shows no cost row at all. */
  sessionCost?: SessionCost | null;
}

export interface SessionCost {
  amount: number;
  /** ISO 4217 code, e.g. "USD". */
  currency: string;
}

export interface UsageComponents {
  input: number;
  cacheRead: number;
  cacheCreation: number;
  output: number;
}

export interface ChatPromptOption {
  key: string;
  label: string;
}

export interface ChatPrompt {
  question: string;
  options: ChatPromptOption[];
}

export interface StateEvent {
  kind: "state";
  status: "working" | "idle" | "waiting_input" | "permission_prompt" | "unknown";
  prompt: ChatPrompt | null;
  /** The probe's liveness verdict, published with every `state` frame — also on
   *  a hidden tab, since the backend probes regardless of focus. `null`/absent
   *  from older backends; use `resolveSessionAliveness`, never read directly. */
  aliveness?: ChatAliveness | null;
}

/** Emitted by the tailer when the newest `*.jsonl` under the agent's
 *  transcript dir changes (session rollover) — carries no uuid; the
 *  consumer must clear its timeline and refetch history. */
export interface SessionChangedEvent {
  kind: "session_changed";
}

/** Live-Vorschau aus dem Terminal (Live-Schicht A): der Tailer liest den
 *  Bildschirm der CLI mit und schickt den ruhig gewordenen Text, BEVOR die
 *  Antwort im Transkript steht. Keine uuid — das ist keine Zeile des
 *  Gespraechs, sondern ein Blick ueber die Schulter: eine neue Vorschau
 *  ersetzt die alte, die echte Antwort loest sie ab. */
export interface PreviewEvent {
  kind: "preview";
  uuid: null;
  ts: string;
  text: string;
  source: "pane" | "acp";
}

/** Eine Hintergrund-Meldung der CLI: ein Subagent oder ein Hintergrund-Befehl
 *  ist fertig. Sie steht im Transkript als Nachricht mit der Rolle `user` —
 *  ungedeutet stand sie darum als Nachricht des Operators im Chat, samt
 *  Kennungen und Host-Pfaden. Der Pfad (`output-file`) kommt bewusst gar
 *  nicht erst vom Backend.
 *
 *  `toolUseId` verbindet sie mit dem Werkzeugaufruf, der den Vorgang gestartet
 *  hat — bei einem Subagenten also mit seiner Karte. Nicht garantiert: über
 *  400 Transkripte gemessen trägt sie 66 von 77 Meldungen. */
export interface NotificationEvent {
  kind: "notification";
  uuid: string;
  ts: string;
  taskId: string | null;
  toolUseId: string | null;
  status: string | null;
  summary: string | null;
}

/** One item of an agent's todo list, normalised across harnesses
 *  (backend `chat_todos.py`): Claude Code `TodoWrite`, an ACP `plan`,
 *  Codex `update_plan` all arrive in this one shape. */
export interface TodoItem {
  content: string;
  status: "pending" | "in_progress" | "completed";
  priority?: "high" | "medium" | "low";
}

/** The agent's whole todo list at one moment — a SNAPSHOT, never a delta.
 *  The newest one per session wins (`chatReducer`), it lives in its own slot
 *  (`todos`), never in the timeline. */
export interface TodoEvent {
  kind: "todo";
  uuid: string;
  ts: string;
  /** Where it came from (tool name or `acp_plan`) — for debugging only. */
  source: string;
  items: TodoItem[];
  /** Set when the list is the snapshot of the task the agent is working on,
   *  read from the task's own session (an ACP task run writes its plan
   *  there, not into the chat session) — history `todos` and live pushes
   *  from the backend's todo sync. Absent for the chat session's own list. */
  taskId?: string;
}

/** One choice of a question (ACP `oneOf`/`anyOf` entry). `const` is what
 *  goes back to the agent, `title` what the button says. */
export interface ElicitationChoice {
  const: string;
  title: string;
  description?: string;
}

/** One field of a question, already cut down by the chat daemon to what the
 *  card renders (`acp_chat.clean_schema`). omp sends four shapes:
 *  select (`string` + `enum`), confirm (`boolean`), input (`string`), and
 *  per ask-tool question `string` + `oneOf` (+ `default` = recommended) or
 *  `array` + `items.anyOf` (multi-select), plus `<key>__other` free text. */
export interface ElicitationProperty {
  type: "string" | "boolean" | "number" | "integer" | "array";
  title?: string;
  description?: string;
  enum?: string[];
  oneOf?: ElicitationChoice[];
  default?: string | boolean;
  items?: { anyOf: ElicitationChoice[] };
}

export interface ElicitationSchema {
  type: "object";
  properties: Record<string, ElicitationProperty>;
  required?: string[];
}

/** A question the agent asked the operator (chat package P3, ACP form
 *  elicitation) — a timeline card with buttons/fields. Its fate arrives as a
 *  separate `elicitation_result` with the same `elicitationId`. */
export interface ElicitationEvent {
  kind: "elicitation";
  uuid: string;
  ts: string;
  elicitationId: string;
  message: string;
  schema: ElicitationSchema;
}

export type ElicitationStatus = "answered" | "declined" | "cancelled" | "expired";

/** What became of a question: answered (with the operator's `content`),
 *  declined/cancelled, or `expired` (its turn ended, the chat restarted). A
 *  side slot keyed by `elicitationId`, never a row of its own. */
export interface ElicitationResultEvent {
  kind: "elicitation_result";
  uuid: string;
  ts: string;
  elicitationId: string;
  status: ElicitationStatus;
  content?: Record<string, unknown> | null;
}

/** The session's mode changed (ACP `current_mode_update`, e.g. plan mode on
 *  or off). Quiet: only the composer's mode chip follows it. */
export interface ModeEvent {
  kind: "mode";
  uuid: string;
  ts: string;
  modeId: string;
}

export type ChatEvent =
  | MessageEvent
  | ToolEvent
  | ThinkingEvent
  | CommandEvent
  | UsageEvent
  | StateEvent
  | PreviewEvent
  | ToolOutputEvent
  | NotificationEvent
  | TodoEvent
  | ElicitationEvent
  | ElicitationResultEvent
  | ModeEvent
  | SessionChangedEvent;

/** The subset of ChatEvent kinds that carry a `uuid` and belong in the
 *  scrollable timeline list, as opposed to `state`/`usage` (side slots) or
 *  `session_changed` (a reset signal, never rendered itself). */
export type TimelineChatEvent =
  | MessageEvent
  | ToolEvent
  | ThinkingEvent
  | CommandEvent
  | NotificationEvent
  | ElicitationEvent;

/**
 * How alive the underlying CLI session is.
 *
 * `live` alone could not express this: it was derived from the transcript's
 * mtime being younger than 60s, which is false for every CLI that is running
 * but idle — waiting at its prompt writes nothing. The UI therefore declared
 * "Session beendet" at a session that was sitting right there, ready. Three
 * states separate "nothing is happening" from "nothing can happen".
 */
export type ChatAliveness = "active" | "idle" | "ended";

export interface ChatSession {
  sessionId: string;
  live: boolean;
  startedAt: string | null;
  /** Absent on older backends — use `resolveAliveness`, never read directly. */
  aliveness?: ChatAliveness | null;
}

/**
 * The one place that decides how alive a session is, so no component can
 * disagree with another.
 *
 * Without the server field, `live === true` means active and everything else
 * means IDLE — deliberately not `ended`. A stale mtime is not evidence that a
 * session finished, and treating it as such is exactly the false alarm this
 * replaces. `ended` is only ever claimed when the server says so.
 */
export function resolveAliveness(session: ChatSession | null | undefined): ChatAliveness {
  if (!session) return "idle";
  if (session.aliveness) return session.aliveness;
  return session.live ? "active" : "idle";
}

/**
 * The same verdict, but preferring the LIVE state frame over the history
 * handshake — because the history query deliberately no longer refetches on
 * window focus (`["chat-history", agentId]` in useChatStream), while the pane
 * probe keeps publishing `state` frames on every change even on a hidden tab.
 * Reading only `session` there would freeze the header badge at whatever the
 * last history fetch said; reading the state frame keeps it honest without a
 * single extra request.
 *
 * Precedence: state frame → history session → "idle". Both server fields come
 * from the same `resolve_aliveness`; the state frame simply arrives later.
 */
export function resolveSessionAliveness(stream: {
  state: StateEvent | null | undefined;
  session: ChatSession | null | undefined;
}): ChatAliveness {
  if (stream.state?.aliveness) return stream.state.aliveness;
  return resolveAliveness(stream.session);
}

/**
 * What this agent's harness can actually do, derived server-side from its
 * runtime (`agent_chat_input.effort_capabilities`). The composer builds its
 * effort chip from this instead of hardcoding a level list — the levels differ
 * per harness and per CLI version, and the backend is the single source that
 * also validates the switch request.
 *
 * `canSwitchEffort: false` with an empty list is the normal, expected answer
 * for every host agent (Boss included): no pane probe exists there, so the chip
 * has nothing to offer and says so rather than showing an empty picker.
 */
export interface ChatCapabilities {
  effortLevels: string[];
  canSwitchEffort: boolean;
  /** Die im `settings.json` des Agenten hinterlegte Effort-Stufe: der Standard,
   *  mit dem jede neue Session startet. Dient dem Composer als Startwert,
   *  solange die laufende Session noch kein `usage`-Ereignis geschrieben hat —
   *  vorher fehlte der Chip in genau diesem Fenster komplett. Ein spaeteres
   *  `usage.effort` gewinnt immer (nur es kennt die session-only-Stufen
   *  `max`/`ultracode`). `null`/absent = unbekannt, der Chip zeigt dann `auto`. */
  effort?: string | null;
  /** True bei Boss: er nutzt Marks eigene ~/.claude/settings.json — eine
   *  persistierende Stufe (low..xhigh) aendert damit auch den Standard der
   *  lokalen Claude-Sessions des Operators. Der Regler-Hinweis muss das am
   *  Wert sagen, nicht verschweigen. */
  effortShared?: boolean;
  /** True, wenn `effort` aus der LAUFENDEN Session stammt (omp: Statuszeile
   *  des Panes) und damit juenger ist als jedes `usage.effort` — das traegt
   *  nach einem Shift+Tab-Wechsel noch die Stufe des vorigen Zugs. Dann
   *  gewinnt `effort` ueber `usage.effort` (Live-Befund 03.09.2026: Chip
   *  zeigte nach Neuladen "high", Pane sagte "low"). Absent/false = der Wert
   *  ist der settings.json-Standard fuer NEUE Sessions, usage bleibt vorn. */
  effortLive?: boolean;
  /** WARUM sich der Effort nicht umschalten laesst — damit das UI es erklaeren
   *  kann, statt das Bedienelement wortlos wegzulassen. Nur gesetzt, wenn
   *  `canSwitchEffort` false ist; `null`/absent = kein Grund noetig oder
   *  aelteres Backend.
   *
   *  - `model_no_effort`: der Harness kann `/effort`, das MODELL des Agenten
   *    nicht. openclaude sagt das selbst im `/model`-Picker
   *    ("Effort not supported for <modell>") — der haeufigste Fall bei
   *    Agenten auf lokalen/eigenen Modellen.
   *  - `foreign_harness`: diese CLI kennt `/effort` gar nicht (kimi, omp).
   *  - `no_pane`: die Runtime hat keinen steuerbaren Kanal (Hermes, Jarvis). */
  effortReason?: string | null;
  /** Das MODELL, ueber das die CLI die Effort-Aussage gemacht hat — direkt aus
   *  ihrer eigenen Picker-Zeile ("Effort not supported for <modell>"). Nur bei
   *  `effortReason: "model_no_effort"` gesetzt.
   *
   *  Der Erklaertext muss dieses Modell nennen, nicht das gerade angezeigte:
   *  die Messung ist zwischengespeichert, die Anzeige nicht. Sonst stand dort
   *  schon "Das Modell X kennt keine Effort-Stufen" ueber ein Modell, das
   *  Stufen sehr wohl kennt. */
  effortModel?: string | null;
  /** Every slash command this harness actually offers — built-ins AND the
   *  agent's skills. Absent on older backends, where the composer falls back to
   *  its own short static list. `name` may or may not carry the leading slash
   *  depending on the source, so consumers must normalize (see
   *  `resolveSlashCommands`) rather than assume. */
  slashCommands?: ChatSlashCommand[] | null;
  /** The models this harness offers, with their context windows. Absent on older
   *  backends, where the switcher falls back to its static list and shows no
   *  window sizes at all — better than printing a number we made up. */
  modelOptions?: ChatModelOption[] | null;
  /** Das im settings.json des Agenten hinterlegte Modell — Startwert fuers
   *  Composer-Label, solange die Session noch kein usage-Ereignis hat (vorher
   *  stand dort "—"). Kommt als Kurz-Alias ("sonnet") ODER volle ID
   *  ("claude-sonnet-5") — beide Gestalten existieren real in der Flotte.
   *  usage.model gewinnt immer. */
  model?: string | null;
  /** Session modes the agent reports (ACP `configOptions[id=mode]`, chat
   *  package P3) — at least two, else empty: the composer then shows no mode
   *  chip. omp: `default` and, when plan mode is enabled, `plan`. */
  modeOptions?: ChatModeOption[] | null;
  /** The current mode at history time; a later `mode` event wins. */
  mode?: string | null;
}

export interface ChatModeOption {
  value: string;
  name: string;
}

export interface ChatSlashCommand {
  name: string;
  description?: string | null;
}

/**
 * One entry of the model switcher, reported by the harness so the picker can
 * show what each model's context window actually is instead of the frontend
 * carrying a model→size map that goes stale the moment a model ships.
 *
 * `command` is what gets sent as `/model <command>`. `name` is accepted as a
 * synonym because the backend assembles this alongside `slashCommands`, whose
 * key IS `name` — tolerating both here costs one line and removes a whole class
 * of silent mismatch. `contextWindow` may legitimately be unknown; then the row
 * simply carries no suffix rather than a guess.
 */
export interface ChatModelOption {
  command?: string | null;
  name?: string | null;
  label?: string | null;
  contextWindow?: number | null;
}

/**
 * Levels Claude Code applies to the CURRENT SESSION ONLY, by its own design
 * ("this session only" in the CLI's own confirmation). Every other level
 * rewrites the agent's persisted default — see
 * `backend/app/services/agent_chat_input.py`, which documents the split from
 * empirical testing, and ADR-073. The distinction matters to the operator: one
 * choice outlives the session, the other does not.
 *
 * `off` is omp's case: its thinking level is cycled with Shift+Tab and the
 * CLI writes every level but `off` to `defaultThinkingLevel` (its settings
 * enum has no `off`) — so `off` lasts until the next task relaunch.
 */
export const SESSION_ONLY_EFFORT_LEVELS = new Set(["max", "ultracode", "off"]);

export function isSessionOnlyEffort(level: string): boolean {
  return SESSION_ONLY_EFFORT_LEVELS.has(level);
}

/** Eine hochgeladene Datei, wie sie der Anhang-Endpunkt zurueckgibt.
 *  `path` ist absolut und gilt auf dem Host wie im Container gleichermassen —
 *  genau dieser String wird der Nachricht angehaengt, damit die CLI die Datei
 *  selbst liest.
 *
 *  `root`/`subpath` sind derselbe Ort in der Sprache des Files-Endpunkts, aus
 *  dem das UI die Bytes holt. Sie kommen direkt aus der Ablage, damit das
 *  Frontend sie nicht aus dem absoluten Pfad zurueckrechnen muss. */
export interface ChatAttachment {
  path: string;
  name: string;
  bytes: number;
  isImage: boolean;
  root: string;
  subpath: string;
}

/** Ein delegierter Auftrag dieser Sitzung — ein Subagenten-Lauf.
 *
 *  Claude Code legt je Subagent eine eigene Transkript-Datei an; im
 *  Hauptstrom steht davon nichts (live gemessen: 0 Zeilen mit
 *  `isSidechain: true`). Was hier steht, kommt aus dem Steckbrief neben
 *  dieser Datei.
 *
 *  Alles ausser `runId` ist `null`-faehig, und das ist kein Schoenheitsfehler:
 *  ueber 754 gemessene Steckbriefe war `agentType` immer vorhanden, `name`
 *  in 50 %, `model` in 57 %. Die Karte muss also ohne jedes einzelne Feld
 *  auskommen. */
export interface SubagentRun {
  runId: string;
  name: string | null;
  agentType: string | null;
  description: string | null;
  model: string | null;
  color: string | null;
  teamName: string | null;
  startedAt: string | null;
}

export interface ChatHistoryResponse {
  events: ChatEvent[];
  session: ChatSession;
  hasMore: boolean;
  /** Absent on older backends — consumers must treat that as "cannot switch". */
  capabilities?: ChatCapabilities | null;
  /** Fehlt bei aelteren Backends und ist dann schlicht leer. Kommt nur beim
   *  Handshake, nicht ueber den Live-Strom: ein Subagent, der NACH dem Laden
   *  startet, erscheint erst beim naechsten Abruf. */
  subagentRuns?: SubagentRun[];
  /** The session's newest todo snapshot (whole file, not just this page) or
   *  `null`. Absent on older backends. Live updates arrive as `todo` events. */
  todos?: TodoEvent | null;
}

/** Der Verlauf EINES Subagenten. Wie eine History-Seite, aber ohne
 *  `capabilities` (es gibt nichts zu steuern) und mit dem Steckbrief dabei. */
export interface SubagentHistoryResponse extends ChatHistoryResponse {
  subagent: SubagentRun;
}

/** 404 body shared by /chat/history and /chat/stream when the agent/runtime
 *  has no transcript, no session file yet, or fails the Boss privacy gate. */
export interface NoTranscriptError {
  reason: "no_transcript";
}

/** `request()` (lib/api.ts) has no typed 404 path — it throws a plain
 *  `Error` whose message embeds the raw response body (`API 404: {"reason":
 *  "no_transcript"}`). This checks for that shape so callers (ChatView) can
 *  fall back to the no-transcript empty state even if the sidebar's
 *  `hasTranscript` derivation was wrong or stale (belt and braces — the
 *  backend's `transcript_allowed` gate is the actual source of truth). */
export function isNoTranscriptError(err: unknown): boolean {
  return err instanceof Error && err.message.includes("no_transcript");
}

/** 409 from any chat-input endpoint on an agent whose runtime has no pane to
 *  drive (host agents — Boss, Hermes, Jarvis). Not an error to shout about:
 *  the control is simply unavailable there, and the UI should say so quietly
 *  rather than offer a button that can never work. */
export function isInputNotSupportedError(err: unknown): boolean {
  return err instanceof Error && err.message.includes("input_not_supported");
}

/** 409 from `/chat/effort` when the switch was sent but could not be verified
 *  as applied. This one IS a real failure — the operator asked for something
 *  and it didn't happen, so it needs surfacing. */
export function isEffortSwitchFailedError(err: unknown): boolean {
  return err instanceof Error && err.message.includes("effort_switch_failed");
}

/** 409 from `/chat/effort` when the agent is mid-turn or sitting on an open
 *  prompt — the backend refuses rather than interrupting it. Nothing is broken
 *  and nothing needs fixing; the operator just picked the wrong moment, so this
 *  wants a "not now", not a failure. */
export function isAgentBusyError(err: unknown): boolean {
  return err instanceof Error && err.message.includes("agent_busy");
}

/** 409 from `/chat/input` when the agent's CLI is still booting. A wait, not a
 *  failure — the message hasn't been rejected, it just can't be delivered yet,
 *  so the echo says so and the send is retried once. */
export function isAgentStartingError(err: unknown): boolean {
  return err instanceof Error && err.message.includes("agent_starting");
}

/** 409 from `/chat/input` when the message's first slash command would
 *  install/modify a plugin, marketplace, MCP server or skill. Operator rule
 *  (decided 12.10.2026): that needs an approval card from the phone/chat,
 *  and the card flow does not exist yet — so chat refuses it outright
 *  rather than letting the agent's own CLI act on it unsupervised. Not a
 *  bug, but a real refusal: the echo is withdrawn and the operator is told
 *  why, same as any other rejected send. */
export function isInstallBlockedError(err: unknown): boolean {
  return err instanceof Error && err.message.includes("install_blocked");
}

/** 409 from `/chat/elicitation/{id}` when the question card is no longer
 *  open — answered elsewhere, its turn ended, or the chat restarted. Not a
 *  failure: the card just says so. */
export function isElicitationNotPendingError(err: unknown): boolean {
  return err instanceof Error && err.message.includes("elicitation_not_pending");
}

/** 409 from `/chat/effort` when the CLI itself said no and explained why.
 *  Distinct from `effort_switch_failed` (which means "we could not verify it"):
 *  here the outcome is known and there is a real reason to pass on. */
export function isEffortSwitchRejectedError(err: unknown): boolean {
  return err instanceof Error && err.message.includes("effort_switch_rejected");
}

/**
 * Pulls the CLI's own words out of an error body, so a rejection can be shown
 * as the reason it actually was instead of a generic failure.
 *
 * `request()` throws a plain Error whose message embeds the raw response body
 * (`API 409: {"reason":…,"message":…}`), so the JSON has to be recovered from
 * the string. Never throws: an unparseable body simply yields `null` and the
 * caller falls back to its own wording.
 */
export function extractErrorMessage(err: unknown): string | null {
  if (!(err instanceof Error)) return null;
  const start = err.message.indexOf("{");
  const end = err.message.lastIndexOf("}");
  if (start < 0 || end <= start) return null;
  try {
    const body = JSON.parse(err.message.slice(start, end + 1)) as { message?: unknown };
    return typeof body.message === "string" && body.message.trim().length > 0
      ? body.message.trim()
      : null;
  } catch {
    return null;
  }
}
