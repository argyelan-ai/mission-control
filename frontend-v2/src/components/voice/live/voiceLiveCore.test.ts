import { describe, expect, it } from "vitest";
import {
  ZERO_USAGE,
  addUsage,
  formatElapsed,
  offerErrorKind,
  openaiCallId,
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

describe("cost and brakes (voice PR 6)", () => {
  it("addUsage sums tokens; a cached count without split goes to text first", () => {
    const one = addUsage(ZERO_USAGE, {
      input_token_details: { text_tokens: 900, audio_tokens: 300, cached_tokens: 1000 },
      output_token_details: { text_tokens: 20, audio_tokens: 400 },
    });
    expect(one).toEqual({
      input_text_tokens: 900,
      input_audio_tokens: 300,
      cached_text_tokens: 900,
      cached_audio_tokens: 100,
      output_text_tokens: 20,
      output_audio_tokens: 400,
    });
    expect(addUsage(one, null)).toBe(one);
    // Garbage from the wire never goes negative or fractional.
    expect(addUsage(ZERO_USAGE, { output_token_details: { audio_tokens: -5, text_tokens: 2.7 } })).toEqual({
      ...ZERO_USAGE,
      output_text_tokens: 2,
    });
  });

  it("openaiCallId reads only a real rtc_ id from the Location header", () => {
    expect(openaiCallId("/v1/realtime/calls/rtc_abc-123")).toBe("rtc_abc-123");
    expect(openaiCallId("https://api.openai.com/v1/realtime/calls/rtc_x?y=1")).toBe("rtc_x");
    expect(openaiCallId(null)).toBeNull();
    expect(openaiCallId("/v1/realtime/calls/../files")).toBeNull();
  });

  it("offerErrorKind tells quota from rate limit", async () => {
    const r = (status: number, body: object) => new Response(JSON.stringify(body), { status });
    expect(await offerErrorKind(r(429, { error: { code: "insufficient_quota" } }))).toBe("no_credits");
    expect(await offerErrorKind(r(429, { error: { code: "rate_limit_exceeded" } }))).toBe("rate_limited");
    expect(await offerErrorKind(new Response("nope", { status: 429 }))).toBe("rate_limited");
    expect(await offerErrorKind(r(500, {}))).toBe("unavailable");
  });

  it("budget_reached from MC is its own, non-retryable error; idle may retry", () => {
    expect(sessionErrorKind(new Error('API 402: {"detail":{"reason":"budget_reached"}}'))).toBe("budget_reached");
    expect(isRetryable("budget_reached")).toBe(false);
    expect(isRetryable("idle")).toBe(true);
  });

  it("idle warning only while a call runs; a failure clears it", () => {
    let s = voiceLiveReducer(live(), { type: "idle_warning", on: true });
    expect(s.idleWarning).toBe(true);
    s = voiceLiveReducer(s, { type: "fail", error: "idle" });
    expect(s.idleWarning).toBe(false);
    expect(voiceLiveReducer(INITIAL_VOICE_STATE, { type: "idle_warning", on: true }).idleWarning).toBe(false);
  });

  it("formatElapsed is m:ss", () => {
    expect(formatElapsed(0)).toBe("0:00");
    expect(formatElapsed(65_400)).toBe("1:05");
    expect(formatElapsed(20 * 60_000)).toBe("20:00");
  });
});
