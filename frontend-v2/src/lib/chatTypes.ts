/**
 * Sessions Chat event types — mirror
 * `backend/app/services/transcript_chat.py` exactly (normalized event dicts
 * emitted by `parse_transcript_line` / `read_history` / `ChatTailerManager`)
 * plus the `state` event from `pane_state.parse_pane_state` (A6). Keep these
 * two files in sync; the backend module docstring is the source of truth.
 */

export interface MessageEvent {
  kind: "message";
  uuid: string;
  ts: string;
  role: "user" | "assistant";
  text: string;
  model: string | null;
  sidechain: boolean;
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
}

/** Emitted by the tailer when the newest `*.jsonl` under the agent's
 *  transcript dir changes (session rollover) — carries no uuid; the
 *  consumer must clear its timeline and refetch history. */
export interface SessionChangedEvent {
  kind: "session_changed";
}

export type ChatEvent =
  | MessageEvent
  | ToolEvent
  | ThinkingEvent
  | CommandEvent
  | UsageEvent
  | StateEvent
  | SessionChangedEvent;

/** The subset of ChatEvent kinds that carry a `uuid` and belong in the
 *  scrollable timeline list, as opposed to `state`/`usage` (side slots) or
 *  `session_changed` (a reset signal, never rendered itself). */
export type TimelineChatEvent = MessageEvent | ToolEvent | ThinkingEvent | CommandEvent;

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
 */
export const SESSION_ONLY_EFFORT_LEVELS = new Set(["max", "ultracode"]);

export function isSessionOnlyEffort(level: string): boolean {
  return SESSION_ONLY_EFFORT_LEVELS.has(level);
}

export interface ChatHistoryResponse {
  events: ChatEvent[];
  session: ChatSession;
  hasMore: boolean;
  /** Absent on older backends — consumers must treat that as "cannot switch". */
  capabilities?: ChatCapabilities | null;
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
