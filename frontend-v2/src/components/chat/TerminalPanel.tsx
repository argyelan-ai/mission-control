"use client";

/**
 * TerminalPanel — Task B6. Moved verbatim from the pre-chat `sessions/page.tsx`
 * (useAgentTerminal, terminalScale pinning, touch-scroll synthetic mouse
 * events, useTerminalRemountSignal usage in the parent page). Not rewritten —
 * the mechanics here are load-bearing and already battle-tested.
 */
import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Terminal as XTerm } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { MonitorOff, Wifi, WifiOff } from "lucide-react";
import { api } from "@/lib/api";
import type { Agent } from "@/lib/types";
import { C, XTERM_THEME } from "@/lib/colors";
import { TERM_MIN_CONTRAST, TERM_FONT_FAMILY, TERM_COLS, TERM_ROWS, useTerminalScale, type TermViewMode } from "@/lib/terminalScale";

// Docker/host session-list responses include fields the shared `Agent` type
// doesn't declare (container_state, session_running/name, and the DB-only
// `slug` column — see backend/app/routers/cli_terminal.py's `model_dump()`).
// Same local-extension pattern as the old page.tsx used.
export type AgentWithState = Agent & {
  container_state?: string;     // for cli-bridge / docker runtime
  session_running?: boolean;    // for host runtime
  session_name?: string;        // for host runtime
  slug?: string | null;
};

export function agentIsRunning(a: AgentWithState): boolean {
  if (a.agent_runtime === "host") return a.session_running === true;
  return a.container_state === "running";
}

// ── WebSocket Terminal Hook ───────────────────────────────────────────────────

