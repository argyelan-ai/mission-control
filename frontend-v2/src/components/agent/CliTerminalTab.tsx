// frontend-v2/src/components/agent/CliTerminalTab.tsx
"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { Loader2, Square, Send, MonitorOff, RotateCcw } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C, XTERM_THEME } from "@/lib/colors";
import { TERM_MIN_CONTRAST, TERM_FONT_FAMILY, TERM_COLS, TERM_ROWS, useTerminalScale } from "@/lib/terminalScale";

interface CliSession {
  task_id: string;
  session: string;
  elapsed_seconds: number;
}

interface Props {
  agentId: string;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h`;
}

// ── Terminal Hook ─────────────────────────────────────────────────────────────

function useTerminalWebSocket(
  agentId: string,
  taskId: string | null,
  term: Terminal | null
) {
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!taskId || !term) return;

    term.clear();
    const url = api.agents.cli.wsUrl(agentId, taskId);
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onmessage = (evt) => {
      term.write(evt.data);
    };

    ws.onerror = () => {
      term.writeln("\r\n\x1b[31m[Connection error]\x1b[0m");
    };

    ws.onclose = (evt) => {
      if (evt.code !== 1000) {
        term.writeln("\r\n\x1b[33m[Session disconnected]\x1b[0m");
      }
    };

    return () => {
      ws.close(1000);
      wsRef.current = null;
    };
  }, [agentId, taskId, term]);
}

// ── Session List ──────────────────────────────────────────────────────────────

function SessionList({
  agentId,
  selectedId,
  onSelect,
}: {
  agentId: string;
  selectedId: string | null;
  onSelect: (taskId: string) => void;
}) {
  const qc = useQueryClient();
  const { data: sessions = [], isLoading } = useQuery({
    queryKey: ["cli-sessions", agentId],
    queryFn: () => api.agents.cli.sessions(agentId),
    refetchInterval: 3_000,
  });

  const killMutation = useMutation({
    mutationFn: (taskId: string) => api.agents.cli.kill(agentId, taskId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["cli-sessions", agentId] }),
  });

  const restartWorkerMutation = useMutation({
    mutationFn: () => api.agents.restartWorker(agentId),
    onSuccess: () => {
      notify.success("Worker restarted");
      qc.invalidateQueries({ queryKey: ["cli-sessions", agentId] });
    },
    onError: (e: Error) => notify.error(`Restart failed: ${e.message}`),
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 size={16} className="animate-spin text-[var(--color-text-muted)]" />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1 p-2">
      <div className="flex items-center justify-between px-2 mb-1">
        <span className="text-[9px] uppercase tracking-wider text-[var(--color-text-muted)] font-semibold">
          Sessions
        </span>
        <button
          onClick={() => restartWorkerMutation.mutate()}
          disabled={restartWorkerMutation.isPending}
          title="Restart worker"
          className="flex items-center gap-1 text-[9px] text-[var(--color-text-muted)] hover:text-[#B8870A] transition-colors disabled:opacity-40"
        >
          <RotateCcw size={10} className={restartWorkerMutation.isPending ? "animate-spin" : ""} />
          <span>Restart</span>
        </button>
      </div>
      {sessions.length === 0 && (
        <div className="flex flex-col items-center justify-center py-8 gap-2 px-4 text-center">
          <MonitorOff size={20} className="text-[var(--color-text-muted)] opacity-30" />
          <p className="text-[10px] text-[var(--color-text-muted)]">No active sessions</p>
        </div>
      )}
      {sessions.map((s) => {
        const isSelected = s.task_id === selectedId;
        return (
          <div key={s.task_id} className="group relative">
            <button
              onClick={() => onSelect(s.task_id)}
              className={[
                "w-full text-left px-3 py-2 rounded-lg transition-colors text-[11px]",
                isSelected
                  ? "bg-[rgba(15,163,163,0.12)] border border-[rgba(15,163,163,0.30)]"
                  : "hover:bg-[rgba(255,255,255,0.03)] border border-transparent",
              ].join(" ")}
            >
              <div className="flex items-center gap-2">
                <span
                  className="w-1.5 h-1.5 rounded-full shrink-0"
                  style={{
                    background: C.online,
                  }}
                />
                <span
                  className={[
                    "flex-1 truncate",
                    isSelected
                      ? "text-[var(--color-text-primary)]"
                      : "text-[var(--color-text-secondary)]",
                  ].join(" ")}
                >
                  {s.session}
                </span>
                <span className="text-[9px] text-[var(--color-text-muted)] shrink-0">
                  {formatElapsed(s.elapsed_seconds)}
                </span>
              </div>
            </button>

            {/* Kill button on hover */}
            <button
              onClick={(e) => {
                e.stopPropagation();
                if (confirm(`End session ${s.session}?`)) {
                  killMutation.mutate(s.task_id);
                }
              }}
              className="absolute right-1 top-1/2 -translate-y-1/2 opacity-0 group-hover:opacity-100 transition-opacity p-1 rounded text-[var(--color-text-muted)] hover:text-red-400 touch-visible"
              title="End session"
            >
              <Square size={10} />
            </button>
          </div>
        );
      })}
    </div>
  );
}

// ── Terminal Panel ────────────────────────────────────────────────────────────

function TerminalPanel({
  agentId,
  taskId,
}: {
  agentId: string;
  taskId: string;
}) {
  const termRef = useRef<HTMLDivElement>(null);
  const outerRef = useRef<HTMLDivElement>(null);
  const [term, setTerm] = useState<Terminal | null>(null);
  const [input, setInput] = useState("");

  const sendMutation = useMutation({
    mutationFn: (text: string) => api.agents.cli.input(agentId, taskId, text),
  });

  // Initialize xterm.js
  useEffect(() => {
    if (!termRef.current) return;

    const t = new Terminal({
      theme: XTERM_THEME,
      minimumContrastRatio: TERM_MIN_CONTRAST,
      scrollback: 5000,
      convertEol: true,
      fontFamily: TERM_FONT_FAMILY,
      fontSize: 12,
      lineHeight: 1.4,
      disableStdin: false,
    });

    t.open(termRef.current);
    // Canonical size — same for every viewer, so the shared tmux window is
    // never reshaped by a browser/phone (see lib/terminalScale.ts).
    t.resize(TERM_COLS, TERM_ROWS);

    setTerm(t);
    return () => {
      t.dispose();
    };
  }, []);

  // Connect WebSocket
  useTerminalWebSocket(agentId, taskId, term);
  const { scale, size } = useTerminalScale(outerRef, term, "fit");

  const handleSend = useCallback(() => {
    if (!input.trim()) return;
    sendMutation.mutate(input.trim());
    setInput("");
  }, [input, sendMutation]);

  return (
    <div className="flex flex-col h-full bg-[#0d0d0d]">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-[rgba(255,255,255,0.06)] shrink-0">
        <span
          className="w-1.5 h-1.5 rounded-full"
          style={{ background: C.online }}
        />
        <span className="text-[10px] text-[var(--color-text-muted)] font-mono">
          {taskId.slice(0, 8)} · session active
        </span>
      </div>

      {/* xterm.js output — canonical size, scaled to fit the container */}
      <div className="flex-1 min-h-0 relative">
        <div ref={outerRef} className="absolute inset-0 overflow-auto">
          <div style={{ width: size ? size.w * scale : undefined, height: size ? size.h * scale : undefined }}>
            <div
              ref={termRef}
              className="p-1"
              style={{ transform: `scale(${scale})`, transformOrigin: "top left" }}
            />
          </div>
        </div>
      </div>

      {/* Input Bar — padding-bottom accounts for virtual keyboard on iOS (M9) */}
      <div
        className="flex items-center gap-2 px-3 py-2 border-t border-[rgba(255,255,255,0.06)] shrink-0"
        style={{ paddingBottom: "calc(0.5rem + var(--keyboard-inset, 0px))" }}
      >
        <span className="text-[11px] text-[var(--color-text-muted)] font-mono shrink-0">$</span>
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") handleSend();
          }}
          placeholder="Enter a command, e.g. /compact"
          className="flex-1 bg-transparent text-[11px] font-mono text-[var(--color-text-primary)] placeholder-[var(--color-text-muted)] outline-none"
        />
        <button
          onClick={handleSend}
          disabled={!input.trim() || sendMutation.isPending}
          className="p-1 rounded text-[var(--color-text-muted)] hover:text-[var(--color-text-primary)] disabled:opacity-30 transition-colors"
        >
          <Send size={12} />
        </button>
      </div>
    </div>
  );
}

// ── Main Export ───────────────────────────────────────────────────────────────

export function CliTerminalTab({ agentId }: Props) {
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);

  // Auto-select first session when none is selected
  const { data: sessions } = useQuery({
    queryKey: ["cli-sessions", agentId],
    queryFn: () => api.agents.cli.sessions(agentId),
    refetchInterval: 3_000,
  });

  useEffect(() => {
    if (!sessions) return;
    if (sessions.length > 0 && !selectedTaskId) {
      // Auto-select first session
      setSelectedTaskId(sessions[0].task_id);
    } else if (selectedTaskId && !sessions.find((s) => s.task_id === selectedTaskId)) {
      // Selected session has ended — reset
      setSelectedTaskId(sessions[0]?.task_id ?? null);
    }
  }, [sessions, selectedTaskId]);

  return (
    <div
      className="flex rounded-xl overflow-hidden border border-[rgba(255,255,255,0.06)]"
      style={{ height: "480px" }}
    >
      {/* Left column: session list */}
      <div className="w-[220px] border-r border-[rgba(255,255,255,0.06)] bg-[rgba(255,255,255,0.02)] shrink-0 overflow-y-auto">
        <SessionList
          agentId={agentId}
          selectedId={selectedTaskId}
          onSelect={setSelectedTaskId}
        />
      </div>

      {/* Right column: terminal */}
      <div className="flex-1 overflow-hidden">
        {selectedTaskId ? (
          <TerminalPanel agentId={agentId} taskId={selectedTaskId} />
        ) : (
          <div className="flex items-center justify-center h-full text-[11px] text-[var(--color-text-muted)]">
            Select a session
          </div>
        )}
      </div>
    </div>
  );
}
