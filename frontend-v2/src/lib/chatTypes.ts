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