function useAgentTerminal(
  agent: Agent | null,
  term: XTerm | null,
): boolean {
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const destroyedRef = useRef(false);
  const dataDisposableRef = useRef<{ dispose: () => void } | null>(null);
  const resizeDisposableRef = useRef<{ dispose: () => void } | null>(null);
  // wsRef used below for touch scroll (synthetic mouse wheel CSI sequences).

  useEffect(() => {
    destroyedRef.current = false;

    function connect() {
      if (destroyedRef.current || !agent || !term) return;

      if (wsRef.current) {
        wsRef.current.close(1000);
        wsRef.current = null;
      }

      const url = agent.agent_runtime === "host"
        ? api.cliSessions.hostPtyWsUrl(agent.id)
        : api.cliSessions.ptyWsUrl(agent.id);
      const ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      ws.onopen = () => {
        if (destroyedRef.current) { ws.close(1000); return; }
        setConnected(true);
        if (term.cols && term.rows) {
          ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
        }
      };

      ws.onmessage = (evt) => {
        if (evt.data instanceof ArrayBuffer) {
          term.write(new Uint8Array(evt.data));
        } else {
          term.write(evt.data as string);
        }
      };

      ws.onerror = () => {
        setConnected(false);
      };

      ws.onclose = (evt) => {
        setConnected(false);
        if (!destroyedRef.current && evt.code !== 1000) {
          // Auto-reconnect after 3s. Status lives in the header badge —
          // writing "[Reconnecting...]" into the scrollback spammed the
          // terminal content on every retry.
          reconnectTimer.current = setTimeout(connect, 3000);
        }
      };

      // Copy on selection
      term.onSelectionChange(() => {
        if (term.hasSelection()) {
          navigator.clipboard.writeText(term.getSelection()).catch(() => {});
        }
      });

      // Paste via Cmd+V / Ctrl+V
      term.attachCustomKeyEventHandler((e: KeyboardEvent) => {
        if ((e.metaKey || e.ctrlKey) && e.key === "v" && e.type === "keydown") {
          navigator.clipboard.readText().then((text) => {
            if (ws.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ type: "input", data: text }));
            }
          }).catch(() => {});
          return false;
        }
        return true;
      });

      // Dispose old listeners before adding new ones
      dataDisposableRef.current?.dispose();
      resizeDisposableRef.current?.dispose();

      dataDisposableRef.current = term.onData((data) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(data);
      });

      resizeDisposableRef.current = term.onResize(({ cols, rows }) => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "resize", cols, rows }));
        }
      });
    }

    connect();

    return () => {
      destroyedRef.current = true;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      wsRef.current?.close(1000);
      wsRef.current = null;
      dataDisposableRef.current?.dispose();
      resizeDisposableRef.current?.dispose();
      setConnected(false);
    };
  }, [agent?.id, term]);

  // Scroll: tmux mouse on + xterm.js native mouse tracking.
  //
  // Why: tmux uses the alternate screen buffer, so xterm.js's own scrollback
  // is useless. The 50000-line history lives in tmux. With "mouse on" in
  // tmux's config, tmux handles wheel events: xterm.js (in mouse-tracking mode
  // because tmux enables it) fires onData with mouse button 4/5 CSI sequences
  // on wheel, which our onData handler forwards via ws → backend → PTY → tmux.
  //
  // No attachCustomWheelEventHandler here: returning false from it would
  // suppress xterm.js's mouse-tracking path (the working path). We let
  // xterm.js handle wheel natively.
  //
  // Mobile (touch): no wheel events → synthesize the same mouse button 4/5
  // SGR sequences manually and send them directly to the PTY. tmux intercepts
  // them the same way it intercepts real wheel events.
  //   \x1b[<64;1;1M = mouse button 64 (wheel up / scroll to older history)
  //   \x1b[<65;1;1M = mouse button 65 (wheel down / scroll to live end)
  useEffect(() => {
    if (!term) return;

    const el = term.element;
    const TOUCH_LINE_PX = 18; // ~1 terminal line per scroll unit
    let lastY: number | null = null;
    let accum = 0;

    const onTouchStart = (e: TouchEvent) => {
      if (e.touches.length !== 1) { lastY = null; return; }
      lastY = e.touches[0].clientY;
      accum = 0;
    };
    const onTouchMove = (e: TouchEvent) => {
      if (lastY === null || e.touches.length !== 1) return;
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
      const y = e.touches[0].clientY;
      accum += y - lastY;
      lastY = y;
      const lines = Math.trunc(accum / TOUCH_LINE_PX);
      if (lines !== 0) {
        accum -= lines * TOUCH_LINE_PX;
        // lines > 0 = finger moving down = scroll up into older history
        const btn = lines > 0 ? "\x1b[<64;1;1M" : "\x1b[<65;1;1M";
        for (let i = 0; i < Math.abs(lines); i++) {
          wsRef.current.send(btn);
        }
      }
      e.preventDefault();
    };
    const onTouchEnd = () => { lastY = null; };

    el?.addEventListener("touchstart", onTouchStart, { passive: true, capture: true });
    el?.addEventListener("touchmove", onTouchMove, { passive: false, capture: true });
    el?.addEventListener("touchend", onTouchEnd, { passive: true, capture: true });
    el?.addEventListener("touchcancel", onTouchEnd, { passive: true, capture: true });

    return () => {
      el?.removeEventListener("touchstart", onTouchStart, { capture: true });
      el?.removeEventListener("touchmove", onTouchMove, { capture: true });
      el?.removeEventListener("touchend", onTouchEnd, { capture: true });
      el?.removeEventListener("touchcancel", onTouchEnd, { capture: true });
    };
  }, [term]);

  return connected;
}

// ── Terminal Panel ────────────────────────────────────────────────────────────

export function TerminalPanel({ agent }: { agent: AgentWithState }) {
  const t = useTranslations("sessions");
  if (!agentIsRunning(agent)) {
    const stateText = agent.agent_runtime === "host"
      ? (agent.session_running ? "running" : "idle")
      : (agent.container_state ?? "unknown");
    return (
      <div className="flex flex-col items-center justify-center flex-1 bg-[var(--color-bg-deep)] gap-3 text-xs" style={{ color: "var(--color-text-muted)" }}>
        <MonitorOff size={32} style={{ opacity: 0.3 }} />
        <div>{t("sessionIs")} <span className="font-mono">{stateText}</span></div>
      </div>
    );
  }
  return <TerminalPanelRunning agent={agent} />;
}

