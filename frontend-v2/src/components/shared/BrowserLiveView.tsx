// frontend-v2/src/components/shared/BrowserLiveView.tsx
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { Filter, Maximize2, Minimize2, MonitorOff, RefreshCw, RotateCcw, Loader2, X } from "lucide-react";
import { api, browserLiveWsUrl } from "@/lib/api";
import { C, STATUS_TEXT, alpha } from "@/lib/colors";
import { StatusDot } from "@/components/shared/StatusDot";
import type { BrowserLiveTarget } from "@/lib/types";

// ── Types ──────────────────────────────────────────────────────────────────

type FrameMessage = { type: "frame"; data: string; metadata?: Record<string, unknown> };
// The server sends a machine-readable `code`, never a free-text message —
// the client is bilingual (i18n) and must never render server English
// verbatim (finding: it used to send a hardcoded English sentence that
// showed up untranslated in the German UI).
type StatusMessage = { type: "status"; code?: string; message?: string; active?: boolean };
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
  // bauplan.md PR B1: true once the server has said attribution is down for
  // THIS scoped connection (gateway unreachable / agent unresolved) — a
  // separate, persistent flag, not the transient `statusCode` banner, so
  // the toolbar toggle can keep showing it even after a `frame`/`attached`
  // message clears `statusCode`.
  scopeUnavailable: boolean;
  // True once THIS connection has actually told us its scope state (a
  // `status`/`targets` message arrived) — distinct from `scopeUnavailable`
  // itself, which starts at `false` and would otherwise be indistinguishable
  // from "the WS already confirmed attribution is fine". The component uses
  // this to stop trusting the REST call's `scopeUnavailable` the moment the
  // WS has an opinion of its own, rather than only on an explicit change
  // (medium finding, round 5).
  scopeKnown: boolean;
  // Live finding 04.10.2026: the agent owns no open tab, but tabs assigned
  // to NO agent exist — the server then streams EVERY tab and says so with
  // `unassigned_fallback`. Persistent like `scopeUnavailable`; `fallbackKnown`
  // marks that this connection has reported it at all (REST value until then).
  unassignedFallback: boolean;
  fallbackKnown: boolean;
  select: (id: string) => void;
  setFollow: (on: boolean) => void;
}

