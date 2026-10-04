// frontend-v2/src/components/shared/BrowserLiveView.tsx
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { Maximize2, Minimize2, MonitorOff, RefreshCw, RotateCcw, Loader2, X } from "lucide-react";
import { api, browserLiveWsUrl } from "@/lib/api";
import { C, alpha } from "@/lib/colors";
import { StatusDot } from "@/components/shared/StatusDot";
import type { BrowserLiveTarget } from "@/lib/types";

// ── Types ──────────────────────────────────────────────────────────────────

type FrameMessage = { type: "frame"; data: string; metadata?: Record<string, unknown> };
// The server sends a machine-readable `code`, never a free-text message —
// the client is bilingual (i18n) and must never render server English
// verbatim (finding: it used to send a hardcoded English sentence that
// showed up untranslated in the German UI).
type StatusMessage = { type: "status"; code?: string; message?: string };
type AttachedMessage = { type: "attached"; target: BrowserLiveTarget };
type TargetsMessage = {
  type: "targets";
  targets: BrowserLiveTarget[];
  activeId: string | null;
  followedId: string | null;
};
type ServerMessage = FrameMessage | StatusMessage | AttachedMessage | TargetsMessage;

function isServerMessage(x: unknown): x is ServerMessage {
  return !!x && typeof x === "object" && "type" in x;
}

// ── WebSocket hook ─────────────────────────────────────────────────────────
//
// View-only: the only messages we ever send are steering ({"follow": bool},
// {"select": "<id>"}) — never forwarded to Chromium by the server either.
// `connectKey` bumps to force a fresh connection (manual Reconnect).

interface LiveSocketState {
  frameSrc: string | null;
  // A status CODE, never free text — mapped to a translation key by the
  // caller (component), which is the only place that has `t()`.
  statusCode: string | null;
  connState: "connecting" | "open" | "closed";
  targets: BrowserLiveTarget[] | null;
  activeId: string | null;
  followedId: string | null;
  attachedTitle: string | null;
  select: (id: string) => void;
  setFollow: (on: boolean) => void;
}

function useBrowserLiveSocket(
  enabled: boolean,
  connectKey: number,
  following: boolean,
): LiveSocketState {
  const wsRef = useRef<WebSocket | null>(null);
  const [frameSrc, setFrameSrc] = useState<string | null>(null);
  const [statusCode, setStatusCode] = useState<string | null>(null);
  const [connState, setConnState] = useState<"connecting" | "open" | "closed">("connecting");
  // `null` until the first server `targets` push — distinct from "[]", which
  // means the agent browser really has no open tabs right now (finding: a
  // momentarily-empty push must not make the UI fall back to the stale
  // first-load list and show tabs that no longer exist).
  const [targets, setTargets] = useState<BrowserLiveTarget[] | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [followedId, setFollowedId] = useState<string | null>(null);
  const [attachedTitle, setAttachedTitle] = useState<string | null>(null);

  // Read inside the connect effect without making `following`/`followedId`
  // reconnect triggers themselves — only `connectKey` does that. This is
  // what lets a reconnect (visibilitychange back to visible, the manual
  // Reconnect button) resume in whatever follow state the UI is currently
  // showing instead of always opening with the server's default (follow=1,
  // no target) — finding: a reconnect could silently start following again
  // even while the picker still showed a hand-picked tab with Follow off.
  const followingRef = useRef(following);
  const followedIdRef = useRef<string | null>(null);
  useEffect(() => {
    followingRef.current = following;
  }, [following]);
  useEffect(() => {
    followedIdRef.current = followedId;
  }, [followedId]);

  useEffect(() => {
    if (!enabled) return;
    setFrameSrc(null);
    setStatusCode(null);
    setConnState("connecting");

    let cancelled = false;
    let ws: WebSocket | null = null;

    // Single-use stream ticket instead of the login token in the URL.
    const wantFollow = followingRef.current;
    const wantTarget = wantFollow ? undefined : (followedIdRef.current ?? undefined);
    browserLiveWsUrl(wantTarget, { follow: wantFollow }).then(
      (url) => {
        if (cancelled) return;
        ws = openSocket(url);
      },
      () => {
        if (cancelled) return;
        setStatusCode((prev) => prev ?? "connectionError");
        setConnState("closed");
      },
    );

    function openSocket(url: string): WebSocket {
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => setConnState("open");

      ws.onmessage = (evt) => {
        let parsed: unknown;
        try {
          parsed = JSON.parse(evt.data as string);
        } catch {
          return;
        }
        if (!isServerMessage(parsed)) return;
        if (parsed.type === "frame") {
          setFrameSrc(`data:image/jpeg;base64,${parsed.data}`);
          // A live frame is proof the stream is healthy — any stale status
          // (e.g. a "no_page"/"connect_error" from before this reconnect)
          // must not linger over it (finding: statusMessage was cleared
          // only on reconnect, never on 'attached'/'frame', so it sat at
          // the bottom of the viewport for the whole rest of the session).
          setStatusCode(null);
        } else if (parsed.type === "status") {
          setStatusCode(parsed.code ?? null);
        } else if (parsed.type === "attached") {
          setAttachedTitle(parsed.target?.title || parsed.target?.url || null);
          setFrameSrc(null);
          setStatusCode(null);
        } else if (parsed.type === "targets") {
          const nextTargets = parsed.targets ?? [];
          setTargets(nextTargets);
          setActiveId(parsed.activeId ?? null);
          setFollowedId(parsed.followedId ?? null);
          // A genuinely-empty push (every tab closed) must drop the frozen
          // last frame instead of leaving it on screen forever (finding:
          // `hasFrame` stayed true after the last tab died since nothing
          // ever cleared `frameSrc` again).
          if (nextTargets.length === 0) {
            setFrameSrc(null);
          }
        }
      };

      ws.onerror = () => {
        setStatusCode((prev) => prev ?? "connectionError");
      };

      ws.onclose = () => {
        setConnState("closed");
      };
      return ws;
    }

    return () => {
      cancelled = true;
      ws?.close(1000);
      wsRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, connectKey]);

  const send = useCallback((payload: Record<string, unknown>) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
    }
  }, []);

  const select = useCallback(
    (id: string) => {
      setFollowedId(id);
      send({ select: id });
    },
    [send],
  );

  const setFollow = useCallback(
    (on: boolean) => {
      send({ follow: on });
    },
    [send],
  );

  return { frameSrc, statusCode, connState, targets, activeId, followedId, attachedTitle, select, setFollow };
}

