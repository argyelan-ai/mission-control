"use client";

import { useState, useEffect, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Terminal as XTerm } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import {
  MonitorPlay,
  Loader2,
  MonitorOff,
  RotateCcw,
  Wifi,
  WifiOff,
  Play,
  Square,
} from "lucide-react";
import { api } from "@/lib/api";
import type { Agent } from "@/lib/types";
import { C, XTERM_THEME } from "@/lib/colors";
import { TERM_MIN_CONTRAST, TERM_FONT_FAMILY, TERM_COLS, TERM_ROWS, useTerminalScale, type TermViewMode } from "@/lib/terminalScale";
import { BrowserLiveView } from "@/components/shared/BrowserLiveView";

type AgentWithState = Agent & {
  container_state?: string;     // for cli-bridge / docker runtime
  session_running?: boolean;    // for host runtime
  session_name?: string;        // for host runtime
};

function agentIsRunning(a: AgentWithState): boolean {
  if (a.agent_runtime === "host") return a.session_running === true;
  return a.container_state === "running";
}
import AppShell from "@/components/layout/AppShell";
import { notify } from "@/lib/notify";
import { StructuredSessionView } from "@/components/session/StructuredSessionView";
import { useTerminalRemountSignal } from "@/hooks/useTerminalRemountSignal";

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

function TerminalPanel({ agent }: { agent: AgentWithState }) {
  if (!agentIsRunning(agent)) {
    const stateText = agent.agent_runtime === "host"
      ? (agent.session_running ? "running" : "idle")
      : (agent.container_state ?? "unknown");
    return (
      <div className="flex flex-col items-center justify-center flex-1 bg-[#0d0d0d] gap-3 text-[11px]" style={{ color: "var(--color-text-muted)" }}>
        <MonitorOff size={32} style={{ opacity: 0.3 }} />
        <div>Session ist <span className="font-mono">{stateText}</span></div>
      </div>
    );
  }
  return <TerminalPanelRunning agent={agent} />;
}

