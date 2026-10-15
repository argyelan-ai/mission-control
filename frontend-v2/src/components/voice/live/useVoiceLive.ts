"use client";

/**
 * Live voice in the chat — the call itself (voice PR 1) and its two tools
 * (voice PR 2: `send_to_agent`, `get_agent_status`, see tools.ts).
 *
 * Flow: secure-context check → microphone → backend mints a 60 s client
 * secret (`POST /api/v1/voice/live/session`, the OpenAI key stays on the
 * server) → WebRTC offer straight to OpenAI's `/v1/realtime/calls` with that
 * secret → audio both ways, server events on the `oai-events` data channel.
 *
 * Tools: when the model emits a function call on the data channel, the
 * hook posts it to MC (`POST /api/v1/voice/live/tool`, bound to this call's
 * `call_ref`), hands MC's answer back as `function_call_output` and asks the
 * model to speak. `send_to_agent` always carries the operator's verbatim
 * words (UtteranceLog). One tool call runs at a time.
 *
 * Listening along (voice PR 3, progress.ts): while the call is open the chat
 * hands its live stream in (`options.progress`). Milestones become one
 * redacted system item each (`POST /api/v1/voice/live/progress`, MC writes
 * the words); the voice is asked to speak about it only when the floor is
 * free — never over the operator. A success claim in the voice's own words
 * before the agent's final reply gets a correction item (no-success guard).
 *
 * Stop and mute (voice PR 4): `stop_agent` carries the operator's LAST
 * utterance verbatim — MC stops only when that, as a whole, is a stop
 * command, through the chat Stop button's own path; follow-ups the agent's
 * daemon held back come back for the composer (`options.onStopped`).
 * Barge-in needs nothing here: OpenAI's semantic VAD (`interrupt_response`)
 * cancels and truncates only the voice's own answer. Mute turns the mic
 * track off and keeps the call; after `MUTED_MAX_MS` the call hangs up.
 *
 * Cost and brakes (voice PR 6): every `response.done` carries OpenAI's
 * token usage; the hook sums it per call and posts the totals to MC
 * (`POST /api/v1/voice/live/usage`, at most every `USAGE_POST_MS` and once
 * more when the call ends, whatever ended it). MC books the cost with its
 * own prices and may answer "hang up" (daily budget, max length). Silence on
 * both sides for `limits.idle_hangup_s` hangs up; `limits.idle_warn_s`
 * before, a soft tone plays and the bar says so. Any speech resets it.
 *
 * Everything the browser offers (getUserMedia, RTCPeerConnection, fetch,
 * isSecureContext, a tone) comes through `VoiceLiveEnv`, so tests drive the
 * whole call without a browser.
 */

import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { api } from "@/lib/api";
import {
  ACTIVITY_EVENTS,
  DEFAULT_LIMITS,
  DISCONNECT_GRACE_MS,
  INITIAL_VOICE_STATE,
  MUTED_MAX_MS,
  ZERO_USAGE,
  addUsage,
  micErrorKind,
  offerErrorKind,
  openaiCallId,
  sessionErrorKind,
  voiceLiveReducer,
  type RealtimeServerEvent,
  type UsageTotals,
  type VoiceLiveErrorKind,
  type VoiceLiveLimits,
  type VoiceLiveState,
} from "./voiceLiveCore";
import {
  ProgressTracker,
  claimsSuccess,
  contextItemEvent,
  speakDecision,
  type Milestone,
  type ProgressRequest,
  type ProgressSnapshot,
} from "./progress";
import {
  TRANSCRIPT_WAIT_MS,
  UtteranceLog,
  functionCallFromEvent,
  functionOutputEvent,
  toolFailureOutput,
  type FunctionCall,
  type VoiceToolName,
} from "./tools";

export interface VoiceLiveSessionResponse {
  client_secret: string;
  expires_at: number;
  model: string;
  voice: string;
  calls_url: string;
  agent_name: string;
  call_ref: string;
  /** The brakes MC wants for this call (voice PR 6). */
  limits?: VoiceLiveLimits;
}

export interface VoiceUsageRequest {
  call_ref: string;
  agent_id: string;
  usage: UsageTotals;
  ended?: boolean;
  end_reason?: string;
  openai_call_id?: string | null;
}

