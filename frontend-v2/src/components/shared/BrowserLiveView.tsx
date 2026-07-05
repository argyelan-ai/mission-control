// frontend-v2/src/components/shared/BrowserLiveView.tsx
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { MonitorOff, RefreshCw, RotateCcw, Loader2 } from "lucide-react";
import { api, browserLiveWsUrl } from "@/lib/api";
import { C } from "@/lib/colors";
import { StatusDot } from "@/components/shared/StatusDot";

// ── Types ──────────────────────────────────────────────────────────────────

type FrameMessage = { type: "frame"; data: string; metadata?: Record<string, unknown> };
type StatusMessage = { type: "status"; message: string };
type ServerMessage = FrameMessage | StatusMessage;

function isServerMessage(x: unknown): x is ServerMessage {
  return !!x && typeof x === "object" && "type" in x;
}

// ── WebSocket hook ─────────────────────────────────────────────────────────
//
// View-only: we never send anything on this socket. `connectKey` bumps to
// force a fresh connection (target switch, manual Reconnect).

function useBrowserLiveSocket(enabled: boolean, targetId: string | null, connectKey: number) {
  const wsRef = useRef<WebSocket | null>(null);
  const [frameSrc, setFrameSrc] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [connState, setConnState] = useState<"connecting" | "open" | "closed">("connecting");

  useEffect(() => {
    if (!enabled) return;
    setFrameSrc(null);
    setStatusMessage(null);
    setConnState("connecting");

    const url = browserLiveWsUrl(targetId ?? undefined);
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
      } else if (parsed.type === "status") {
        setStatusMessage(parsed.message);
      }
    };

    ws.onerror = () => {
      setStatusMessage((prev) => prev ?? "Connection error");
    };

    ws.onclose = () => {
      setConnState("closed");
    };

    return () => {
      ws.close(1000);
      wsRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, targetId, connectKey]);

  return { frameSrc, statusMessage, connState };
}

// ── Main component ───────────────────────────────────────────────────────────