function shortTitle(t: BrowserLiveTarget): string {
  if (t.title) return t.title;
  if (t.url) {
    try {
      return new URL(t.url).hostname || t.url;
    } catch {
      return t.url;
    }
  }
  return t.id;
}

// ── Main component ───────────────────────────────────────────────────────────

export function BrowserLiveView() {
  const t = useTranslations("browserLive");
  const [connectKey, setConnectKey] = useState(0);
  const [connect, setConnect] = useState(true); // PR A1: connects on open, no click needed
  const [following, setFollowingState] = useState(true);
  const [fullscreen, setFullscreen] = useState(false);
  const [justSwitchedTitle, setJustSwitchedTitle] = useState<string | null>(null);
  const switchHintTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // First paint + fallback while the WS is down — not a 15s poll that fights
  // the live `targets` push once connected (that was the old bug: the panel
  // only ever refreshed on a manual tap).
  const {
    data: initialTargets = [],
    isLoading,
    isError,
    error,
    refetch,
    isFetching,
  } = useQuery({
    queryKey: ["browser-live", "targets"],
    queryFn: () => api.browserLive.targets(),
    refetchInterval: connect ? false : 5_000,
  });

  const {
    frameSrc,
    statusCode,
    connState,
    targets: wsTargets,
    activeId,
    followedId,
    attachedTitle,
    select,
    setFollow,
  } = useBrowserLiveSocket(connect, connectKey, following);

  // Machine status code → translated text, with an unknown/legacy code
  // falling back to a generic message instead of silently rendering
  // nothing (or, worse, raw server English — the thing this fixes).
  const statusMessage = statusCode
    ? t.has(`status.${statusCode}`)
      ? t(`status.${statusCode}` as Parameters<typeof t>[0])
      : t("status.unknown")
    : null;

  // wsTargets is `null` until the first server push, so a push that is
  // legitimately empty (every tab closed) is never masked by the stale
  // first-load `initialTargets` list — see useBrowserLiveSocket's comment.
  const targets = wsTargets ?? initialTargets;

  // Pause the stream when the tab/panel isn't visible, reconnect on return —
  // cheap screencasts are still CPU on the shared cdp-browser for no reason.
  useEffect(() => {
    function onVisibility() {
      if (document.visibilityState === "visible") {
        setConnect(true);
        setConnectKey((k) => k + 1);
      } else {
        setConnect(false);
      }
    }
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  // Esc closes fullscreen (DESIGN.md / bauplan A1 item 4 — desktop keyboard).
  useEffect(() => {
    if (!fullscreen) return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setFullscreen(false);
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [fullscreen]);

  // Brief "New tab: <title>" hint when the followed target switches on its own.
  const prevAttached = useRef<string | null>(null);
  useEffect(() => {
    if (attachedTitle && attachedTitle !== prevAttached.current && prevAttached.current !== null) {
      setJustSwitchedTitle(attachedTitle);
      if (switchHintTimer.current) clearTimeout(switchHintTimer.current);
      switchHintTimer.current = setTimeout(() => setJustSwitchedTitle(null), 2000);
    }
    prevAttached.current = attachedTitle;
    return () => {
      if (switchHintTimer.current) clearTimeout(switchHintTimer.current);
    };
  }, [attachedTitle]);

  const handleReconnect = useCallback(() => {
    setConnectKey((k) => k + 1);
    setConnect(true);
  }, []);

  const handleSelect = useCallback(
    (id: string) => {
      setFollowingState(false);
      select(id);
    },
    [select],
  );

  const handleToggleFollow = useCallback(() => {
    // Compute the next state outside the setState updater — a setState
    // updater must be pure, but this one also sent over the socket as a
    // side effect; harmless under StrictMode's double-invoke (sends the
    // same steering message twice) but impure for no reason (cleanup
    // finding). `following` is read fresh via the functional form below
    // only for the value actually stored.
    setFollowingState((prev) => !prev);
    setFollow(!following);
  }, [following, setFollow]);

  const hasFrame = connect && frameSrc !== null;
  const streamEnded = connect && connState === "closed";
  const shownId = followedId ?? activeId;

  // ── Empty states ───────────────────────────────────────────────────────

  if (isLoading && targets.length === 0) {
    return (
      <div className="flex items-center justify-center h-full min-h-[200px]">
        <Loader2 size={16} className="animate-spin" style={{ color: C.textMuted }} />
      </div>
    );
  }

  // Only "the agent browser isn't reachable at all" means `notRunning`. A
  // live, open WS that explicitly pushed an empty `targets` list (the
  // browser IS running, it just has zero open tabs right now) is a
  // different, far less alarming state — showing `notRunning` there used to
  // tell the operator the agent browser was down while it was running fine
  // (finding, round 4).
  const wsConnectedWithNoTabs = connect && connState === "open" && wsTargets !== null && wsTargets.length === 0;

  if ((isError || targets.length === 0) && !hasFrame) {
    return (
      <div className="flex flex-col items-center justify-center h-full min-h-[200px] gap-3 px-6 text-center">
        <MonitorOff size={28} style={{ color: C.textMuted, opacity: 0.3 }} />
        <p className="text-[11px] max-w-xs" style={{ color: C.textMuted }}>
          {isError
            ? `${t("notRunning")} (${(error as Error)?.message ?? "unreachable"})`
            : wsConnectedWithNoTabs
              ? t("status.no_page")
              : t("notRunning")}
        </p>
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="flex items-center gap-1.5 text-[10px] px-2.5 py-1.5 rounded-md transition-colors disabled:opacity-40"
          style={{
            background: "transparent",
            border: `1px solid ${C.border}`,
            color: C.textSecondary,
          }}
        >
          <RefreshCw size={11} className={isFetching ? "animate-spin" : ""} />
          {t("refresh")}
        </button>
      </div>
    );
  }

  const viewport = (
    <div
      className="relative flex-1 min-h-0 flex items-center justify-center overflow-hidden"
      style={{ background: "var(--color-bg-base)" }}
    >
      {connect && frameSrc && (
        // eslint-disable-next-line @next/next/no-img-element -- data: URL, not a Next-optimizable asset
        <img
          src={frameSrc}
          alt={t("liveViewAlt")}
          className="w-full h-full object-contain"
        />
      )}

      {connect && !frameSrc && !streamEnded && (
        <div className="flex flex-col items-center gap-2">
          <Loader2 size={18} className="animate-spin" style={{ color: C.textMuted }} />
          <p className="text-[11px]" style={{ color: C.textMuted }}>
            {t("connecting")}
          </p>
        </div>
      )}

      {!connect && (
        <div className="flex flex-col items-center gap-2">
          <Loader2 size={18} className="animate-spin" style={{ color: C.textMuted }} />
          <p className="text-[11px]" style={{ color: C.textMuted }}>
            {t("connecting")}
          </p>
        </div>
      )}

      {connect && streamEnded && (
        <div className="flex flex-col items-center gap-2 px-6 text-center">
          <MonitorOff size={24} style={{ color: C.textMuted, opacity: 0.4 }} />
          <p className="text-[11px]" style={{ color: C.textMuted }}>
            {t("streamEnded")}
            {statusMessage ? ` — ${statusMessage}` : ""}
          </p>
          <button
            onClick={handleReconnect}
            className="flex items-center gap-1.5 text-[10px] px-2.5 py-1.5 rounded-md transition-colors"
            style={{ background: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}
          >
            <RotateCcw size={11} />
            {t("reconnect")}
          </button>
        </div>
      )}

      {connect && statusMessage && !streamEnded && (
        <div
          className="absolute bottom-2 left-2 right-2 text-[10px] px-2.5 py-1.5 rounded-md"
          style={{ background: alpha(C.scrim, 0.6), color: C.textSecondary, border: `1px solid ${C.border}` }}
        >
          {statusMessage}
        </div>
      )}

      {justSwitchedTitle && (
        <div
          className="absolute top-2 left-2 right-2 text-[10px] px-2.5 py-1.5 rounded-md text-center truncate"
          style={{ background: alpha(C.scrim, 0.7), color: C.textPrimary, border: `1px solid ${C.border}` }}
        >
          {t("newTab", { title: justSwitchedTitle })}
        </div>
      )}

      {fullscreen && (
        <button
          onClick={() => setFullscreen(false)}
          aria-label={t("closeFullscreen")}
          className="absolute top-2 right-2 flex items-center justify-center rounded-md"
          style={{
            width: 44,
            height: 44,
            background: alpha(C.scrim, 0.7),
            color: C.textPrimary,
            border: `1px solid ${C.border}`,
          }}
        >
          <X size={18} />
        </button>
      )}
    </div>
  );

  const currentTarget = targets.find((tg) => tg.id === shownId);

  const header = (
    <div
      className="flex items-center gap-2 px-3 py-2 border-b shrink-0 flex-wrap"
      style={{ borderColor: C.border }}
    >
      <label htmlFor="browser-live-target" className="sr-only">
        {t("pageLabel")}
      </label>
      {/* min-h-11 = 44px hit area (DESIGN.md K11) around an 11px visual row;
          text-base (16px) so iOS doesn't auto-zoom on tap. */}
      <select
        id="browser-live-target"
        value={shownId ?? ""}
        onChange={(e) => handleSelect(e.target.value)}
        className="min-h-11 text-base sm:text-[11px] rounded-md px-2 outline-none"
        style={{
          background: C.bgDeep,
          border: `1px solid ${C.border}`,
          color: C.textPrimary,
          maxWidth: 200,
        }}
      >
        {targets.map((tg) => (
          <option key={tg.id} value={tg.id}>
            {shortTitle(tg)}
          </option>
        ))}
      </select>

      <button
        onClick={handleToggleFollow}
        className="min-h-11 min-w-11 flex items-center justify-center gap-1.5 text-[10px] px-2 rounded-md font-medium transition-colors shrink-0"
        style={
          following
            ? { background: C.accent, color: C.onAccent, border: `1px solid ${C.accent}` }
            : { border: `1px solid ${C.border}`, color: C.textSecondary }
        }
        aria-pressed={following}
      >
        {/* A filled dot on top of the solid-fill button (not just the
            subtler tint the button used before) makes "Follow" on vs. off
            tell apart at a glance — finding: the two states were almost
            indistinguishable in the build-A screenshots. */}
        {following && <span className="w-1.5 h-1.5 rounded-full" style={{ background: C.onAccent }} />}
        {t("follow")}
      </button>

      <button
        onClick={() => setFullscreen((f) => !f)}
        title={t("fullscreen")}
        aria-label={t("fullscreen")}
        className="min-h-11 min-w-11 flex items-center justify-center rounded-md transition-colors shrink-0"
        style={{ border: `1px solid ${C.border}`, color: C.textSecondary }}
      >
        {fullscreen ? <Minimize2 size={11} /> : <Maximize2 size={11} />}
      </button>

      <div className="ml-auto flex items-center gap-2">
        <span className="flex items-center gap-1.5 text-[9px] font-mono" style={{ color: C.textMuted }}>
          <StatusDot status={hasFrame ? "online" : "idle"} size="sm" pulse={hasFrame} />
          {hasFrame ? t("live") : streamEnded ? t("streamEnded") : t("connecting")}
        </span>
        {streamEnded && (
          <button
            onClick={handleReconnect}
            title={t("reconnect")}
            aria-label={t("reconnect")}
            className="min-h-11 min-w-11 flex items-center justify-center gap-1 text-[10px] rounded-md transition-colors"
            style={{ border: `1px solid ${C.border}`, color: C.textSecondary }}
          >
            <RotateCcw size={10} />
          </button>
        )}
      </div>
    </div>
  );

  const urlBar = currentTarget?.url ? (
    <div
      className="px-3 py-1 text-[10px] font-mono truncate border-b shrink-0"
      style={{ color: C.textMuted, borderColor: C.border }}
    >
      {currentTarget.url}
    </div>
  ) : null;

  if (fullscreen) {
    return (
      <div className="fixed inset-0 z-[100] flex flex-col" style={{ background: C.bgDeep }}>
        {viewport}
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {header}
      {urlBar}
      {viewport}
    </div>
  );
}