function TerminalPanelRunning({ agent }: { agent: Agent }) {
  const termRef = useRef<HTMLDivElement>(null);
  const outerRef = useRef<HTMLDivElement>(null);
  const [term, setTerm] = useState<XTerm | null>(null);
  const [viewMode, setViewMode] = useState<"terminal" | "structured">("terminal");
  const [termView, setTermView] = useState<TermViewMode>("fit");

  useEffect(() => {
    setViewMode("terminal");
  }, [agent.id]);

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
    <div className="flex flex-col flex-1 overflow-hidden bg-[#0d0d0d]">
      {/* Header */}
      <div
        className="flex items-center gap-3 px-4 py-2.5 border-b shrink-0"
        style={{ borderColor: "rgba(255,255,255,0.06)" }}
      >
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
          {connected ? "connected" : "disconnected"}
        </span>
        <span className="text-[11px] font-mono truncate" style={{ color: "var(--color-text-secondary)" }}>
          mc-agent-{agent.name}
        </span>
        <div
          className="flex items-center rounded-md overflow-hidden ml-auto mr-2"
          style={{ border: "1px solid rgba(255,255,255,0.08)" }}
        >
          {(["fit", "native"] as const).map((m) => (
            <button
              key={m}
              onClick={() => setTermView(m)}
              className="px-2.5 py-1 text-[9px] font-medium uppercase tracking-wide transition-colors cursor-pointer"
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
        <div
          className="flex items-center rounded-md overflow-hidden"
          style={{ border: "1px solid rgba(255,255,255,0.08)" }}
        >
          {(["terminal", "structured"] as const).map((mode) => (
            <button
              key={mode}
              onClick={() => setViewMode(mode)}
              className="px-2.5 py-1 text-[9px] font-medium uppercase tracking-wide transition-colors cursor-pointer"
              style={{
                background: viewMode === mode ? C.accentSubtle : "transparent",
                color: viewMode === mode ? C.accent : C.textMuted,
                borderRight: mode === "terminal" ? "1px solid rgba(255,255,255,0.06)" : undefined,
              }}
            >
              {mode === "terminal" ? "Terminal" : "Structured"}
            </button>
          ))}
        </div>
      </div>
      {/* Body */}
      <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
        <div
          className="flex-1 min-h-0 relative"
          style={{ display: viewMode === "terminal" ? "flex" : "none" }}
        >
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
        {viewMode === "structured" && (
          <StructuredSessionView selected={{ agent_id: agent.id, agent_name: agent.name, agent_slug: agent.name, session: agent.name, task_id: agent.id, elapsed_seconds: 0, permanent: true, shell: false }} />
        )}
      </div>
    </div>
  );
}

// ── Agent List ────────────────────────────────────────────────────────────────

function AgentList({
  agents,
  selected,
  onSelect,
  isLoading,
  onStart,
  onStop,
  onRestart,
  pendingId,
}: {
  agents: AgentWithState[];
  selected: AgentWithState | null;
  onSelect: (a: AgentWithState) => void;
  isLoading: boolean;
  onStart: (id: string) => void;
  onStop: (id: string) => void;
  onRestart: (id: string) => void;
  pendingId: string | null;
}) {
  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 size={16} className="animate-spin" style={{ color: "var(--color-text-muted)" }} />
      </div>
    );
  }

  if (agents.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-3 px-6 text-center">
        <MonitorOff size={28} style={{ color: "var(--color-text-muted)", opacity: 0.3 }} />
        <p className="text-[11px]" style={{ color: "var(--color-text-muted)" }}>
          Keine Agents gefunden
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1 p-3 overflow-y-auto flex-1 min-h-0">
      {agents.map((agent) => {
        const isSelected = selected?.id === agent.id;
        const isRunning = agentIsRunning(agent);
        const isPending = pendingId === agent.id;
        const isHost = agent.agent_runtime === "host";
        const stateLabel = isHost
          ? (isRunning ? "running" : "idle")
          : (agent.container_state ?? "unknown");
        return (
          <div
            key={agent.id}
            onClick={() => onSelect(agent)}
            className="w-full flex items-center min-h-touch text-left px-3 py-2.5 rounded-lg transition-colors text-[11px] cursor-pointer group"
            style={{
              background: isSelected ? C.accentSubtle : "transparent",
              border: isSelected ? `1px solid ${C.borderAccent}` : "1px solid transparent",
            }}
          >
            <div className="flex items-center gap-2.5 w-full">
              <span
                className="inline-block w-1.5 h-1.5 rounded-full shrink-0"
                style={{
                  background: isRunning ? C.online : C.textDim,
                }}
                title={stateLabel}
              />
              <span style={{ fontSize: "14px", lineHeight: 1 }}>{agent.emoji ?? "🤖"}</span>
              <span
                className="flex-1 truncate font-medium"
                style={{ color: isSelected ? "var(--color-text-primary)" : "var(--color-text-secondary)" }}
              >
                {agent.name}
              </span>
              <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity touch-visible">
                {isRunning ? (
                  <>
                    <button
                      onClick={(e) => { e.stopPropagation(); onRestart(agent.id); }}
                      disabled={isPending}
                      title="Session neu starten"
                      className="flex items-center justify-center w-5 h-5 rounded transition-colors"
                      style={{
                        background: `${C.warning}14`,
                        border: `1px solid ${C.warning}26`,
                        color: C.warning,
                        opacity: isPending ? 0.5 : 1,
                      }}
                    >
                      <RotateCcw size={10} className={isPending ? "animate-spin" : ""} />
                    </button>
                    <button
                      onClick={(e) => { e.stopPropagation(); onStop(agent.id); }}
                      disabled={isPending}
                      title="Session stoppen"
                      className="flex items-center justify-center w-5 h-5 rounded transition-colors"
                      style={{
                        background: `${C.error}14`,
                        border: `1px solid ${C.error}26`,
                        color: C.error,
                        opacity: isPending ? 0.5 : 1,
                      }}
                    >
                      <Square size={10} />
                    </button>
                  </>
                ) : (
                  <button
                    onClick={(e) => { e.stopPropagation(); onStart(agent.id); }}
                    disabled={isPending}
                    title="Session starten"
                    className="flex items-center justify-center w-5 h-5 rounded transition-colors"
                    style={{
                      background: `${C.online}14`,
                      border: `1px solid ${C.online}26`,
                      color: C.online,
                      opacity: isPending ? 0.5 : 1,
                    }}
                  >
                    {isPending ? <Loader2 size={10} className="animate-spin" /> : <Play size={10} />}
                  </button>
                )}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function SessionsPage() {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<AgentWithState | null>(null);
  // Mobile (<md) stack navigation: which pane is visible. Desktop (≥md) ignores
  // this and always shows the split. Kept separate from `selected` so the
  // back button can return to the list without nulling `selected` (which would
  // immediately re-trigger the auto-select effect below and snap back).
  const [mobileView, setMobileView] = useState<"list" | "terminal">("list");
  // Right-pane content switch — the shared agent browser lives alongside the
  // per-agent terminal, not per-agent itself (one CDP session for the fleet).
  const [rightPane, setRightPane] = useState<"terminal" | "browser">("terminal");
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [restartTick, setRestartTick] = useState<Record<string, number>>({});

  const { data: dockerAgents = [], isLoading, isError } = useQuery({
    queryKey: ["agents", "docker-sessions"],
    queryFn: () => api.agents.listDockerSessions(),
    refetchInterval: 10_000,
  });

  const { data: hostAgents = [] } = useQuery({
    queryKey: ["agents", "host-sessions"],
    queryFn: () => api.agents.listHostSessions(),
    refetchInterval: 5_000,
  });

  const agents: AgentWithState[] = [...dockerAgents, ...hostAgents];

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["agents", "docker-sessions"] });
    qc.invalidateQueries({ queryKey: ["agents", "host-sessions"] });
  };

  // Runtime-aware: host agents use SSH→launchctl, containers use docker.
  const findAgent = (id: string) => agents.find((a) => a.id === id);
  const isHostAgent = (id: string) => findAgent(id)?.agent_runtime === "host";

  const { mutate: restartContainer } = useMutation<unknown, Error, string>({
    mutationFn: (agentId: string) =>
      isHostAgent(agentId)
        ? api.agents.restartHost(agentId)
        : api.agents.restartContainer(agentId),
    onMutate: (id) => setPendingId(id),
    onSuccess: (_data, agentId) => {
      notify.success("Session neu gestartet");
      invalidate();
      // tmux PTY is new after restart — the terminal must remount,
      // otherwise the old WebSocket is stuck on a frozen buffer.
      setRestartTick((prev) => ({ ...prev, [agentId]: (prev[agentId] ?? 0) + 1 }));
    },
    onError: (e: Error) => notify.error(`Restart fehlgeschlagen: ${e.message}`),
    onSettled: () => setPendingId(null),
  });

  const { mutate: startContainer } = useMutation<unknown, Error, string>({
    mutationFn: (agentId: string) =>
      isHostAgent(agentId)
        ? api.agents.startHost(agentId)
        : api.agents.startContainer(agentId),
    onMutate: (id) => setPendingId(id),
    onSuccess: () => { notify.success("Session gestartet"); invalidate(); },
    onError: (e: Error) => notify.error(`Start fehlgeschlagen: ${e.message}`),
    onSettled: () => setPendingId(null),
  });

  const { mutate: stopContainer } = useMutation<unknown, Error, string>({
    mutationFn: (agentId: string) =>
      isHostAgent(agentId)
        ? api.agents.stopHost(agentId)
        : api.agents.stopContainer(agentId),
    onMutate: (id) => setPendingId(id),
    onSuccess: () => { notify.success("Session gestoppt"); invalidate(); },
    onError: (e: Error) => notify.error(`Stop fehlgeschlagen: ${e.message}`),
    onSettled: () => setPendingId(null),
  });

  // Auto-select the first running agent
  useEffect(() => {
    if (agents.length > 0 && !selected) {
      const running = agents.find((a) => agentIsRunning(a));
      setSelected(running ?? agents[0]);
    }
  }, [agents, selected]);

  // Phase 15 T3.7: re-mount the terminal when the backend switches the
  // selected agent's runtime (incl. cross-image recreate). Without this
  // the WebSocket still points at the killed container's tmux PTY and
  // shows a frozen buffer.
  useTerminalRemountSignal(selected?.id ?? null, (payload) => {
    if (!selected) return;
    setRestartTick((prev) => ({ ...prev, [selected.id]: (prev[selected.id] ?? 0) + 1 }));
    invalidate();
    notify.success(
      payload.image_changed
        ? "Runtime gewechselt — Container neu gebaut"
        : "Runtime gewechselt — Container neugestartet",
    );
  });

  return (
    <AppShell fullHeight>
      <div className="flex flex-col flex-1 overflow-hidden">
        {isError && (
          <div className="text-red-400 text-xs p-4">Verbindung zum Backend fehlgeschlagen</div>
        )}
        {/* Page Header */}
        <div
          className="flex items-center gap-3 px-6 py-4 border-b shrink-0"
          style={{ borderColor: "var(--color-border-subtle)" }}
        >
          <MonitorPlay size={18} style={{ color: "var(--color-text-secondary)" }} />
          <h1 className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
            Agent Terminals
          </h1>
          <span
            className="ml-1 text-[10px] px-2 py-0.5 rounded-full font-mono"
            style={{
              background: C.accentSubtle,
              color: C.accent,
              border: `1px solid ${C.borderAccent}`,
            }}
          >
            {agents.length}
          </span>

          {/* Terminal / Agent browser tab switch */}
          <div
            className="ml-auto flex items-center rounded-md overflow-hidden"
            style={{ border: `1px solid ${C.border}` }}
          >
            {(["terminal", "browser"] as const).map((mode) => (
              <button
                key={mode}
                onClick={() => setRightPane(mode)}
                className="px-2.5 py-1.5 text-[10px] font-medium uppercase tracking-wide transition-colors cursor-pointer"
                style={{
                  background: rightPane === mode ? C.accentSubtle : "transparent",
                  color: rightPane === mode ? C.accent : C.textMuted,
                  borderRight: mode === "terminal" ? `1px solid ${C.border}` : undefined,
                }}
              >
                {mode === "terminal" ? "Terminal" : "Agent browser"}
              </button>
            ))}
          </div>
        </div>

        {/* Split Layout */}
        <div className="flex flex-col md:flex-row flex-1 overflow-hidden">
          {/* Agent List — mobile: visible only in list view; desktop: always */}
          <div
            className={`border-b md:border-b-0 md:border-r flex-col min-h-0 ${mobileView === "list" ? "flex flex-1" : "hidden"} md:flex md:flex-none md:w-[220px]`}
            style={{
              borderColor: "var(--color-border-subtle)",
              background: "rgba(255,255,255,0.01)",
            }}
          >
            <AgentList
              agents={agents}
              selected={selected}
              onSelect={(a) => { setSelected(a); setMobileView("terminal"); }}
              isLoading={isLoading}
              onStart={startContainer}
              onStop={stopContainer}
              onRestart={restartContainer}
              pendingId={pendingId}
            />
          </div>

          {/* Right pane — mobile: visible only in terminal view; desktop: always */}
          <div className={`flex-1 overflow-hidden flex-col min-h-0 ${mobileView === "terminal" ? "flex" : "hidden"} md:flex`}>
            {rightPane === "browser" ? (
              <BrowserLiveView />
            ) : selected ? (
              <>
                {/* Mobile: back button — returns to the agent list (stack nav) */}
                <button
                  onClick={() => setMobileView("list")}
                  className="flex md:hidden items-center gap-2 px-4 py-3 text-sm border-b cursor-pointer min-h-touch"
                  style={{
                    color: "var(--color-text-secondary)",
                    borderColor: "var(--color-border-subtle)",
                    background: "rgba(255,255,255,0.02)",
                  }}
                >
                  <span style={{ fontSize: "16px" }}>←</span>
                  <span>Agents</span>
                </button>
                <TerminalPanel key={`${selected.id}:${restartTick[selected.id] ?? 0}`} agent={selected} />
              </>
            ) : (
              <div className="hidden md:flex items-center justify-center flex-1 text-[11px]" style={{ color: "var(--color-text-muted)" }}>
                Select an agent
              </div>
            )}
          </div>
        </div>
      </div>
    </AppShell>
  );
}
