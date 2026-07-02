"use client";

import { useQuery } from "@tanstack/react-query";
import { useAppStore } from "@/lib/store";
import { api } from "@/lib/api";

export default function StatusBar() {
  const { setCommandPaletteOpen, activeBoardId, boards } = useAppStore();

  const { data: status } = useQuery({
    queryKey: ["system-status"],
    queryFn: api.system.status,
    refetchInterval: 30_000,
  });

  const { data: metrics } = useQuery({
    queryKey: ["system-metrics"],
    queryFn: api.system.metrics,
    refetchInterval: 30_000,
  });

  // Gateway was retired (Phase 29 / ADR-039). Connection now reflects core deps.
  const dbOk = status?.components?.database?.status === "ok";
  const redisOk = status?.components?.redis?.status === "ok";
  const connected = !!status && dbOk && redisOk;
  const agentsOnline = metrics?.agents?.online ?? 0;
  const agentsTotal = metrics?.agents?.total ?? 0;

  const activeBoard = boards.find((b) => b.id === activeBoardId);

  return (
    <div
      className="hidden md:flex items-center justify-between px-4 shrink-0"
      style={{
        height: "28px",
        backgroundColor: "rgba(255, 255, 255, 0.02)",
        backdropFilter: "blur(12px)",
        WebkitBackdropFilter: "blur(12px)",
        borderTop: "1px solid var(--color-border-subtle)",
        fontSize: "11px",
        color: "var(--color-text-muted)",
      }}
    >
      {/* Left: connection + agents + board */}
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-1.5">
          <span
            className="w-1.5 h-1.5 rounded-full"
            style={{
              backgroundColor: connected
                ? "var(--color-online)"
                : "var(--color-error)",
              boxShadow: connected
                ? "0 0 4px rgba(43, 154, 74, 0.45)"
                : "0 0 4px rgba(194, 56, 56, 0.45)",
            }}
          />
          <span>{connected ? "Connected" : "Offline"}</span>
        </div>

        <div className="flex items-center gap-1.5">
          <span
            className="w-1.5 h-1.5 rounded-full"
            style={{
              backgroundColor:
                agentsOnline > 0
                  ? "var(--color-online)"
                  : "var(--color-text-muted)",
            }}
          />
          <span>
            {agentsOnline}/{agentsTotal} agents
          </span>
        </div>

        {activeBoard && (
          <div
            className="flex items-center gap-1.5"
            style={{ color: "var(--color-text-secondary)" }}
          >
            <span style={{ fontSize: "10px" }}>|</span>
            <span>{activeBoard.name}</span>
          </div>
        )}
      </div>

      {/* Right: keyboard hint */}
      <div className="flex items-center gap-3">
        <button
          onClick={() => setCommandPaletteOpen(true)}
          className="flex items-center gap-1.5 cursor-pointer hover:opacity-70 transition-opacity"
          style={{ color: "var(--color-text-muted)" }}
        >
          <kbd
            className="px-1.5 py-0.5 rounded font-mono"
            style={{
              backgroundColor: "var(--color-bg-elevated)",
              border: "1px solid var(--color-border)",
              fontSize: "10px",
            }}
          >
            Cmd+K
          </kbd>
          <span>Command Palette</span>
        </button>
      </div>
    </div>
  );
}