export interface VoiceUsageResponse {
  call_cost_usd: number;
  today_usd: number;
  budget_usd: number;
  hang_up: "budget_reached" | "max_length" | null;
}

/** A soft notice tone. Made inside the tap that starts the call (iOS only
 *  lets audio play that a gesture began), played before an idle hang-up. */
export interface WarnTone {
  play: () => void;
  close: () => void;
}

export interface VoiceToolRequest {
  call_ref: string;
  agent_id: string;
  name: VoiceToolName;
  arguments: Record<string, unknown>;
  transcript: string;
}

export interface VoiceToolResponse {
  output: Record<string, unknown>;
  sent_text: string | null;
  mid_turn: boolean;
  /** `stop_agent`: the stop went out. */
  stopped?: boolean;
  /** `stop_agent` on a headless agent: follow-ups taken back from its
   *  daemon (oldest first); `null` = nothing taken back. */
  taken_back?: string[] | null;
}

export interface VoiceProgressResponse {
  text: string;
  agent_final: boolean;
}

export interface VoiceLiveEnv {
  isSecureContext: () => boolean;
  getUserMedia: (c: MediaStreamConstraints) => Promise<MediaStream>;
  createPeerConnection: () => RTCPeerConnection;
  createAudioElement: () => HTMLAudioElement;
  mintSession: (agentId: string, lang: "de" | "en") => Promise<VoiceLiveSessionResponse>;
  postOffer: (url: string, secret: string, sdp: string) => Promise<Response>;
  runTool: (body: VoiceToolRequest) => Promise<VoiceToolResponse>;
  progress: (body: { call_ref: string; agent_id: string } & ProgressRequest) => Promise<VoiceProgressResponse>;
  /** Books the call's token totals in MC (voice PR 6). Optional: without
   *  it nothing is booked (tests of other parts). */
  reportUsage?: (body: VoiceUsageRequest) => Promise<VoiceUsageResponse>;
  createWarnTone?: () => WarnTone | null;
  now: () => number;
  setTimeout: (fn: () => void, ms: number) => ReturnType<typeof setTimeout>;
  clearTimeout: (id: ReturnType<typeof setTimeout>) => void;
}

export const browserVoiceLiveEnv: VoiceLiveEnv = {
  isSecureContext: () => typeof window !== "undefined" && window.isSecureContext === true,
  getUserMedia: (c) => {
    if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) {
      // Insecure origins have no mediaDevices at all.
      return Promise.reject(Object.assign(new Error("no mediaDevices"), { name: "SecurityError" }));
    }
    return navigator.mediaDevices.getUserMedia(c);
  },
  createPeerConnection: () => new RTCPeerConnection(),
  createAudioElement: () => {
    const el = document.createElement("audio");
    el.autoplay = true;
    return el;
  },
  mintSession: (agentId, lang) => api.voiceLive.session(agentId, lang),
  postOffer: (url, secret, sdp) =>
    fetch(url, {
      method: "POST",
      body: sdp,
      headers: { Authorization: `Bearer ${secret}`, "Content-Type": "application/sdp" },
    }),
  runTool: (body) => api.voiceLive.tool(body),
  progress: (body) => api.voiceLive.progress(body),
  reportUsage: (body) => api.voiceLive.usage(body),
  createWarnTone: () => {
    if (typeof window === "undefined") return null;
    const Ctx =
      window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctx) return null;
    let ctx: AudioContext;
    try {
      ctx = new Ctx();
    } catch {
      return null;
    }
    return {
      play: () => {
        try {
          void ctx.resume?.();
          const t = ctx.currentTime;
          const osc = ctx.createOscillator();
          const gain = ctx.createGain();
          osc.type = "sine";
          osc.frequency.value = 660;
          gain.gain.setValueAtTime(0, t);
          gain.gain.linearRampToValueAtTime(0.06, t + 0.04);
          gain.gain.linearRampToValueAtTime(0, t + 0.4);
          osc.connect(gain).connect(ctx.destination);
          osc.start(t);
          osc.stop(t + 0.42);
        } catch {
          /* no sound is fine: the bar says it too */
        }
      },
      close: () => void ctx.close?.().catch?.(() => undefined),
    };
  },
  now: () => Date.now(),
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (id) => clearTimeout(id),
};

