/**
 * Live voice in the chat (voice PR 1) — the pure part: call states, the
 * mapping of OpenAI Realtime server events onto them, and error mapping.
 * No React, no WebRTC here, so every transition is unit-tested.
 *
 * The voice is the chat agent's ears and mouth, never its hands (ADR-089 §6).
 * Since PR 2 it can pass the operator's words on (`send_to_agent`, see
 * tools.ts); lines that reached the agent are marked `forwarded`.
 */

export type VoiceLiveStatus = "idle" | "connecting" | "listening" | "speaking" | "error";

export type VoiceLiveErrorKind =
  | "insecure"
  | "mic_denied"
  | "mic_missing"
  | "network"
  | "no_credits"
  | "rate_limited"
  | "unavailable"
  | "max_length"
  | "muted_too_long";

export interface TranscriptLine {
  /** Realtime item id — deltas and the final text of one utterance merge on it. */
  id: string;
  who: "you" | "voice";
  text: string;
  final: boolean;
  /** This utterance reached the agent's chat (voice PR 2). */
  forwarded?: boolean;
}

export interface VoiceLiveState {
  status: VoiceLiveStatus;
  error: VoiceLiveErrorKind | null;
  transcript: TranscriptLine[];
  /** A tool call is running in MC right now ("Gibt weiter…"). */
  passing: boolean;
  /** The voice is telling the agent's progress on its own (voice PR 3). */
  narrating: boolean;
  /** The operator's mic track is off; the call stays open (voice PR 4). */
  muted: boolean;
}

export const INITIAL_VOICE_STATE: VoiceLiveState = {
  status: "idle",
  error: null,
  transcript: [],
  passing: false,
  narrating: false,
  muted: false,
};

/** Hard cap per call on the client (bauplan B6: 20 min). The server-side
 *  timer and the daily budget come with PR 6. */
export const MAX_CALL_MS = 20 * 60 * 1000;
/** Muted this long with the call open: hang up (bauplan B6 — the session
 *  bills time while muted, and a forgotten muted call is pure cost). */
export const MUTED_MAX_MS = 5 * 60 * 1000;
/** `disconnected` may heal by itself; only after this long it counts as lost. */
export const DISCONNECT_GRACE_MS = 5_000;
/** Keep the caption list short — it is a live caption, not a chat log. */
export const MAX_TRANSCRIPT_LINES = 40;

export type VoiceLiveAction =
  | { type: "start" }
  | { type: "connected" }
  | { type: "fail"; error: VoiceLiveErrorKind }
  | { type: "hangup" }
  | { type: "tool_busy"; busy: boolean }
  | { type: "forwarded"; ids: string[] }
  | { type: "narrating"; on: boolean }
  | { type: "muted"; on: boolean }
  | { type: "server_event"; event: RealtimeServerEvent };

/** The subset of Realtime server events PR 1 reads. Unknown events are ignored. */
export interface RealtimeServerEvent {
  type: string;
  item_id?: string;
  /** Function calls (voice PR 2, see tools.ts). */
  call_id?: string;
  name?: string;
  arguments?: string;
  item?: { type?: string; call_id?: string; name?: string; arguments?: string } | null;
  delta?: string;
  transcript?: string;
  error?: { type?: string; code?: string | null; message?: string } | null;
  response?: {
    status?: string;
    status_details?: { error?: { type?: string; code?: string | null } | null } | null;
  } | null;
}

function upsertLine(
  lines: TranscriptLine[],
  id: string,
  who: TranscriptLine["who"],
  patch: (prev: TranscriptLine | undefined) => TranscriptLine,
): TranscriptLine[] {
  const idx = lines.findIndex((l) => l.id === id && l.who === who);
  if (idx === -1) {
    const next = [...lines, patch(undefined)];
    return next.length > MAX_TRANSCRIPT_LINES ? next.slice(next.length - MAX_TRANSCRIPT_LINES) : next;
  }
  const copy = lines.slice();
  copy[idx] = patch(lines[idx]);
  return copy;
}

/** Quota and output failures, in both shapes OpenAI reports them (an `error`
 *  event, or a failed `response.done`). gpt-live reported quota only as
 *  `output_creation_failed` (incident 2026-09-11) — mapped the same way. */
export function realtimeErrorKind(code: string | null | undefined): VoiceLiveErrorKind {
  if (code === "insufficient_quota" || code === "output_creation_failed") return "no_credits";
  if (code === "rate_limit_exceeded") return "rate_limited";
  return "unavailable";
}

/** Server errors that end the call. Others (e.g. a rejected client event)
 *  are noise for the operator and are ignored. */
function isFatalRealtimeError(code: string | null | undefined): boolean {
  return code === "insufficient_quota" || code === "output_creation_failed" || code === "rate_limit_exceeded";
}

