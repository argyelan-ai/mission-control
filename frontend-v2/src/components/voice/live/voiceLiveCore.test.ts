import { describe, expect, it } from "vitest";
import {
  INITIAL_VOICE_STATE,
  MAX_TRANSCRIPT_LINES,
  isRetryable,
  micErrorKind,
  sessionErrorKind,
  voiceLiveReducer,
  type RealtimeServerEvent,
  type VoiceLiveState,
} from "./voiceLiveCore";

const ev = (event: RealtimeServerEvent) => ({ type: "server_event" as const, event });
const live = (): VoiceLiveState =>
  voiceLiveReducer(voiceLiveReducer(INITIAL_VOICE_STATE, { type: "start" }), { type: "connected" });

describe("voiceLiveReducer — call states", () => {
  it("idle → connecting → listening → speaking → listening", () => {
    let s = voiceLiveReducer(INITIAL_VOICE_STATE, { type: "start" });
    expect(s.status).toBe("connecting");
    s = voiceLiveReducer(s, { type: "connected" });
    expect(s.status).toBe("listening");
    s = voiceLiveReducer(s, ev({ type: "output_audio_buffer.started" }));
    expect(s.status).toBe("speaking");
    s = voiceLiveReducer(s, ev({ type: "output_audio_buffer.stopped" }));
    expect(s.status).toBe("listening");
  });

  it("barge-in: operator speech while the voice talks → listening", () => {
    let s = voiceLiveReducer(live(), ev({ type: "output_audio_buffer.started" }));
    s = voiceLiveReducer(s, ev({ type: "input_audio_buffer.speech_started" }));
    expect(s.status).toBe("listening");
  });

  it("server events before the channel opens do not skip 'connecting'", () => {
    const s = voiceLiveReducer(
      voiceLiveReducer(INITIAL_VOICE_STATE, { type: "start" }),
      ev({ type: "input_audio_buffer.speech_started" }),
    );
    expect(s.status).toBe("connecting");
  });

  it("events after hang-up are ignored", () => {
    const s = voiceLiveReducer(INITIAL_VOICE_STATE, ev({ type: "output_audio_buffer.started" }));
    expect(s).toBe(INITIAL_VOICE_STATE);
  });

  it("hangup resets everything", () => {
    const s = voiceLiveReducer(live(), { type: "hangup" });
    expect(s).toEqual(INITIAL_VOICE_STATE);
  });

  it("fail keeps the transcript", () => {
    let s = voiceLiveReducer(
      live(),
      ev({ type: "conversation.item.input_audio_transcription.completed", item_id: "a", transcript: "Hallo" }),
    );
    s = voiceLiveReducer(s, { type: "fail", error: "network" });
    expect(s.status).toBe("error");
    expect(s.error).toBe("network");
    expect(s.transcript).toHaveLength(1);
  });
});

describe("voiceLiveReducer — transcript", () => {
  it("operator line: deltas then the final text, merged on the item id", () => {
    let s = voiceLiveReducer(
      live(),
      ev({ type: "conversation.item.input_audio_transcription.delta", item_id: "u1", delta: "Hal" }),
    );
    s = voiceLiveReducer(s, ev({ type: "conversation.item.input_audio_transcription.delta", item_id: "u1", delta: "lo" }));
    expect(s.transcript).toEqual([{ id: "u1", who: "you", text: "Hallo", final: false }]);
    s = voiceLiveReducer(
      s,
      ev({ type: "conversation.item.input_audio_transcription.completed", item_id: "u1", transcript: " Hallo, hörst du mich? " }),
    );
    expect(s.transcript).toEqual([{ id: "u1", who: "you", text: "Hallo, hörst du mich?", final: true }]);
  });

  it("an empty final transcript removes the half line", () => {
    let s = voiceLiveReducer(
      live(),
      ev({ type: "conversation.item.input_audio_transcription.delta", item_id: "u1", delta: "äh" }),
    );
    s = voiceLiveReducer(s, ev({ type: "conversation.item.input_audio_transcription.completed", item_id: "u1", transcript: "" }));
    expect(s.transcript).toEqual([]);
  });

  it("voice line: transcript deltas then done", () => {
    let s = voiceLiveReducer(live(), ev({ type: "response.output_audio_transcript.delta", item_id: "v1", delta: "Ja, " }));
    s = voiceLiveReducer(s, ev({ type: "response.output_audio_transcript.delta", item_id: "v1", delta: "ich höre dich." }));
    s = voiceLiveReducer(s, ev({ type: "response.output_audio_transcript.done", item_id: "v1", transcript: "Ja, ich höre dich." }));
    expect(s.transcript).toEqual([{ id: "v1", who: "voice", text: "Ja, ich höre dich.", final: true }]);
  });

  it("keeps the order of lines and caps the list", () => {
    let s = live();
    for (let i = 0; i < MAX_TRANSCRIPT_LINES + 5; i++) {
      s = voiceLiveReducer(
        s,
        ev({ type: "conversation.item.input_audio_transcription.completed", item_id: `u${i}`, transcript: `line ${i}` }),
      );
    }
    expect(s.transcript).toHaveLength(MAX_TRANSCRIPT_LINES);
    expect(s.transcript.at(-1)?.text).toBe(`line ${MAX_TRANSCRIPT_LINES + 4}`);
  });
});

describe("voiceLiveReducer — server errors", () => {
  it("quota mid-call ends the call with no_credits", () => {
    const s = voiceLiveReducer(live(), ev({ type: "error", error: { type: "invalid_request_error", code: "insufficient_quota" } }));
    expect(s).toMatchObject({ status: "error", error: "no_credits" });
  });

  it("a failed response with output_creation_failed also means no credits", () => {
    const s = voiceLiveReducer(
      live(),
      ev({ type: "response.done", response: { status: "failed", status_details: { error: { code: "output_creation_failed" } } } }),
    );
    expect(s).toMatchObject({ status: "error", error: "no_credits" });
  });

  it("a non-fatal error event (rejected client event) keeps the call", () => {
    const s = voiceLiveReducer(live(), ev({ type: "error", error: { type: "invalid_request_error", code: "unknown_parameter" } }));
    expect(s.status).toBe("listening");
  });
});

describe("error mapping", () => {
  it("microphone errors", () => {
    expect(micErrorKind({ name: "NotAllowedError" })).toBe("mic_denied");
    expect(micErrorKind({ name: "SecurityError" })).toBe("mic_denied");
    expect(micErrorKind({ name: "NotFoundError" })).toBe("mic_missing");
    expect(micErrorKind(new Error("x"))).toBe("unavailable");
  });

  it("session endpoint errors", () => {
    expect(sessionErrorKind(new Error('API 402: {"detail":{"reason":"no_credits"}}'))).toBe("no_credits");
    expect(sessionErrorKind(new Error('API 429: {"detail":{"reason":"rate_limited"}}'))).toBe("rate_limited");
    expect(sessionErrorKind(new Error('API 502: {"detail":{"reason":"upstream_unreachable"}}'))).toBe("network");
    expect(sessionErrorKind(new Error('API 404: {"detail":"voice_live_disabled"}'))).toBe("unavailable");
    expect(sessionErrorKind(new TypeError("Failed to fetch"))).toBe("network");
  });

  it("retry only where it can help", () => {
    expect(isRetryable("network")).toBe(true);
    expect(isRetryable("mic_denied")).toBe(false);
    expect(isRetryable("insecure")).toBe(false);
    expect(isRetryable("no_credits")).toBe(false);
  });
});