export interface VoiceLiveCall extends VoiceLiveState {
  /** Opens a call. `agentId` overrides the hook's agent for this one call
   *  (voice PR 5: the app-wide call picks its agent per tap — the chat's
   *  agent, or the lead agent from the navigation mic). */
  start: (agentId?: string) => Promise<void>;
  /** The agent the open (or last) call talks to. */
  agentId: string;
  hangUp: () => void;
  /** Leave the error state without calling again. */
  dismiss: () => void;
  /** Mic on/off; the call stays open (voice PR 4). */
  toggleMute: () => void;
  /** The clock `connectedAt` is measured on (the call bar's elapsed time). */
  now: () => number;
}

export interface VoiceLiveOptions {
  /** A `send_to_agent` reached the agent: `text` is the exact chat message
   *  (the chat echoes it at once), `midTurn` = the agent was working. */
  onForwarded?: (text: string, midTurn: boolean) => void;
  /** The chat's live stream (state, timeline, todos) — what the voice
   *  listens along to (voice PR 3). */
  progress?: ProgressSnapshot;
  /** `stop_agent` stopped the agent's turn (voice PR 4). `takenBack` = the
   *  follow-ups its daemon held, for the composer — like the Stop button;
   *  `null` when nothing was taken back (TUI agents keep their own queue). */
  onStopped?: (takenBack: string[] | null) => void;
  /** Today's voice budget is used up (voice PR 6, live finding 14.10.):
   *  `start` then ends at once with `budget_reached` — no mic prompt, no
   *  session. Read at the tap, so a reset at midnight counts right away. */
  isBudgetReached?: () => boolean;
}

/** How often the progress clock runs while a call is open (final settle,
 *  held tool lines, the periodic "still working"). */
export const PROGRESS_TICK_MS = 1_000;
/** After the operator stops talking the model answers by itself; a held
 *  progress update waits this long so it never races that answer. */
export const AFTER_SPEECH_MS = 1_500;
/** Usage goes to MC at most this often while a call runs (plus once at the
 *  end) — often enough for the daily budget, cheap for the backend. */
export const USAGE_POST_MS = 10_000;

interface Live {
  /** The agent this call talks to — fixed for the call's whole life. */
  agentId: string;
  pc: RTCPeerConnection;
  stream: MediaStream;
  dc: RTCDataChannel;
  audio: HTMLAudioElement;
  timers: ReturnType<typeof setTimeout>[];
  disconnectTimer: ReturnType<typeof setTimeout> | null;
  muteTimer: ReturnType<typeof setTimeout> | null;
  muted: boolean;
  callRef: string;
  utterances: UtteranceLog;
  seenCalls: Set<string>;
  /** Tool calls run one after another, never in parallel. */
  toolChain: Promise<void>;
  /** Re-checked on every finished transcription; return true when done. */
  waiters: (() => boolean)[];
  /** A response is being generated: a `response.create` now would be refused. */
  responseActive: boolean;
  wantResponse: boolean;
  // ── listening along (voice PR 3) ──
  /** The data channel is open: progress items can be sent. */
  connected: boolean;
  tracker: ProgressTracker;
  /** Progress items go out one after another, in order. */
  progressChain: Promise<void>;
  tickTimer: ReturnType<typeof setTimeout> | null;
  userSpeaking: boolean;
  voicePlaying: boolean;
  lastUserSpeechAt: number;
  lastSpokenProgressAt: number;
  /** A progress item waits to be spoken once the floor is free. */
  pendingSpeak: boolean;
  /** The next response is one we asked for to tell progress. */
  narrateNext: boolean;
  narrating: boolean;
  /** Voice items already corrected by the no-success guard. */
  corrected: Set<string>;
  // ── cost and brakes (voice PR 6) ──
  limits: VoiceLiveLimits;
  usage: UsageTotals;
  usageDirty: boolean;
  lastUsagePostAt: number;
  usageInFlight: boolean;
  openaiCallId: string | null;
  idleWarnTimer: ReturnType<typeof setTimeout> | null;
  idleHangTimer: ReturnType<typeof setTimeout> | null;
  idleWarned: boolean;
  tone: WarnTone | null;
}