function onServerEvent(state: VoiceLiveState, ev: RealtimeServerEvent): VoiceLiveState {
  if (state.status === "idle" || state.status === "error") return state;
  switch (ev.type) {
    case "output_audio_buffer.started":
      return { ...state, status: "speaking" };
    case "output_audio_buffer.stopped":
    case "output_audio_buffer.cleared":
    case "input_audio_buffer.speech_started":
      return state.status === "connecting" ? state : { ...state, status: "listening" };
    case "conversation.item.input_audio_transcription.delta": {
      if (!ev.item_id || !ev.delta) return state;
      return {
        ...state,
        transcript: upsertLine(state.transcript, ev.item_id, "you", (p) => ({
          id: ev.item_id!,
          who: "you",
          text: (p?.text ?? "") + ev.delta,
          final: false,
        })),
      };
    }
    case "conversation.item.input_audio_transcription.completed": {
      if (!ev.item_id) return state;
      const text = (ev.transcript ?? "").trim();
      if (!text) {
        return { ...state, transcript: state.transcript.filter((l) => !(l.id === ev.item_id && l.who === "you")) };
      }
      return {
        ...state,
        transcript: upsertLine(state.transcript, ev.item_id, "you", (p) => ({
          id: ev.item_id!,
          who: "you",
          text,
          final: true,
          forwarded: p?.forwarded,
        })),
      };
    }
    case "response.output_audio_transcript.delta": {
      if (!ev.item_id || !ev.delta) return state;
      return {
        ...state,
        transcript: upsertLine(state.transcript, ev.item_id, "voice", (p) => ({
          id: ev.item_id!,
          who: "voice",
          text: (p?.text ?? "") + ev.delta,
          final: false,
        })),
      };
    }
    case "response.output_audio_transcript.done": {
      if (!ev.item_id) return state;
      return {
        ...state,
        transcript: upsertLine(state.transcript, ev.item_id, "voice", (p) => ({
          id: ev.item_id!,
          who: "voice",
          text: (ev.transcript ?? p?.text ?? "").trim(),
          final: true,
        })),
      };
    }
    case "response.done": {
      const code = ev.response?.status_details?.error?.code;
      if (ev.response?.status === "failed" && isFatalRealtimeError(code)) {
        return { ...state, status: "error", error: realtimeErrorKind(code) };
      }
      return state;
    }
    case "error": {
      const code = ev.error?.code ?? ev.error?.type;
      if (isFatalRealtimeError(code)) return { ...state, status: "error", error: realtimeErrorKind(code) };
      return state;
    }
    default:
      return state;
  }
}

export function voiceLiveReducer(state: VoiceLiveState, action: VoiceLiveAction): VoiceLiveState {
  switch (action.type) {
    case "start":
      return { status: "connecting", error: null, transcript: [], passing: false, narrating: false, muted: false };
    case "connected":
      return state.status === "connecting" ? { ...state, status: "listening" } : state;
    case "fail":
      // The transcript stays: what was said before the drop is still worth reading.
      return { ...state, status: "error", error: action.error, passing: false, narrating: false, muted: false };
    case "tool_busy":
      return state.status === "idle" || state.status === "error" ? state : { ...state, passing: action.busy };
    case "narrating":
      return state.status === "idle" || state.status === "error" || state.narrating === action.on
        ? state
        : { ...state, narrating: action.on };
    case "muted":
      return state.status === "idle" || state.status === "error" || state.muted === action.on
        ? state
        : { ...state, muted: action.on };
    case "forwarded": {
      if (action.ids.length === 0) return state;
      const ids = new Set(action.ids);
      return {
        ...state,
        transcript: state.transcript.map((l) => (l.who === "you" && ids.has(l.id) ? { ...l, forwarded: true } : l)),
      };
    }
    case "hangup":
      return INITIAL_VOICE_STATE;
    case "server_event":
      return onServerEvent(state, action.event);
  }
}

/** Errors from `getUserMedia`. */
export function micErrorKind(err: unknown): VoiceLiveErrorKind {
  const name = (err as { name?: string } | null)?.name;
  if (name === "NotAllowedError" || name === "SecurityError") return "mic_denied";
  if (name === "NotFoundError" || name === "OverconstrainedError") return "mic_missing";
  return "unavailable";
}

/** Errors from `POST /api/v1/voice/live/session`. `request()` throws
 *  `Error("API <status>: <body>")`; the body carries `{detail:{reason}}`. */
export function sessionErrorKind(err: unknown): VoiceLiveErrorKind {
  const msg = err instanceof Error ? err.message : String(err ?? "");
  const m = /^API (\d+): ([\s\S]*)$/.exec(msg);
  if (!m) return "network"; // fetch itself failed: no network
  let reason = "";
  try {
    reason = JSON.parse(m[2])?.detail?.reason ?? "";
  } catch {
    /* non-JSON body */
  }
  if (reason === "no_credits") return "no_credits";
  if (reason === "rate_limited") return "rate_limited";
  if (reason === "upstream_unreachable") return "network";
  return "unavailable";
}

/** i18n key (namespace `voiceLive`) for each error. */
export const ERROR_MESSAGE_KEY: Record<VoiceLiveErrorKind, string> = {
  insecure: "errInsecure",
  mic_denied: "errMicDenied",
  mic_missing: "errMicMissing",
  network: "errNetwork",
  no_credits: "errNoCredits",
  rate_limited: "errRateLimited",
  unavailable: "errUnavailable",
  max_length: "errMaxLength",
  muted_too_long: "errMutedTooLong",
};

/** Errors where trying again can help (shows a retry button). */
export function isRetryable(kind: VoiceLiveErrorKind): boolean {
  return (
    kind === "network" ||
    kind === "rate_limited" ||
    kind === "unavailable" ||
    kind === "max_length" ||
    kind === "muted_too_long"
  );
}