export function BrowserLiveView() {
  const [selectedTarget, setSelectedTarget] = useState<string | null>(null);
  const [connectKey, setConnectKey] = useState(0);
  const [connect, setConnect] = useState(false);

  const {
    data: targets = [],
    isLoading,
    isError,
    error,
    refetch,
    isFetching,
  } = useQuery({
    queryKey: ["browser-live", "targets"],
    queryFn: () => api.browserLive.targets(),
    refetchInterval: connect ? false : 15_000,
  });

  // Default to the first (newest) target once the list arrives.
  useEffect(() => {
    if (targets.length > 0 && (!selectedTarget || !targets.find((t) => t.id === selectedTarget))) {
      setSelectedTarget(targets[0].id);
    } else if (targets.length === 0) {
      setSelectedTarget(null);
    }
  }, [targets, selectedTarget]);

  const { frameSrc, statusMessage, connState } = useBrowserLiveSocket(
    connect,
    selectedTarget,
    connectKey,
  );

  const handleReconnect = useCallback(() => {
    setConnectKey((k) => k + 1);
    setConnect(true);
  }, []);

  const hasFrame = connect && frameSrc !== null;
  const streamEnded = connect && connState === "closed";

  // ── Empty states ───────────────────────────────────────────────────────

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full min-h-[200px]">
        <Loader2 size={16} className="animate-spin" style={{ color: C.textMuted }} />
      </div>
    );
  }

  if (isError || targets.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-full min-h-[200px] gap-3 px-6 text-center">
        <MonitorOff size={28} style={{ color: C.textMuted, opacity: 0.3 }} />
        <p className="text-[11px] max-w-xs" style={{ color: C.textMuted }}>
          {isError
            ? `Agent browser not running — start a task with the E2E toggle or enable the browser profile. (${(error as Error)?.message ?? "unreachable"})`
            : "Agent browser not running — start a task with the E2E toggle or enable the browser profile."}
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
          Refresh
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header: target picker + controls */}
      <div
        className="flex items-center gap-2 px-3 py-2 border-b shrink-0 flex-wrap"
        style={{ borderColor: C.border }}
      >
        <label htmlFor="browser-live-target" className="sr-only">
          Browser page
        </label>
        <select
          id="browser-live-target"
          value={selectedTarget ?? ""}
          onChange={(e) => setSelectedTarget(e.target.value)}
          className="text-[11px] rounded-md px-2 py-1 outline-none"
          style={{
            background: C.bgDeep,
            border: `1px solid ${C.border}`,
            color: C.textPrimary,
            maxWidth: 260,
          }}
        >
          {targets.map((t) => (
            <option key={t.id} value={t.id}>
              {t.title || t.url || t.id}
            </option>
          ))}
        </select>

        <button
          onClick={() => refetch()}
          disabled={isFetching}
          title="Refresh target list"
          className="flex items-center justify-center w-6 h-6 rounded-md transition-colors disabled:opacity-40"
          style={{ border: `1px solid ${C.border}`, color: C.textSecondary }}
        >
          <RefreshCw size={11} className={isFetching ? "animate-spin" : ""} />
        </button>

        <div className="ml-auto flex items-center gap-2">
          {connect && (
            <span className="flex items-center gap-1.5 text-[9px] font-mono" style={{ color: C.textMuted }}>
              <StatusDot status={hasFrame ? "online" : "idle"} size="sm" pulse={hasFrame} />
              {hasFrame ? "Live" : streamEnded ? "Stream ended" : "Connecting…"}
            </span>
          )}

          {!connect ? (
            <button
              onClick={handleReconnect}
              className="text-[10px] px-2.5 py-1.5 rounded-md font-medium transition-colors"
              style={{ background: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}
            >
              Connect
            </button>
          ) : (
            <>
              <button
                onClick={handleReconnect}
                title="Reconnect"
                className="flex items-center gap-1 text-[10px] px-2 py-1.5 rounded-md transition-colors"
                style={{ border: `1px solid ${C.border}`, color: C.textSecondary }}
              >
                <RotateCcw size={10} />
                Reconnect
              </button>
              <button
                onClick={() => setConnect(false)}
                className="text-[10px] px-2.5 py-1.5 rounded-md transition-colors"
                style={{ border: `1px solid ${C.border}`, color: C.textMuted }}
              >
                Disconnect
              </button>
            </>
          )}
        </div>
      </div>

      {/* Viewport */}
      <div
        className="relative flex-1 min-h-0 flex items-center justify-center overflow-hidden"
        style={{ background: "#0d0d0d" }}
      >
        {!connect && (
          <p className="text-[11px] px-6 text-center" style={{ color: C.textMuted }}>
            Click Connect to view the shared agent browser live.
          </p>
        )}

        {connect && frameSrc && (
          // eslint-disable-next-line @next/next/no-img-element -- data: URL, not a Next-optimizable asset
          <img
            src={frameSrc}
            alt="Live agent browser view"
            className="max-w-full max-h-full object-contain"
          />
        )}

        {connect && !frameSrc && !streamEnded && (
          <div className="flex flex-col items-center gap-2">
            <Loader2 size={18} className="animate-spin" style={{ color: C.textMuted }} />
            <p className="text-[11px]" style={{ color: C.textMuted }}>
              Connecting…
            </p>
          </div>
        )}

        {connect && streamEnded && (
          <div className="flex flex-col items-center gap-2 px-6 text-center">
            <MonitorOff size={24} style={{ color: C.textMuted, opacity: 0.4 }} />
            <p className="text-[11px]" style={{ color: C.textMuted }}>
              Stream ended{statusMessage ? ` — ${statusMessage}` : ""}
            </p>
            <button
              onClick={handleReconnect}
              className="flex items-center gap-1.5 text-[10px] px-2.5 py-1.5 rounded-md transition-colors"
              style={{ background: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}
            >
              <RotateCcw size={11} />
              Reconnect
            </button>
          </div>
        )}

        {connect && statusMessage && !streamEnded && (
          <div
            className="absolute bottom-2 left-2 right-2 text-[10px] px-2.5 py-1.5 rounded-md"
            style={{ background: "rgba(0,0,0,0.6)", color: C.textSecondary, border: `1px solid ${C.border}` }}
          >
            {statusMessage}
          </div>
        )}
      </div>
    </div>
  );
}