function TerminalPanelRunning({ agent }: { agent: Agent }) {
  const t = useTranslations("sessions");
  const termRef = useRef<HTMLDivElement>(null);
  const outerRef = useRef<HTMLDivElement>(null);
  const [term, setTerm] = useState<XTerm | null>(null);
  const [termView, setTermView] = useState<TermViewMode>("fit");

  useEffect(() => {
    if (!termRef.current) return;
    const t = new XTerm({
      theme: XTERM_THEME,
      minimumContrastRatio: TERM_MIN_CONTRAST,
      scrollback: 5000,
      cursorBlink: true,
      convertEol: true,
      fontFamily: TERM_FONT_FAMILY,
      fontSize: 14,
      lineHeight: 1.4,
    });
    t.open(termRef.current);
    // Canonical size for every viewer — the shared tmux window must not be
    // reshaped per browser/phone (see lib/terminalScale.ts).
    t.resize(TERM_COLS, TERM_ROWS);
    requestAnimationFrame(() => requestAnimationFrame(() => t.focus()));

    const parent = termRef.current.parentElement!;
    const onContainerClick = () => t.focus();
    parent.addEventListener("click", onContainerClick);

    setTerm(t);
    return () => { t.dispose(); parent.removeEventListener("click", onContainerClick); };
  }, []);

  const connected = useAgentTerminal(agent, term);
  const { scale, size } = useTerminalScale(outerRef, term, termView);

  return (
    <div className="flex flex-col flex-1 overflow-hidden bg-[var(--color-bg-deep)]">
      {/* Header */}
      {/* flex-wrap: on phones the two toggles drop to their own row instead of
          getting crushed next to the status badge + agent name */}
      <div
        className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2.5 border-b shrink-0"
        style={{ borderColor: "rgba(255,255,255,0.06)" }}
      >
        <div className="flex items-center gap-3 min-w-0 flex-1">
          {connected ? (
            <Wifi size={12} style={{ color: C.online, flexShrink: 0 }} />
          ) : (
            <WifiOff size={12} style={{ color: C.error, flexShrink: 0 }} />
          )}
          <span
            className="text-[9px] px-1.5 py-0.5 rounded font-mono shrink-0"
            style={{
              background: connected ? `${C.online}1A` : `${C.error}1A`,
              color: connected ? C.online : C.error,
              border: `1px solid ${connected ? `${C.online}33` : `${C.error}33`}`,
            }}
          >
            {connected ? t("connected") : t("disconnected")}
          </span>
          <span className="text-xs font-mono truncate min-w-0" style={{ color: "var(--color-text-secondary)" }}>
            mc-agent-{agent.name}
          </span>
        </div>
        {/* w-full forces the toggles onto their own row on phones — the status
            group has flex-basis 0 (flex-1), so flex-wrap alone never fires */}
        <div className="flex items-center gap-2 shrink-0 w-full md:w-auto md:ml-auto">
        <div
          className="flex items-center rounded-md overflow-hidden shrink-0"
          style={{ border: "1px solid rgba(255,255,255,0.08)" }}
        >
          {(["fit", "native"] as const).map((m) => (
            <button
              key={m}
              onClick={() => setTermView(m)}
              className="px-2.5 py-1.5 md:py-1 text-[9px] font-medium uppercase tracking-wide transition-colors cursor-pointer whitespace-nowrap"
              style={{
                background: termView === m ? C.accentSubtle : "transparent",
                color: termView === m ? C.accent : C.textMuted,
                borderRight: m === "fit" ? "1px solid rgba(255,255,255,0.06)" : undefined,
              }}
            >
              {m === "fit" ? "Fit" : "1:1"}
            </button>
          ))}
        </div>
        </div>
      </div>
      {/* Body */}
      <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
        <div className="flex-1 min-h-0 relative flex">
          <div ref={outerRef} className="absolute inset-0 overflow-auto">
            <div
              style={{
                width: size ? size.w * scale : undefined,
                height: size ? size.h * scale : undefined,
              }}
            >
              <div
                ref={termRef}
                className="p-1"
                style={{ transform: `scale(${scale})`, transformOrigin: "top left" }}
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
