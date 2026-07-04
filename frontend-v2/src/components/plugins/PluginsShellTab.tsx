"use client";
import { useEffect, useRef, useState, useCallback } from "react";
import { useMutation } from "@tanstack/react-query";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal, Power, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C, STATUS_TEXT, XTERM_THEME } from "@/lib/colors";
import "@xterm/xterm/css/xterm.css";

export function PluginsShellTab() {
  const termRef = useRef<HTMLDivElement>(null);
  const termInstance = useRef<XTerm | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);
  const [starting, setStarting] = useState(false);

  const connectWs = useCallback(() => {
    if (!termInstance.current) return;
    const term = termInstance.current;

    const url = api.plugins.shellWsUrl();
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      // Re-fit after connect, then send resize
      requestAnimationFrame(() => {
        fitAddonRef.current?.fit();
        if (term.cols && term.rows) {
          ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
        }
      });
    };

    ws.onmessage = (evt) => {
      if (evt.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(evt.data));
      } else {
        term.write(evt.data);
      }
    };

    ws.onclose = () => {
      setConnected(false);
    };

    ws.onerror = () => {
      setConnected(false);
    };

    // Keyboard input → WS
    const dataDisposable = term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(data);
    });

    return () => {
      dataDisposable.dispose();
      ws.close(1000);
      wsRef.current = null;
    };
  }, []);

  // Initialize xterm.js
  useEffect(() => {
    if (!termRef.current || termInstance.current) return;

    const term = new XTerm({
      theme: XTERM_THEME,
      scrollback: 5000,
      convertEol: true,
      fontFamily: '"JetBrains Mono", "Fira Code", "Cascadia Code", monospace',
      fontSize: 12,
      lineHeight: 1.4,
      cursorBlink: true,
      cursorStyle: "bar",
      disableStdin: false,
    });

    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.open(termRef.current);
    requestAnimationFrame(() => fitAddon.fit());

    termInstance.current = term;
    fitAddonRef.current = fitAddon;

    // Resize observer
    const ro = new ResizeObserver(() => {
      fitAddon.fit();
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows })
        );
      }
    });
    ro.observe(termRef.current);

    return () => {
      ro.disconnect();
      term.dispose();
      termInstance.current = null;
    };
  }, []);

  // Auto-reconnect: check if plugins-shell tmux session already exists on mount
  const autoConnectDone = useRef(false);
  useEffect(() => {
    if (autoConnectDone.current || !termInstance.current) return;
    autoConnectDone.current = true;
    // startShell is idempotent — returns ok:true if already running
    api.plugins.startShell().then((res) => {
      if (res?.ok) {
        setTimeout(() => connectWs(), 300);
      }
    }).catch(() => {
      // Bridge not reachable — ignore, user can click "Shell starten"
    });
  }, [connectWs]);

  // Start shell + connect
  const startShell = useMutation({
    mutationFn: () => api.plugins.startShell(),
    onSuccess: () => {
      setStarting(false);
      setTimeout(() => connectWs(), 500);
    },
    onError: (e: Error) => {
      setStarting(false);
      notify.error(`Failed to start shell: ${e.message}`);
    },
  });

  const handleStart = () => {
    setStarting(true);
    termInstance.current?.clear();
    startShell.mutate();
  };

  const handleStop = async () => {
    wsRef.current?.close(1000);
    setConnected(false);
    try {
      await api.plugins.stopShell();
    } catch {
      // ignore
    }
    termInstance.current?.clear();
    termInstance.current?.writeln("\x1b[33m[Session ended]\x1b[0m");
  };

  return (
    <div className="space-y-3">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Terminal size={14} style={{ color: connected ? C.online : "var(--color-text-muted)" }} />
          <span className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
            🛠️ Installer
          </span>
          <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>
            claude-sonnet-4.6 · MCP/Plugin/Skill-Setup
          </span>
          {connected && (
            <span
              className="text-[10px] px-1.5 py-0.5 rounded"
              style={{ background: `${C.online}1A`, color: C.online }}
            >
              connected
            </span>
          )}
        </div>
        <div className="flex gap-2">
          {!connected ? (
            <button
              onClick={handleStart}
              disabled={starting}
              className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg cursor-pointer transition-colors"
              style={{ backgroundColor: C.accent, color: "#fff" }}
            >
              {starting ? <Loader2 size={12} className="animate-spin" /> : <Terminal size={12} />}
              Start installer
            </button>
          ) : (
            <button
              onClick={handleStop}
              className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg cursor-pointer transition-colors"
              style={{
                backgroundColor: `${C.error}1A`,
                color: C.error,
                border: `1px solid ${C.error}33`,
              }}
            >
              <Power size={12} />
              Stop
            </button>
          )}
        </div>
      </div>

      {/* Help text when not connected */}
      {!connected && !starting && (
        <div
          className="text-xs p-3 rounded-xl"
          style={{
            backgroundColor: "rgba(255,255,255,0.02)",
            border: "1px solid rgba(255,255,255,0.06)",
            color: "var(--color-text-muted)",
          }}
        >
          Start the installer to set up MCP servers, CLI plugins, or custom skills.
          The installer knows the MC API + Vault — just tell it what you want.
          <br />
          <code className="text-[11px] mt-1 inline-block" style={{ color: STATUS_TEXT.info }}>
            "Install Brave Search MCP for Researcher"
          </code>
          <br />
          <span className="text-[10px] mt-1 inline-block" style={{ color: "var(--color-text-muted)", opacity: 0.7 }}>
            Also via task delegation: <code>mc delegate --to Installer ...</code>
          </span>
        </div>
      )}

      {/* Terminal */}
      <div
        className="rounded-xl overflow-hidden flex-1"
        style={{
          backgroundColor: XTERM_THEME.background,
          border: "1px solid rgba(255,255,255,0.06)",
          height: "calc(100dvh - 240px)",
          minHeight: "300px",
        }}
      >
        <div ref={termRef} className="w-full h-full" />
      </div>
    </div>
  );
}