export function useVoiceLive(
  agentId: string,
  lang: "de" | "en",
  env: VoiceLiveEnv = browserVoiceLiveEnv,
  options: VoiceLiveOptions = {},
): VoiceLiveCall {
  const [state, dispatch] = useReducer(voiceLiveReducer, INITIAL_VOICE_STATE);
  const [callAgent, setCallAgent] = useState(agentId);
  const liveRef = useRef<Live | null>(null);
  const onForwardedRef = useRef(options.onForwarded);
  onForwardedRef.current = options.onForwarded;
  const onStoppedRef = useRef(options.onStopped);
  onStoppedRef.current = options.onStopped;
  const progressSnap = options.progress;
  const progressRef = useRef(progressSnap);
  progressRef.current = progressSnap;
  const budgetRef = useRef(options.isBudgetReached);
  budgetRef.current = options.isBudgetReached;
  // Bumped on every start/hang-up: a slow step of an older attempt (mic
  // prompt, mint, SDP answer) must not resurrect a call the operator ended.
  const attemptRef = useRef(0);

  /** Books the call's totals in MC. The final report goes out whatever
   *  ended the call — hang-up, error, idle, budget — so every call is booked. */
  const postUsage = useCallback(
    (live: Live, final: { reason: string } | null) => {
      if (!env.reportUsage || !live.callRef) return undefined;
      live.usageDirty = false;
      live.lastUsagePostAt = env.now();
      const body: VoiceUsageRequest = {
        call_ref: live.callRef,
        agent_id: live.agentId,
        usage: { ...live.usage },
        ...(live.openaiCallId ? { openai_call_id: live.openaiCallId } : {}),
        ...(final ? { ended: true, end_reason: final.reason } : {}),
      };
      return env.reportUsage(body).catch(() => null);
    },
    [env],
  );

  const teardown = useCallback((reason = "hangup") => {
    const live = liveRef.current;
    liveRef.current = null;
    if (!live) return;
    void postUsage(live, { reason });
    live.tone?.close();
    live.tone = null;
    if (live.idleWarnTimer) env.clearTimeout(live.idleWarnTimer);
    if (live.idleHangTimer) env.clearTimeout(live.idleHangTimer);
    live.idleWarnTimer = null;
    live.idleHangTimer = null;
    live.timers.forEach((t) => env.clearTimeout(t));
    if (live.disconnectTimer) env.clearTimeout(live.disconnectTimer);
    if (live.muteTimer) env.clearTimeout(live.muteTimer);
    live.muteTimer = null;
    if (live.tickTimer) env.clearTimeout(live.tickTimer);
    live.tickTimer = null;
    live.connected = false;
    try {
      if (live.dc.readyState === "open") live.dc.send(JSON.stringify({ type: "session.close" }));
    } catch {
      /* closing anyway */
    }
    try {
      live.dc.close();
    } catch {
      /* already closed */
    }
    live.stream.getTracks().forEach((t) => t.stop());
    live.pc.close();
    live.audio.srcObject = null;
    live.waiters = [];
  }, [env, postUsage]);

  const fail = useCallback(
    (error: VoiceLiveErrorKind) => {
      attemptRef.current += 1;
      teardown(error);
      dispatch({ type: "fail", error });
    },
    [teardown],
  );
  const failRef = useRef(fail);
  failRef.current = fail;

  /** Idle brake: (re)armed whenever someone stops talking; off while anyone
   *  talks. Warning first (tone + notice), then hang up. */
  const armIdle = useCallback(
    (live: Live) => {
      if (liveRef.current !== live) return;
      if (live.idleWarnTimer) env.clearTimeout(live.idleWarnTimer);
      if (live.idleHangTimer) env.clearTimeout(live.idleHangTimer);
      live.idleWarnTimer = null;
      live.idleHangTimer = null;
      if (live.idleWarned) {
        live.idleWarned = false;
        dispatch({ type: "idle_warning", on: false });
      }
      if (live.userSpeaking || live.voicePlaying) return;
      const { idle_hangup_s: hang, idle_warn_s: warn } = live.limits;
      if (warn > 0 && warn < hang) {
        live.idleWarnTimer = env.setTimeout(() => {
          if (liveRef.current !== live) return;
          live.idleWarned = true;
          dispatch({ type: "idle_warning", on: true });
          live.tone?.play();
        }, (hang - warn) * 1000);
      }
      live.idleHangTimer = env.setTimeout(() => {
        if (liveRef.current === live) failRef.current("idle");
      }, hang * 1000);
    },
    [env],
  );

  /** Posts the running totals now and acts on MC's answer. */
  const flushUsage = useCallback(
    (live: Live) => {
      if (live.usageInFlight || !env.reportUsage) return;
      live.usageInFlight = true;
      void Promise.resolve(postUsage(live, null)).then((res) => {
        live.usageInFlight = false;
        if (liveRef.current !== live || !res) return;
        if (res.hang_up === "budget_reached") failRef.current("budget_reached");
        else if (res.hang_up === "max_length") failRef.current("max_length");
      });
    },
    [env, postUsage],
  );

  const sendEvent = useCallback((live: Live, event: object) => {
    if (liveRef.current !== live || live.dc.readyState !== "open") return;
    try {
      live.dc.send(JSON.stringify(event));
    } catch {
      /* channel closing — the call is ending anyway */
    }
  }, []);

  /** Ask the model to speak — now, or right after the current response. */
  const requestResponse = useCallback(
    (live: Live) => {
      if (live.responseActive) {
        live.wantResponse = true;
        return;
      }
      live.responseActive = true;
      sendEvent(live, { type: "response.create" });
    },
    [sendEvent],
  );

  /** Speak a waiting progress item — only when nobody is talking and the
   *  model is not answering the operator. Never interrupts. */
  const flushSpeak = useCallback(
    (live: Live) => {
      if (!live.pendingSpeak || liveRef.current !== live) return;
      const t = env.now();
      if (live.userSpeaking || live.voicePlaying || live.responseActive) return;
      if (t - live.lastUserSpeechAt < AFTER_SPEECH_MS) return;
      live.pendingSpeak = false;
      live.narrateNext = true;
      live.lastSpokenProgressAt = t;
      requestResponse(live);
    },
    [env, requestResponse],
  );

  /** One milestone → one redacted system item (MC writes the words) →
   *  maybe a spoken line. A failure only means the voice knows less. */
  const deliver = useCallback(
    (live: Live, m: Milestone) => {
      live.progressChain = live.progressChain
        .then(async () => {
          if (liveRef.current !== live) return;
          const decision = speakDecision(
            m.speak,
            {
              userSpeaking: live.userSpeaking,
              voiceBusy: live.voicePlaying || live.responseActive,
              lastUserSpeechAt: live.lastUserSpeechAt,
              lastSpokenProgressAt: live.lastSpokenProgressAt,
            },
            env.now(),
          );
          let res: VoiceProgressResponse;
          try {
            res = await env.progress({ call_ref: live.callRef, agent_id: live.agentId, ...m.req });
          } catch {
            return;
          }
          if (liveRef.current !== live || !res?.text) return;
          sendEvent(live, contextItemEvent(res.text));
          if (decision !== "no") {
            live.pendingSpeak = true;
            flushSpeak(live);
          }
        })
        .catch(() => undefined);
    },
    [env, sendEvent, flushSpeak],
  );

  const observeProgress = useCallback(
    (live: Live, snap: ProgressSnapshot | undefined) => {
      if (!snap || !live.connected || liveRef.current !== live) return;
      for (const m of live.tracker.observe(snap, env.now())) deliver(live, m);
    },
    [env, deliver],
  );

  const tickRef = useRef<(live: Live) => void>(() => undefined);
  tickRef.current = (live: Live) => {
    if (liveRef.current !== live || !live.connected) return;
    for (const m of live.tracker.tick(env.now())) deliver(live, m);
    flushSpeak(live);
    if (live.usageDirty && env.now() - live.lastUsagePostAt >= USAGE_POST_MS) flushUsage(live);
    live.tickTimer = env.setTimeout(() => tickRef.current(live), PROGRESS_TICK_MS);
  };

  const waitForTranscript = useCallback(
    (live: Live) =>
      new Promise<void>((resolve) => {
        if (!live.utterances.pending()) {
          resolve();
          return;
        }
        let settled = false;
        const finish = () => {
          if (!settled) {
            settled = true;
            resolve();
          }
          return true;
        };
        live.timers.push(env.setTimeout(finish, TRANSCRIPT_WAIT_MS));
        live.waiters.push(() => (live.utterances.pending() ? settled : finish()));
      }),
    [env],
  );

  const runCall = useCallback(
    async (live: Live, fc: FunctionCall) => {
      if (liveRef.current !== live) return;
      dispatch({ type: "tool_busy", busy: true });
      let output: Record<string, unknown>;
      try {
        let ids: string[] = [];
        let transcript = "";
        if (fc.name === "send_to_agent") {
          await waitForTranscript(live);
          if (liveRef.current !== live) return;
          ({ ids, text: transcript } = live.utterances.take());
        } else if (fc.name === "stop_agent") {
          // Only the last utterance: "Stopp" after "mach erst X" is a stop,
          // and MC must see exactly that, not the two joined.
          await waitForTranscript(live);
          if (liveRef.current !== live) return;
          const last = live.utterances.last();
          ids = last ? [last.id] : [];
          transcript = last?.text ?? "";
        }
        const res = await env.runTool({
          call_ref: live.callRef,
          agent_id: live.agentId,
          name: fc.name,
          arguments: fc.args,
          transcript,
        });
        output = res.output;
        if (fc.name === "stop_agent" && res.stopped === true && liveRef.current === live) {
          // Spent: a later send must not attach "Stopp" as the operator's words.
          live.utterances.consume(ids);
          onStoppedRef.current?.(res.taken_back ?? null);
        }
        if (res.sent_text && liveRef.current === live) {
          live.utterances.consume(ids);
          live.tracker.forwarded(res.mid_turn === true, env.now());
          dispatch({ type: "forwarded", ids });
          onForwardedRef.current?.(res.sent_text, res.mid_turn === true);
        }
      } catch (err) {
        output = toolFailureOutput(fc.name, err);
      } finally {
        if (liveRef.current === live) dispatch({ type: "tool_busy", busy: false });
      }
      if (liveRef.current !== live) return;
      sendEvent(live, functionOutputEvent(fc.callId, output));
      requestResponse(live);
    },
    [env, sendEvent, requestResponse, waitForTranscript],
  );

  /** Bookkeeping for the tools, next to the reducer (which owns the UI). */
  const onToolEvent = useCallback(
    (live: Live, ev: RealtimeServerEvent) => {
      const id = ev.item_id;
      switch (ev.type) {
        case "input_audio_buffer.committed":
          if (id) live.utterances.committed(id);
          break;
        case "conversation.item.input_audio_transcription.delta":
          if (id && ev.delta) live.utterances.delta(id, ev.delta);
          break;
        case "conversation.item.input_audio_transcription.completed":
          if (id) live.utterances.final(id, ev.transcript ?? "");
          live.waiters = live.waiters.filter((w) => !w());
          break;
        case "conversation.item.input_audio_transcription.failed":
          if (id) live.utterances.failed(id);
          live.waiters = live.waiters.filter((w) => !w());
          break;
        case "input_audio_buffer.speech_started":
          live.userSpeaking = true;
          live.lastUserSpeechAt = env.now();
          break;
        case "input_audio_buffer.speech_stopped":
          live.userSpeaking = false;
          live.lastUserSpeechAt = env.now();
          break;
        case "output_audio_buffer.started":
          live.voicePlaying = true;
          break;
        case "output_audio_buffer.stopped":
        case "output_audio_buffer.cleared":
          live.voicePlaying = false;
          flushSpeak(live);
          break;
        case "response.created":
          live.responseActive = true;
          if (live.narrateNext) {
            live.narrateNext = false;
            live.narrating = true;
            dispatch({ type: "narrating", on: true });
          }
          break;
        case "response.done":
          if (ev.response?.usage) {
            live.usage = addUsage(live.usage, ev.response.usage);
            live.usageDirty = true;
          }
          live.responseActive = false;
          if (live.narrating) {
            live.narrating = false;
            dispatch({ type: "narrating", on: false });
          }
          if (live.wantResponse) {
            live.wantResponse = false;
            requestResponse(live);
          } else {
            flushSpeak(live);
          }
          break;
        case "response.output_audio_transcript.done":
          // No-success guard (bauplan B4 layer b): the voice claimed success
          // while the agent still owes its result → correct it at once.
          if (
            id &&
            !live.corrected.has(id) &&
            live.tracker.guardArmed() &&
            claimsSuccess(ev.transcript ?? "")
          ) {
            live.corrected.add(id);
            deliver(live, { req: { kind: "premature_success" }, speak: "always" });
          }
          break;
      }
      if (ACTIVITY_EVENTS.has(ev.type)) armIdle(live);
      const fc = functionCallFromEvent(ev);
      if (fc && !live.seenCalls.has(fc.callId)) {
        live.seenCalls.add(fc.callId);
        live.toolChain = live.toolChain.then(() => runCall(live, fc)).catch(() => undefined);
      }
    },
    [env, requestResponse, runCall, flushSpeak, deliver, armIdle],
  );


  const start = useCallback(async (override?: string) => {
    const target = override || agentId;
    const attempt = ++attemptRef.current;
    setCallAgent(target);
    teardown();
    dispatch({ type: "start" });
    if (!env.isSecureContext()) {
      dispatch({ type: "fail", error: "insecure" });
      return;
    }
    if (budgetRef.current?.()) {
      // MC would refuse the call (402) anyway: say so before asking for
      // the microphone, so a tap never opens a permission prompt for nothing.
      dispatch({ type: "fail", error: "budget_reached" });
      return;
    }
    // Created inside the tap handler so iOS lets it play later.
    const audio = env.createAudioElement();
    let tone: WarnTone | null = null;
    try {
      tone = env.createWarnTone?.() ?? null;
    } catch {
      tone = null;
    }

    let stream: MediaStream;
    try {
      stream = await env.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (err) {
      tone?.close();
      if (attempt === attemptRef.current) dispatch({ type: "fail", error: micErrorKind(err) });
      return;
    }
    const stale = () => {
      if (attempt === attemptRef.current) return false;
      stream.getTracks().forEach((t) => t.stop());
      tone?.close();
      return true;
    };
    if (stale()) return;

    let session: VoiceLiveSessionResponse;
    try {
      session = await env.mintSession(target, lang);
    } catch (err) {
      stream.getTracks().forEach((t) => t.stop());
      tone?.close();
      if (attempt === attemptRef.current) dispatch({ type: "fail", error: sessionErrorKind(err) });
      return;
    }
    if (stale()) return;

    let pc: RTCPeerConnection;
    let dc: RTCDataChannel;
    try {
      pc = env.createPeerConnection();
      dc = pc.createDataChannel("oai-events");
    } catch {
      // No (usable) WebRTC in this browser: end honestly, release the mic.
      stream.getTracks().forEach((t) => t.stop());
      tone?.close();
      dispatch({ type: "fail", error: "unavailable" });
      return;
    }
    const live: Live = {
      agentId: target,
      pc,
      stream,
      dc,
      audio,
      timers: [],
      disconnectTimer: null,
      muteTimer: null,
      muted: false,
      callRef: session.call_ref ?? "",
      utterances: new UtteranceLog(),
      seenCalls: new Set(),
      toolChain: Promise.resolve(),
      waiters: [],
      responseActive: false,
      wantResponse: false,
      connected: false,
      tracker: new ProgressTracker(),
      progressChain: Promise.resolve(),
      tickTimer: null,
      userSpeaking: false,
      voicePlaying: false,
      lastUserSpeechAt: -Infinity,
      lastSpokenProgressAt: -Infinity,
      pendingSpeak: false,
      narrateNext: false,
      narrating: false,
      corrected: new Set(),
      limits: session.limits ?? DEFAULT_LIMITS,
      usage: ZERO_USAGE,
      usageDirty: false,
      lastUsagePostAt: env.now(),
      usageInFlight: false,
      openaiCallId: null,
      idleWarnTimer: null,
      idleHangTimer: null,
      idleWarned: false,
      tone,
    };
    liveRef.current = live;
    dispatch({ type: "limits", limits: live.limits });

    pc.ontrack = (e) => {
      audio.srcObject = e.streams[0] ?? null;
      void audio.play?.()?.catch?.(() => undefined);
    };
    try {
      stream.getTracks().forEach((track) => pc.addTrack(track, stream));
    } catch {
      fail("unavailable");
      return;
    }
    dc.onopen = () => {
      if (liveRef.current !== live) return;
      dispatch({ type: "connected", at: env.now() });
      armIdle(live);
      // Listening along starts here: baseline of the chat as it is now, then
      // the progress clock.
      live.connected = true;
      observeProgress(live, progressRef.current);
      live.tickTimer = env.setTimeout(() => tickRef.current(live), PROGRESS_TICK_MS);
    };
    dc.onmessage = (e: MessageEvent) => {
      if (liveRef.current !== live) return;
      let ev: RealtimeServerEvent;
      try {
        ev = JSON.parse(typeof e.data === "string" ? e.data : "");
      } catch {
        return;
      }
      if (ev && typeof ev.type === "string") {
        dispatch({ type: "server_event", event: ev });
        onToolEvent(live, ev);
      }
    };
    pc.onconnectionstatechange = () => {
      if (liveRef.current !== live) return;
      const s = pc.connectionState;
      if (s === "failed") {
        fail("network");
      } else if (s === "disconnected") {
        if (!live.disconnectTimer) {
          live.disconnectTimer = env.setTimeout(() => {
            if (liveRef.current === live && pc.connectionState !== "connected") fail("network");
          }, DISCONNECT_GRACE_MS);
        }
      } else if (s === "connected" && live.disconnectTimer) {
        env.clearTimeout(live.disconnectTimer);
        live.disconnectTimer = null;
      }
    };
    live.timers.push(
      env.setTimeout(() => {
        if (liveRef.current === live) fail("max_length");
      }, live.limits.max_call_s * 1000),
    );

    try {
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      const resp = await env.postOffer(session.calls_url, session.client_secret, offer.sdp ?? "");
      if (liveRef.current !== live) return;
      if (!resp.ok) {
        const kind = await offerErrorKind(resp);
        if (liveRef.current === live) fail(kind);
        return;
      }
      // OpenAI's id for this call, when the browser may read it: MC then
      // hangs up server-side at max length or budget (voice PR 6).
      try {
        live.openaiCallId = openaiCallId(resp.headers?.get?.("Location"));
      } catch {
        live.openaiCallId = null;
      }
      const answer = await resp.text();
      if (liveRef.current !== live) return;
      await pc.setRemoteDescription({ type: "answer", sdp: answer });
    } catch {
      if (liveRef.current === live) fail("network");
    }
  }, [agentId, lang, env, teardown, fail, onToolEvent, observeProgress, armIdle]);

  const hangUp = useCallback(() => {
    attemptRef.current += 1;
    teardown("hangup");
    dispatch({ type: "hangup" });
  }, [teardown]);

  const dismiss = hangUp;

  /** Mic track off/on. The call (and billing time) stays open, so a muted
   *  call hangs up by itself after MUTED_MAX_MS. The voice can still speak:
   *  mute is the operator's mic, not the speaker. */
  const toggleMute = useCallback(() => {
    const live = liveRef.current;
    if (!live) return;
    const on = !live.muted;
    live.muted = on;
    const tracks = live.stream.getAudioTracks?.() ?? live.stream.getTracks();
    tracks.forEach((t) => {
      t.enabled = !on;
    });
    if (live.muteTimer) env.clearTimeout(live.muteTimer);
    live.muteTimer = on
      ? env.setTimeout(() => {
          if (liveRef.current === live && live.muted) fail("muted_too_long");
        }, MUTED_MAX_MS)
      : null;
    dispatch({ type: "muted", on });
  }, [env, fail]);

  // Every change of the chat's live stream is looked at once.
  useEffect(() => {
    const live = liveRef.current;
    if (live) observeProgress(live, progressSnap);
  }, [progressSnap, observeProgress]);

  // A fatal server event (quota mid-call) flips the reducer to "error" — the
  // connection must close with it, or the mic stays open and billing runs.
  useEffect(() => {
    if (state.status === "error" && liveRef.current) {
      attemptRef.current += 1;
      teardown(state.error ?? "unavailable");
    }
  }, [state.status, state.error, teardown]);

  // Switching the hook's agent or unmounting ends the call. Since voice PR 5
  // the hook lives in the app-wide VoiceLiveCallProvider (AppShell), which
  // passes a fixed agentId and picks the agent per call via `start(id)` — so
  // only leaving the app ends a call there, not leaving a page.
  useEffect(() => {
    return () => {
      attemptRef.current += 1;
      teardown("closed");
      dispatch({ type: "hangup" });
    };
  }, [agentId, teardown]);

  return { ...state, start, hangUp, dismiss, toggleMute, agentId: callAgent, now: env.now };
}
