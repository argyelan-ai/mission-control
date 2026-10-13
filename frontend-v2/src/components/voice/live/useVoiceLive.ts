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
 * Everything the browser offers (getUserMedia, RTCPeerConnection, fetch,
 * isSecureContext) comes through `VoiceLiveEnv`, so tests drive the whole
 * call without a browser.
 */

import { useCallback, useEffect, useReducer, useRef } from "react";
import { api } from "@/lib/api";
import {
  DISCONNECT_GRACE_MS,
  INITIAL_VOICE_STATE,
  MAX_CALL_MS,
  MUTED_MAX_MS,
  micErrorKind,
  sessionErrorKind,
  voiceLiveReducer,
  type RealtimeServerEvent,
  type VoiceLiveErrorKind,
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
  now: () => Date.now(),
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (id) => clearTimeout(id),
};

export interface VoiceLiveCall extends VoiceLiveState {
  start: () => Promise<void>;
  hangUp: () => void;
  /** Leave the error state without calling again. */
  dismiss: () => void;
  /** Mic on/off; the call stays open (voice PR 4). */
  toggleMute: () => void;
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
}

/** How often the progress clock runs while a call is open (final settle,
 *  held tool lines, the periodic "still working"). */
export const PROGRESS_TICK_MS = 1_000;
/** After the operator stops talking the model answers by itself; a held
 *  progress update waits this long so it never races that answer. */
export const AFTER_SPEECH_MS = 1_500;

interface Live {
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
}

export function useVoiceLive(
  agentId: string,
  lang: "de" | "en",
  env: VoiceLiveEnv = browserVoiceLiveEnv,
  options: VoiceLiveOptions = {},
): VoiceLiveCall {
  const [state, dispatch] = useReducer(voiceLiveReducer, INITIAL_VOICE_STATE);
  const liveRef = useRef<Live | null>(null);
  const onForwardedRef = useRef(options.onForwarded);
  onForwardedRef.current = options.onForwarded;
  const onStoppedRef = useRef(options.onStopped);
  onStoppedRef.current = options.onStopped;
  const progressSnap = options.progress;
  const progressRef = useRef(progressSnap);
  progressRef.current = progressSnap;
  // Bumped on every start/hang-up: a slow step of an older attempt (mic
  // prompt, mint, SDP answer) must not resurrect a call the operator ended.
  const attemptRef = useRef(0);

  const teardown = useCallback(() => {
    const live = liveRef.current;
    liveRef.current = null;
    if (!live) return;
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
  }, [env]);

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
            res = await env.progress({ call_ref: live.callRef, agent_id: agentId, ...m.req });
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
    [agentId, env, sendEvent, flushSpeak],
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
          agent_id: agentId,
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
    [agentId, env, sendEvent, requestResponse, waitForTranscript],
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
      const fc = functionCallFromEvent(ev);
      if (fc && !live.seenCalls.has(fc.callId)) {
        live.seenCalls.add(fc.callId);
        live.toolChain = live.toolChain.then(() => runCall(live, fc)).catch(() => undefined);
      }
    },
    [env, requestResponse, runCall, flushSpeak, deliver],
  );

  const fail = useCallback(
    (error: VoiceLiveErrorKind) => {
      attemptRef.current += 1;
      teardown();
      dispatch({ type: "fail", error });
    },
    [teardown],
  );

  const start = useCallback(async () => {
    const attempt = ++attemptRef.current;
    teardown();
    dispatch({ type: "start" });
    if (!env.isSecureContext()) {
      dispatch({ type: "fail", error: "insecure" });
      return;
    }
    // Created inside the tap handler so iOS lets it play later.
    const audio = env.createAudioElement();

    let stream: MediaStream;
    try {
      stream = await env.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (err) {
      if (attempt === attemptRef.current) dispatch({ type: "fail", error: micErrorKind(err) });
      return;
    }
    const stale = () => {
      if (attempt === attemptRef.current) return false;
      stream.getTracks().forEach((t) => t.stop());
      return true;
    };
    if (stale()) return;

    let session: VoiceLiveSessionResponse;
    try {
      session = await env.mintSession(agentId, lang);
    } catch (err) {
      stream.getTracks().forEach((t) => t.stop());
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
      dispatch({ type: "fail", error: "unavailable" });
      return;
    }
    const live: Live = {
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
    };
    liveRef.current = live;

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
      dispatch({ type: "connected" });
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
      }, MAX_CALL_MS),
    );

    try {
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      const resp = await env.postOffer(session.calls_url, session.client_secret, offer.sdp ?? "");
      if (liveRef.current !== live) return;
      if (!resp.ok) {
        fail(resp.status === 429 ? "rate_limited" : "unavailable");
        return;
      }
      const answer = await resp.text();
      if (liveRef.current !== live) return;
      await pc.setRemoteDescription({ type: "answer", sdp: answer });
    } catch {
      if (liveRef.current === live) fail("network");
    }
  }, [agentId, lang, env, teardown, fail, onToolEvent, observeProgress]);

  const hangUp = useCallback(() => {
    attemptRef.current += 1;
    teardown();
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
      teardown();
    }
  }, [state.status, teardown]);

  // A call belongs to one chat: switching agent or leaving the page ends it
  // (PR 5 moves the call into AppShell so it survives navigation).
  useEffect(() => {
    return () => {
      attemptRef.current += 1;
      teardown();
      dispatch({ type: "hangup" });
    };
  }, [agentId, teardown]);

  return { ...state, start, hangUp, dismiss, toggleMute };
}