function useBrowserLiveSocket(
  enabled: boolean,
  connectKey: number,
  following: boolean,
  agentId: string | undefined,
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
  const [scopeUnavailable, setScopeUnavailable] = useState(false);
  const [scopeKnown, setScopeKnown] = useState(false);
  const [unassignedFallback, setUnassignedFallback] = useState(false);
  const [fallbackKnown, setFallbackKnown] = useState(false);

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
    setScopeUnavailable(false);
    setScopeKnown(false);
    setUnassignedFallback(false);
    setFallbackKnown(false);
    // A toggle (showAllTabs) or reconnect must not keep showing the
    // PREVIOUS connection's target list under the new scope — without this,
    // switching from "all tabs" to "only this agent" (or back) displayed the
    // old, differently-scoped list until the first push from the new
    // connection arrived (medium finding, round 5).
    setTargets(null);
    setActiveId(null);
    setFollowedId(null);
    setConnState("connecting");

    let cancelled = false;
    let ws: WebSocket | null = null;

    // Single-use stream ticket instead of the login token in the URL.
    const wantFollow = followingRef.current;
    const wantTarget = wantFollow ? undefined : (followedIdRef.current ?? undefined);
    browserLiveWsUrl(wantTarget, { follow: wantFollow, agentId }).then(
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
          if (parsed.code === "scope_unavailable") {
            // Persistent attribution flag, kept separate from the
            // transient connection-state banner below — a live frame must
            // not silently clear it (the panel could still be showing
            // every tab, not just this agent's).
            setScopeUnavailable(!!parsed.active);
            setScopeKnown(true);
          } else if (parsed.code === "unassigned_fallback") {
            setUnassignedFallback(!!parsed.active);
            setFallbackKnown(true);
          } else {
            setStatusCode(parsed.code ?? null);
          }
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

  return {
    frameSrc,
    statusCode,
    connState,
    targets,
    activeId,
    followedId,
    attachedTitle,
    scopeUnavailable,
    scopeKnown,
    unassignedFallback,
    fallbackKnown,
    select,
    setFollow,
  };
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

// Per-device, per-agent "show all tabs instead of just mine" choice
// (bauplan.md PR B1). try/catch: a private window or blocked site data must
// never break the panel — it just forgets the choice, same as any other
// localStorage convenience in this codebase (never load-bearing state).
function readShowAllTabs(agentId: string): boolean {
  try {
    return localStorage.getItem(`mc.browserLive.showAllTabs.${agentId}`) === "1";
  } catch {
    return false;
  }
}

function writeShowAllTabs(agentId: string, value: boolean): void {
  try {
    if (value) localStorage.setItem(`mc.browserLive.showAllTabs.${agentId}`, "1");
    else localStorage.removeItem(`mc.browserLive.showAllTabs.${agentId}`);
  } catch {
    // per-viewer convenience only — nothing to recover
  }
}

// ── Main component ───────────────────────────────────────────────────────────

interface BrowserLiveViewProps {
  /** bauplan.md PR B1: when given, the panel scopes to this agent's own
   *  tabs (via cdp-gateway) unless the operator picked "show all tabs" for
   *  this agent on this device. Omitted entirely (e.g. no agent context) →
   *  always every tab, exactly like before B1. */
  agentId?: string;
  agentName?: string;
}

export function BrowserLiveView({ agentId, agentName }: BrowserLiveViewProps = {}) {
  const t = useTranslations("browserLive");
  const [connectKey, setConnectKey] = useState(0);
  const [connect, setConnect] = useState(true); // PR A1: connects on open, no click needed
  const [following, setFollowingState] = useState(true);
  const [fullscreen, setFullscreen] = useState(false);
  const [justSwitchedTitle, setJustSwitchedTitle] = useState<string | null>(null);
  const switchHintTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [showAllTabs, setShowAllTabsState] = useState(() => (agentId ? readShowAllTabs(agentId) : true));
  // Skips the very first run of the effect below — on mount there is no
  // "previous" connection to re-scope away from, so bumping `connectKey`
  // there just opens a second WS connection (and a second stream ticket)
  // immediately after the first, for no reason (finding: every panel open
  // made two connects).
  const didMountAgentEffect = useRef(false);

  // A different agentId (switched chats) re-reads this device's choice for
  // THAT agent instead of carrying over whatever the previous agent's panel
  // was showing.
  useEffect(() => {
    setShowAllTabsState(agentId ? readShowAllTabs(agentId) : true);
    if (didMountAgentEffect.current) {
      setConnectKey((k) => k + 1); // switched chats: re-scope the live WS to the new agent
    }
    didMountAgentEffect.current = true;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentionally only on agentId, see useBrowserLiveSocket's connect effect for the same pattern
  }, [agentId]);

  const setShowAllTabs = useCallback(
    (value: boolean) => {
      setShowAllTabsState(value);
      if (agentId) writeShowAllTabs(agentId, value);
      setConnectKey((k) => k + 1); // re-scope the live WS immediately
    },
    [agentId],
  );

  const effectiveAgentId = agentId && !showAllTabs ? agentId : undefined;

  // First paint + fallback while the WS is down — not a 15s poll that fights
  // the live `targets` push once connected (that was the old bug: the panel
  // only ever refreshed on a manual tap).
  const {
    data: initialData,
    isLoading,
    isError,
    error,
    refetch,
    isFetching,
  } = useQuery({
    queryKey: ["browser-live", "targets", effectiveAgentId ?? "all"],
    queryFn: () => api.browserLive.targets(effectiveAgentId),
    refetchInterval: connect ? false : 5_000,
  });
  const initialTargets = initialData?.targets ?? [];

  const {
    frameSrc,
    statusCode,
    connState,
    targets: wsTargets,
    activeId,
    followedId,
    attachedTitle,
    scopeUnavailable: wsScopeUnavailable,
    scopeKnown: wsScopeKnown,
    unassignedFallback: wsUnassignedFallback,
    fallbackKnown: wsFallbackKnown,
    select,
    setFollow,
  } = useBrowserLiveSocket(connect, connectKey, following, effectiveAgentId);

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

  // bauplan.md PR B1: true when the panel is SCOPED (not "show all tabs")
  // but attribution isn't actually working right now. Once the live WS has
  // ANY opinion of its own (`wsScopeKnown`), it is authoritative — a REST
  // `/targets` call that happened to land during a brief gateway outage
  // must never keep the amber "attribution unavailable" hint up for the
  // rest of the session after the WS reconnects and finds the gateway back
  // (medium finding, round 5: the two signals used to be OR'd together
  // forever, so only `true` could ever "win"). Before the WS has said
  // anything yet, the REST value is still the best first-paint guess.
  const scopeUnavailable = !!effectiveAgentId && (wsScopeKnown ? wsScopeUnavailable : !!initialData?.scopeUnavailable);

  // Scoped, the agent owns no open tab, but some tab is assigned to no agent
  // (live finding 04.10.2026 — the panel used to say "<agent> has no open
  // tab" while the agent was working in such a tab). The list then holds
  // EVERY tab; the hint says why. Same "WS is authoritative once it spoke"
  // rule as `scopeUnavailable`, which outranks it (nothing is attributed then).
  const unassignedFallback =
    !!effectiveAgentId &&
    !scopeUnavailable &&
    (wsFallbackKnown ? wsUnassignedFallback : !!initialData?.unassignedFallback);
  const displayName = agentName ?? t("thisAgent");
  const fallbackHint = t("unassignedFallbackHint", { name: displayName });

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
  // bauplan.md PR B1: scoped to an agent, genuinely has no tabs, nothing is
  // actually broken — a different empty state than "browser unreachable",
  // with its own way out (look at every tab instead of waiting).
  const scopedEmpty = !!effectiveAgentId && !isError && (wsConnectedWithNoTabs || (!connect && targets.length === 0));

  if ((isError || targets.length === 0) && !hasFrame) {
    return (
      <div className="flex flex-col items-center justify-center h-full min-h-[200px] gap-3 px-6 text-center">
        <MonitorOff size={28} style={{ color: C.textMuted, opacity: 0.3 }} />
        <p className="text-[11px] max-w-xs" style={{ color: C.textMuted }}>
          {isError
            ? `${t("notRunning")} (${(error as Error)?.message ?? "unreachable"})`
            : scopedEmpty
              ? t("noTabsForAgent", { name: agentName ?? t("thisAgent") })
              : wsConnectedWithNoTabs
                ? t("status.no_page")
                : t("notRunning")}
        </p>
        {scopedEmpty ? (
          <button
            onClick={() => setShowAllTabs(true)}
            className="min-h-11 flex items-center gap-1.5 text-[10px] px-3 rounded-md transition-colors"
            style={{ background: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}
          >
            {t("showAllTabs")}
          </button>
        ) : (
          <button
            onClick={() => refetch()}
            disabled={isFetching}
            className="min-h-11 flex items-center gap-1.5 text-[10px] px-3 rounded-md transition-colors disabled:opacity-40"
            style={{
              background: "transparent",
              border: `1px solid ${C.border}`,
              color: C.textSecondary,
            }}
          >
            <RefreshCw size={11} className={isFetching ? "animate-spin" : ""} />
            {t("refresh")}
          </button>
        )}
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

      {/* One bottom banner slot, not three: the transient connection-status
          message, the persistent "attribution unavailable" hint and the
          "showing all tabs, some aren't assigned" hint
          (bauplan.md PR B1 / review finding — the panel used to fall back to
          showing EVERY tab here with NO signal at all, while the toolbar
          toggle still claimed to be scoped) share it rather than stacking
          two near-identical `absolute bottom-2` bars. Scope-unavailable
          wins when both are true — it says more (and stays up alongside a
          live frame, unlike the transient one). */}
      {connect && !streamEnded && (scopeUnavailable || unassignedFallback || statusMessage) && (
        <div
          className="absolute bottom-2 left-2 right-2 text-[10px] px-2.5 py-1.5 rounded-md"
          style={
            scopeUnavailable
              ? { background: alpha(C.warning, 0.15), color: C.warning, border: `1px solid ${C.warning}` }
              : unassignedFallback
                ? { background: C.bgElevated, color: STATUS_TEXT.info, border: `1px solid ${C.info}` }
                : { background: alpha(C.scrim, 0.6), color: C.textSecondary, border: `1px solid ${C.border}` }
          }
        >
          {scopeUnavailable ? t("scopeUnavailableHint") : unassignedFallback ? fallbackHint : statusMessage}
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
      {/* flex-1 + min-w-0: the picker takes whatever width is left and
          truncates, so Follow / filter / fullscreen stay on ONE line at
          393 px (finding: the fullscreen button used to wrap below). pr-6
          keeps the truncated title clear of the native dropdown arrow. */}
      <select
        id="browser-live-target"
        value={shownId ?? ""}
        onChange={(e) => handleSelect(e.target.value)}
        className="min-h-11 min-w-0 flex-1 text-base sm:text-[11px] rounded-md pl-2 pr-6 outline-none"
        style={{
          background: C.bgDeep,
          border: `1px solid ${C.border}`,
          color: C.textPrimary,
          maxWidth: 200,
        }}
      >
        {targets.map((tg) => (
          <option key={tg.id} value={tg.id}>
            {tg.unassigned ? `${shortTitle(tg)} · ${t("unassignedTag")}` : shortTitle(tg)}
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

      {/* bauplan.md PR B1: only an agent-scoped panel gets this toggle — a
          panel opened with no agentId (e.g. a future "all agents" view)
          always shows everything and has nothing to switch between.
          A filter icon + "Only <name>" / "All tabs" reads as a FILTER at a
          glance (finding: the bare agent name alone didn't); `aria-pressed`
          always means the SAME thing — "the only-this-agent filter is on" —
          instead of flipping its meaning together with the visible label. */}
      {agentId && (
        <button
          onClick={() => setShowAllTabs(!showAllTabs)}
          className="min-h-11 flex items-center gap-1 text-[10px] px-2 rounded-md transition-colors shrink-0"
          style={
            scopeUnavailable
              ? { border: `1px solid ${C.warning}`, color: C.warning }
              : { border: `1px solid ${C.border}`, color: C.textSecondary }
          }
          aria-pressed={!showAllTabs}
          // One accessible name for both visible label sizes below; it
          // contains either visible text ("label in name").
          aria-label={showAllTabs ? t("showingAllTabs") : t("showingOnlyThisAgent", { name: displayName })}
          title={
            scopeUnavailable
              ? t("scopeUnavailableHint")
              : unassignedFallback
                ? fallbackHint
                : showAllTabs
                  ? t("showingAllTabs")
                  : t("showingOnlyThisAgent", { name: displayName })
          }
        >
          <Filter size={11} />
          {/* Phone: just the name / "All" (393 px finding — "Only <name>" pushed
              the fullscreen button onto a second line). From `sm` up the
              full "Only <name>" / "All tabs". */}
          <span data-chip-label="compact" className="sm:hidden truncate max-w-20">
            {showAllTabs ? t("allTabsShort") : displayName}
          </span>
          <span data-chip-label="full" className="hidden sm:inline">
            {showAllTabs ? t("allTabs") : t("onlyAgent", { name: displayName })}
          </span>
        </button>
      )}

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
