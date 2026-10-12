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
  micErrorKind,
  sessionErrorKind,
  voiceLiveReducer,
  type RealtimeServerEvent,
  type VoiceLiveErrorKind,
  type VoiceLiveState,
} from "./voiceLiveCore";
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
}

export interface VoiceLiveEnv {
  isSecureContext: () => boolean;
  getUserMedia: (c: MediaStreamConstraints) => Promise<MediaStream>;
  createPeerConnection: () => RTCPeerConnection;
  createAudioElement: () => HTMLAudioElement;
  mintSession: (agentId: string, lang: "de" | "en") => Promise<VoiceLiveSessionResponse>;
  postOffer: (url: string, secret: string, sdp: string) => Promise<Response>;
  runTool: (body: VoiceToolRequest) => Promise<VoiceToolResponse>;
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
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (id) => clearTimeout(id),
};

export interface VoiceLiveCall extends VoiceLiveState {
  start: () => Promise<void>;
  hangUp: () => void;
  /** Leave the error state without calling again. */
  dismiss: () => void;
}

export interface VoiceLiveOptions {
  /** A `send_to_agent` reached the agent: `text` is the exact chat message
   *  (the chat echoes it at once), `midTurn` = the agent was working. */
  onForwarded?: (text: string, midTurn: boolean) => void;
}

interface Live {
  pc: RTCPeerConnection;
  stream: MediaStream;
  dc: RTCDataChannel;
  audio: HTMLAudioElement;
  timers: ReturnType<typeof setTimeout>[];
  disconnectTimer: ReturnType<typeof setTimeout> | null;
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
  // Bumped on every start/hang-up: a slow step of an older attempt (mic
  // prompt, mint, SDP answer) must not resurrect a call the operator ended.
  const attemptRef = useRef(0);

  const teardown = useCallback(() => {
    const live = liveRef.current;
    liveRef.current = null;
    if (!live) return;
    live.timers.forEach((t) => env.clearTimeout(t));
    if (live.disconnectTimer) env.clearTimeout(live.disconnectTimer);
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
        }
        const res = await env.runTool({
          call_ref: live.callRef,
          agent_id: agentId,
          name: fc.name,
          arguments: fc.args,
          transcript,
        });
        output = res.output;
        if (res.sent_text && liveRef.current === live) {
          live.utterances.consume(ids);
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
        case "response.created":
          live.responseActive = true;
          break;
        case "response.done":
          live.responseActive = false;
          if (live.wantResponse) {
            live.wantResponse = false;
            requestResponse(live);
          }
          break;
      }
      const fc = functionCallFromEvent(ev);
      if (fc && !live.seenCalls.has(fc.callId)) {
        live.seenCalls.add(fc.callId);
        live.toolChain = live.toolChain.then(() => runCall(live, fc)).catch(() => undefined);
      }
    },
    [requestResponse, runCall],
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
      callRef: session.call_ref ?? "",
      utterances: new UtteranceLog(),
      seenCalls: new Set(),
      toolChain: Promise.resolve(),
      waiters: [],
      responseActive: false,
      wantResponse: false,
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
      if (liveRef.current === live) dispatch({ type: "connected" });
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
  }, [agentId, lang, env, teardown, fail, onToolEvent]);

  const hangUp = useCallback(() => {
    attemptRef.current += 1;
    teardown();
    dispatch({ type: "hangup" });
  }, [teardown]);

  const dismiss = hangUp;

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

  return { ...state, start, hangUp, dismiss };
}
