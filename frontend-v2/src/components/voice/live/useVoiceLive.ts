"use client";

/**
 * Live voice in the chat — the call itself (voice PR 1).
 *
 * Flow: secure-context check → microphone → backend mints a 60 s client
 * secret (`POST /api/v1/voice/live/session`, the OpenAI key stays on the
 * server) → WebRTC offer straight to OpenAI's `/v1/realtime/calls` with that
 * secret → audio both ways, server events on the `oai-events` data channel.
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

export interface VoiceLiveSessionResponse {
  client_secret: string;
  expires_at: number;
  model: string;
  voice: string;
  calls_url: string;
  agent_name: string;
}

export interface VoiceLiveEnv {
  isSecureContext: () => boolean;
  getUserMedia: (c: MediaStreamConstraints) => Promise<MediaStream>;
  createPeerConnection: () => RTCPeerConnection;
  createAudioElement: () => HTMLAudioElement;
  mintSession: (agentId: string, lang: "de" | "en") => Promise<VoiceLiveSessionResponse>;
  postOffer: (url: string, secret: string, sdp: string) => Promise<Response>;
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
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (id) => clearTimeout(id),
};

export interface VoiceLiveCall extends VoiceLiveState {
  start: () => Promise<void>;
  hangUp: () => void;
  /** Leave the error state without calling again. */
  dismiss: () => void;
}

interface Live {
  pc: RTCPeerConnection;
  stream: MediaStream;
  dc: RTCDataChannel;
  audio: HTMLAudioElement;
  timers: ReturnType<typeof setTimeout>[];
  disconnectTimer: ReturnType<typeof setTimeout> | null;
}

export function useVoiceLive(
  agentId: string,
  lang: "de" | "en",
  env: VoiceLiveEnv = browserVoiceLiveEnv,
): VoiceLiveCall {
  const [state, dispatch] = useReducer(voiceLiveReducer, INITIAL_VOICE_STATE);
  const liveRef = useRef<Live | null>(null);
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
  }, [env]);

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
    const live: Live = { pc, stream, dc, audio, timers: [], disconnectTimer: null };
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
      if (ev && typeof ev.type === "string") dispatch({ type: "server_event", event: ev });
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
  }, [agentId, lang, env, teardown, fail]);

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
