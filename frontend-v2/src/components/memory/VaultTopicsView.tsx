"use client";

/**
 * VaultTopicsView — Topic-centric cluster cards for the /memory Themen tab.
 *
 * Fetches GET /vault/topics and renders each cluster as a card with:
 * - Cluster label (derived from common tags)
 * - Note count badge
 * - Top-5 note titles
 * - Contributing agent avatars
 */

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Layers, FileText, Users } from "lucide-react";
import { C } from "@/lib/colors";

interface TopicCluster {
  cluster_id: number;
  label: string;
  note_count: number;
  top_notes: string[];
  agents: string[];
}

interface TopicsResponse {
  topics: TopicCluster[];
  total_notes: number;
}

// Agent color map — consistent with the rest of the memory UI (no purple)
const AGENT_COLORS: Record<string, string> = {
  boss:       C.accent,          // teal (was purple)
  researcher: C.online,          // #2B9A4A
  sparky:     C.warning,         // #B8870A
  deployer:   C.info,            // #2E6FD8
  tester:     C.error,           // #C23838
  davinci:    "#EC4899",         // pink — external brand identity
  freecode:   C.accent,          // teal (was indigo #6366F1 — non-brand, mapped to accent)
  jarvis:     C.accentHover,     // #14C4C4
  system:     C.textMuted,       // #888888
};

function agentColor(agent: string): string {
  return AGENT_COLORS[agent] || C.textSecondary;
}

export function VaultTopicsView() {
  const { data, isLoading, error } = useQuery<TopicsResponse>({
    queryKey: ["vault-topics"],
    queryFn: () => api.vault.topics(),
    staleTime: 60_000,
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="flex items-center gap-3 text-[var(--color-text-muted)]">
          <Layers size={18} className="animate-pulse" />
          <span className="text-sm font-mono">Themen werden geladen...</span>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="text-center py-20 text-[var(--color-text-muted)] text-sm">
        Themen konnten nicht geladen werden.
      </div>
    );
  }

  const topics = data?.topics || [];
  const totalNotes = data?.total_notes || 0;

  if (topics.length === 0) {
    return (
      <div className="text-center py-20 text-[var(--color-text-muted)]">
        <Layers size={32} className="mx-auto mb-3 opacity-40" />
        <p className="text-sm">Noch keine Themen-Cluster vorhanden.</p>
        <p className="text-xs mt-1 opacity-60">
          Themen werden aus semantischen Embeddings generiert.
        </p>
      </div>
    );
  }

  return (
    <div>
      {/* Summary */}
      <div className="flex items-center gap-2 mb-6 text-[var(--color-text-muted)] text-xs font-mono">
        <Layers size={13} />
        <span>
          {topics.length} Themen aus {totalNotes} Notes
        </span>
      </div>

      {/* Topic Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {topics.map((topic) => (
          <div
            key={topic.cluster_id}
            className="rounded-xl p-5 transition-all hover:scale-[1.01]"
            style={{
              background: "rgba(255,255,255,0.02)",
              border: "1px solid rgba(255,255,255,0.06)",
            }}
          >
            {/* Header */}
            <div className="flex items-start justify-between mb-3">
              <h3
                className="text-sm font-semibold tracking-tight"
                style={{ color: "var(--color-text-primary)" }}
              >
                {topic.label}
              </h3>
              <span
                className="px-2 py-0.5 rounded-full text-[10px] font-mono font-semibold"
                style={{
                  background: C.accentSubtle,
                  color: C.accent,
                }}
              >
                {topic.note_count}
              </span>
            </div>

            {/* Top Notes */}
            <ul className="space-y-1.5 mb-4">
              {topic.top_notes.map((title, i) => (
                <li
                  key={i}
                  className="flex items-start gap-2 text-xs"
                  style={{ color: "var(--color-text-muted)" }}
                >
                  <FileText
                    size={11}
                    className="mt-0.5 shrink-0 opacity-40"
                  />
                  <span className="line-clamp-1">{title}</span>
                </li>
              ))}
            </ul>

            {/* Agent Dots */}
            <div className="flex items-center gap-1.5">
              <Users size={11} className="opacity-30" />
              {topic.agents.map((agent) => (
                <span
                  key={agent}
                  className="px-1.5 py-0.5 rounded text-[9px] font-mono"
                  style={{
                    background: `${agentColor(agent)}18`,
                    color: agentColor(agent),
                  }}
                >
                  {agent}
                </span>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
