"use client";

import { useState, useEffect } from "react";
import { useQueryClient, useMutation } from "@tanstack/react-query";
import { Server, Globe, Zap, Trash2, Check, Info } from "lucide-react";
import type { Agent, MCPServer } from "@/lib/types";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";

interface Props {
  servers: MCPServer[];
  agents: Agent[];
  showDeleteButton?: boolean;
  onDeleteServer?: (name: string) => void;
}

const TRANSPORT_ICON = {
  stdio: Server,
  http: Globe,
  sse: Zap,
} as const;

export function MCPServerMatrix({
  servers,
  agents,
  showDeleteButton = false,
  onDeleteServer,
}: Props) {
  const qc = useQueryClient();

  const initial: Record<string, string[] | null> = Object.fromEntries(
    agents.map((a) => [a.id, a.mcp_servers ?? null]),
  );
  const [assignments, setAssignments] = useState<Record<string, string[] | null>>(initial);
  const [savedAssignments, setSavedAssignments] = useState<Record<string, string[] | null>>(initial);
  const [tooltipServer, setTooltipServer] = useState<string | null>(null);

  // Re-sync when parent re-fetches and passes a new agents array
  useEffect(() => {
    const fresh = Object.fromEntries(agents.map((a) => [a.id, a.mcp_servers ?? null]));
    setAssignments(fresh);
    setSavedAssignments(fresh);
  }, [agents]);

  const toggleMutation = useMutation({
    mutationFn: ({
      agentId,
      servers: next,
    }: {
      agentId: string;
      servers: string[] | null;
    }) => api.mcpServers.setForAgent(agentId, next),
    onError: (_e, vars) => {
      // Roll back this agent's assignment to the saved snapshot
      setAssignments((s) => ({ ...s, [vars.agentId]: savedAssignments[vars.agentId] ?? null }));
      notify.error("MCP-Zuweisung fehlgeschlagen");
    },
    onSuccess: (_data, vars) => {
      // Promote optimistic state to saved
      setSavedAssignments((s) => ({ ...s, [vars.agentId]: vars.servers }));
      qc.invalidateQueries({ queryKey: ["mcp-servers"] });
      qc.invalidateQueries({ queryKey: ["agents"] });
    },
  });

  function toggle(agentId: string, serverName: string) {
    // CRITICAL: preserve null-means-all semantics (do not change this resolution)
    const current = assignments[agentId] ?? servers.map((s) => s.name);
    const next = current.includes(serverName)
      ? current.filter((n) => n !== serverName)
      : [...current, serverName];
    setAssignments((s) => ({ ...s, [agentId]: next })); // optimistic
    toggleMutation.mutate({ agentId, servers: next });
  }

  if (servers.length === 0) {
    return (
      <div
        className="rounded-xl p-8 text-center"
        style={{
          backgroundColor: C.bgElevated,
          border: "1px solid rgba(255,255,255,0.06)",
        }}
      >
        <Server className="mx-auto h-8 w-8" style={{ color: "rgba(255,255,255,0.15)" }} />
        <p className="mt-3 text-sm" style={{ color: "var(--color-text-muted)" }}>
          Keine MCP-Server installiert
        </p>
        <p className="mt-1 text-xs" style={{ color: "rgba(255,255,255,0.2)" }}>
          Boss kann Install-Requests stellen, oder du installierst via CLI.
        </p>
      </div>
    );
  }

  return (
    <div
      className="overflow-x-auto rounded-xl"
      style={{
        backgroundColor: C.bgElevated,
        border: "1px solid rgba(255,255,255,0.06)",
        overscrollBehaviorX: "contain",
      } as React.CSSProperties}
      tabIndex={0}
      role="region"
      aria-label="MCP Server Zuweisung"
    >
      <table className="w-full text-xs">
        <thead>
          <tr>
            <th
              className="sticky left-0 z-10 p-3 text-left font-medium"
              style={{
                backgroundColor: C.bgElevated,
                color: "var(--color-text-muted)",
              }}
            >
              MCP Server
            </th>
            {agents.map((a) => (
              <th
                key={a.id}
                className="p-3 text-center font-medium min-w-[80px]"
                style={{ color: "var(--color-text-secondary)" }}
              >
                <span className="text-base">{a.emoji}</span>
                <br />
                <span className="text-[10px]">{a.name}</span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {servers.map((s) => {
            const Icon = TRANSPORT_ICON[s.transport as keyof typeof TRANSPORT_ICON] ?? Server;
            return (
              <tr
                key={s.name}
                className="border-t"
                style={{ borderColor: "rgba(255,255,255,0.04)" }}
              >
                <td
                  className="sticky left-0 z-10 p-3"
                  style={{ backgroundColor: "var(--bg-elevated)" }}
                >
                  <div className="group flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2 min-w-0">
                      <Icon size={14} style={{ color: C.accent }} />
                      <div className="min-w-0 flex items-center gap-1.5">
                        <div className="min-w-0">
                          <div
                            className="font-medium"
                            style={{ color: "var(--color-text-primary)" }}
                          >
                            {s.name}
                          </div>
                          {s.description && (
                            <div className="text-[10px] truncate" style={{ color: "var(--color-text-muted)" }}>
                              {s.description}
                            </div>
                          )}
                          {(s.command || s.url) && (
                            <div className="font-mono text-[10px] truncate" style={{ color: "rgba(255,255,255,0.3)" }}>
                              {s.command ?? s.url}
                            </div>
                          )}
                        </div>
                        {(s.description || s.command || s.url) && (
                          <div className="relative shrink-0">
                            <button
                              onMouseEnter={() => setTooltipServer(s.name)}
                              onMouseLeave={() => setTooltipServer(null)}
                              className="flex items-center cursor-pointer"
                              style={{ color: "rgba(255,255,255,0.25)" }}
                              aria-label={`Info zu ${s.name}`}
                            >
                              <Info size={11} />
                            </button>
                            {tooltipServer === s.name && (
                              <div
                                className="absolute left-0 top-5 z-50 rounded-lg p-2.5 text-[11px] leading-relaxed pointer-events-none"
                                style={{
                                  width: "220px",
                                  background: C.bgElevated,
                                  border: "1px solid rgba(255,255,255,0.1)",
                                  boxShadow: "0 8px 24px rgba(0,0,0,0.6)",
                                  color: "var(--color-text-secondary)",
                                }}
                              >
                                {s.description && <p>{s.description}</p>}
                                {(s.command || s.url) && (
                                  <p
                                    className="mt-1 font-mono text-[10px] break-all"
                                    style={{ color: "rgba(255,255,255,0.4)" }}
                                  >
                                    {s.command ?? s.url}
                                  </p>
                                )}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                    {showDeleteButton && (
                      <button
                        onClick={() => onDeleteServer?.(s.name)}
                        className="p-1 rounded cursor-pointer opacity-0 group-hover:opacity-100 transition-opacity shrink-0 touch-visible"
                        style={{ color: "var(--color-text-muted)" }}
                        title={`${s.name} entfernen`}
                      >
                        <Trash2 size={13} />
                      </button>
                    )}
                  </div>
                </td>
                {agents.map((a) => {
                  const allowlist = assignments[a.id] ?? null; // undefined → null (all-assigned) until useEffect syncs
                  const enabled = allowlist === null || allowlist.includes(s.name);
                  return (
                    <td key={a.id} className="p-3 text-center">
                      <button
                        onClick={() => toggle(a.id, s.name)}
                        className="w-5 h-5 rounded flex items-center justify-center mx-auto cursor-pointer transition-colors"
                        style={{
                          backgroundColor: enabled
                            ? `${C.accent}33`
                            : "rgba(255,255,255,0.04)",
                          border: `1px solid ${
                            enabled
                              ? `${C.accent}66`
                              : "rgba(255,255,255,0.08)"
                          }`,
                        }}
                        aria-label={`${enabled ? "Deactivate" : "Activate"} ${s.name} for ${a.name}`}
                      >
                        {enabled && <Check size={12} style={{ color: C.accent }} />}
                      </button>
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
