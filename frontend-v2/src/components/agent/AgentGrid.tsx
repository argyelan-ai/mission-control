"use client";

import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { motion, AnimatePresence } from "framer-motion";
import { LayoutGrid, List, Filter } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { useAppStore } from "@/lib/store";
import { AgentCard } from "./AgentCard";
import { GlassCard } from "@/components/shared/GlassCard";
import { StatusDot } from "@/components/shared/StatusDot";
import { Pill } from "@/components/shared/Pill";
import type { Agent } from "@/lib/types";

type ViewMode = "grid" | "list";
type StatusFilter = "all" | "online" | "busy" | "idle" | "offline" | "error";

const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: "all", label: "Alle" },
  { value: "online", label: "Online" },
  { value: "busy", label: "Busy" },
  { value: "idle", label: "Idle" },
  { value: "offline", label: "Offline" },
  { value: "error", label: "Error" },
];

interface AgentGridProps {
  agents: Agent[];
  isLoading?: boolean;
}

export function AgentGrid({ agents, isLoading }: AgentGridProps) {
  const [viewMode, setViewMode] = useState<ViewMode>("grid");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");

  const filteredAgents = useMemo(() => {
    if (statusFilter === "all") return agents;
    return agents.filter((a) => a.status === statusFilter);
  }, [agents, statusFilter]);

  if (isLoading) {
    return (
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {[...Array(6)].map((_, i) => (
          <GlassCard key={i} className="p-4 animate-pulse">
            <div className="h-32 rounded-lg bg-[rgba(255,255,255,0.03)]" />
          </GlassCard>
        ))}
      </div>
    );
  }

  return (
    <div>
      {/* Toolbar */}
      <div className="flex items-center justify-between gap-3 mb-4">
        {/* Status filter */}
        <div className="flex items-center gap-1.5 overflow-x-auto">
          {STATUS_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              onClick={() => setStatusFilter(opt.value)}
              className={cn(
                "text-[11px] px-2.5 py-1 rounded-full transition-all duration-150 cursor-pointer whitespace-nowrap",
                statusFilter === opt.value
                  ? "bg-[var(--color-accent)] text-white"
                  : "bg-[rgba(255,255,255,0.04)] text-[var(--color-text-muted)] hover:text-[var(--color-text-secondary)]"
              )}
            >
              {opt.label}
            </button>
          ))}
        </div>

        {/* View toggle */}
        <div className="flex items-center gap-1 bg-[rgba(255,255,255,0.04)] rounded-lg p-0.5">
          <button
            onClick={() => setViewMode("grid")}
            className={cn(
              "p-1.5 rounded-md transition-colors cursor-pointer",
              viewMode === "grid"
                ? "bg-[rgba(255,255,255,0.08)] text-[var(--color-text-primary)]"
                : "text-[var(--color-text-muted)]"
            )}
          >
            <LayoutGrid size={14} />
          </button>
          <button
            onClick={() => setViewMode("list")}
            className={cn(
              "p-1.5 rounded-md transition-colors cursor-pointer",
              viewMode === "list"
                ? "bg-[rgba(255,255,255,0.08)] text-[var(--color-text-primary)]"
                : "text-[var(--color-text-muted)]"
            )}
          >
            <List size={14} />
          </button>
        </div>
      </div>

      {/* Grid / List */}
      {filteredAgents.length === 0 ? (
        <GlassCard className="py-16 text-center">
          <p className="text-sm text-[var(--color-text-muted)]">
            Keine Agents gefunden
          </p>
        </GlassCard>
      ) : viewMode === "grid" ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          <AnimatePresence mode="popLayout">
            {filteredAgents.map((agent, i) => (
              <motion.div
                key={agent.id}
                initial={{ opacity: 0, y: 12 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.95 }}
                transition={{ duration: 0.25, delay: i * 0.04, ease: [0.16, 1, 0.3, 1] }}
              >
                <AgentCard agent={agent} />
              </motion.div>
            ))}
          </AnimatePresence>
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <AnimatePresence mode="popLayout">
            {filteredAgents.map((agent, i) => (
              <motion.div
                key={agent.id}
                initial={{ opacity: 0, x: -8 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 8 }}
                transition={{ duration: 0.2, delay: i * 0.03 }}
              >
                <AgentListRow agent={agent} />
              </motion.div>
            ))}
          </AnimatePresence>
        </div>
      )}
    </div>
  );
}

// ── List Row ──────────────────────────────────────────────────────────────────

import Link from "next/link";
import { contextPercent, contextColor, timeAgo } from "@/lib/utils";

function AgentListRow({ agent }: { agent: Agent }) {
  const pct = contextPercent(agent.context_tokens, agent.context_max);
  const barColor = contextColor(pct);

  const dotStatus = (() => {
    switch (agent.status) {
      case "online": return "online" as const;
      case "busy": return "busy" as const;
      case "error": return "error" as const;
      case "restarting": return "warning" as const;
      case "idle": return "idle" as const;
      default: return "offline" as const;
    }
  })();

  return (
    <Link href={`/agents/${agent.id}`}>
      <GlassCard className="p-3 hover:border-[rgba(255,255,255,0.12)] transition-all duration-150 cursor-pointer">
        <div className="flex items-center gap-4">
          {/* Emoji + name */}
          <span className="text-lg shrink-0">{agent.emoji ?? "🤖"}</span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium text-[var(--color-text-primary)] truncate">
                {agent.name}
              </span>
              <StatusDot status={dotStatus} size="sm" pulse={dotStatus === "online"} />
            </div>
            <span className="text-[11px] text-[var(--color-text-muted)] font-mono truncate block">
              {agent.model ? agent.model.split("/").pop() : "No model"}
            </span>
          </div>

          {/* Context bar (mini) */}
          <div className="w-20 shrink-0 hidden sm:block">
            <div className="h-1 rounded-full bg-[rgba(255,255,255,0.06)] overflow-hidden">
              <div
                className="h-full rounded-full transition-all"
                style={{ backgroundColor: barColor, width: `${Math.min(pct, 100)}%` }}
              />
            </div>
          </div>

          {/* Last seen */}
          <span className="text-[10px] text-[var(--color-text-muted)] shrink-0 hidden md:block w-20 text-right">
            {timeAgo(agent.last_seen_at)}
          </span>

          {/* Provision */}
          <Pill
            color={
              agent.provision_status === "provisioned" ? C.online :
              agent.provision_status === "error" ? C.error :
              agent.provision_status === "provisioning" ? C.warning :
              C.textDim
            }
            size="sm"
          >
            {agent.provision_status === "provisioned" ? "Live" :
             agent.provision_status === "error" ? "Error" :
             agent.provision_status === "provisioning" ? "Provisioning" :
             "Local"}
          </Pill>
        </div>
      </GlassCard>
    </Link>
  );
}
