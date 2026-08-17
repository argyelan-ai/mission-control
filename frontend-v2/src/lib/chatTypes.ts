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

export interface ChatSession {
  sessionId: string;
  live: boolean;
  startedAt: string | null;
}

export interface ChatHistoryResponse {
  events: ChatEvent[];
  session: ChatSession;
  hasMore: boolean;
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
