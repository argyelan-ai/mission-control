"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { api } from "@/lib/api";
import { C, STATUS, STATUS_TEXT } from "@/lib/colors";
import type { Runtime, RuntimeLiveStatus } from "@/lib/types";
import { fmtCtx } from "./ContextSettings";

const TYPE_LABELS: Record<string, string> = {
  vllm_docker: "vLLM Docker", lmstudio: "LM Studio", unsloth: "Unsloth",
  unsloth_porsche: "Unsloth · PORSCHE", openai_compatible: "OpenAI-compatible",
  cloud: "Cloud API", hermes: "Hermes", grok: "Grok", kimi: "Kimi",
  omp: "OMP", llamacpp_docker: "llama.cpp",
};
export const typeLabel = (t: string) => TYPE_LABELS[t] ?? t;

const ACTIVE = new Set(["ready", "starting", "warming"]);

function dotColor(state: string): string {
  if (state === "ready") return STATUS.online;
  if (state === "starting") return C.info;
  if (state === "warming") return C.warning;
  if (state === "failed") return C.error;
  return STATUS.offline;
}

function AgentChips({ runtime }: { runtime: Runtime }) {
  const slug = runtime.slug ?? runtime.id;
  const { data } = useQuery({
    queryKey: ["runtimes", slug, "agents"],
    queryFn: () => api.runtimes.db.agents(slug),
    staleTime: 15_000,
    retry: false,
  });
  const agents = data?.agents ?? [];
  if (agents.length === 0) return null;
  const shown = agents.slice(0, 3);
  return (
    <span className="flex items-center gap-1 shrink-0" onClick={(e) => e.stopPropagation()}>
      {shown.map((a) => (
        <Link key={a.id} href={`/agents/${a.id}`}
          className="font-mono text-[10px] px-1.5 py-0.5 rounded-md"
          style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.textSecondary }}>
          {a.name}
        </Link>
      ))}
      {agents.length > 3 && (
        <span className="text-[10px]" style={{ color: C.textMuted }}>+{agents.length - 3}</span>
      )}
    </span>
  );
}

function DriftChip() {
  return (
    <span
      className="text-[10px] font-medium px-1.5 py-0.5 rounded-md shrink-0"
      style={{ background: `${C.warning}14`, border: `1px solid ${STATUS.warning}`, color: STATUS_TEXT.warning }}
    >
      Drift
    </span>
  );
}

export function RuntimeListCard({ runtime, live, sizeGb, onOpen }: {
  runtime: Runtime;
  live?: RuntimeLiveStatus;
  sizeGb?: number;
  onOpen: (rt: Runtime) => void;
}) {
  const state = runtime.state ?? "unknown";
  const failed = state === "failed" || (live != null && !live.reachable && ACTIVE.has(state));
  const active = !failed && ACTIVE.has(state);

  const handleClick = () => onOpen(runtime);
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onOpen(runtime);
    }
  };

  const sharedProps = {
    role: "button" as const,
    tabIndex: 0,
    onClick: handleClick,
    onKeyDown: handleKeyDown,
  };

  if (failed) {
    const reason = `Engine unreachable (${live?.consecutive_failures ?? "?"} probes)`;
    return (
      <div
        {...sharedProps}
        className="flex items-center gap-2 px-3 py-2 rounded-lg cursor-pointer"
        style={{
          background: C.bgSurface,
          border: `1px solid ${C.border}`,
          borderLeft: `2px solid ${C.error}`,
        }}
      >
        <span data-testid="state-dot" className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: dotColor("failed") }} />
        <span className="text-sm font-medium truncate" style={{ color: C.textPrimary }}>
          {runtime.display_name}
        </span>
        <span className="text-xs truncate" style={{ color: STATUS_TEXT.error }}>
          {reason}
        </span>
        <span className="flex-1" />
        <AgentChips runtime={runtime} />
      </div>
    );
  }

  if (!active) {
    // Stopped / unknown: single dimmed row.
    return (
      <div
        {...sharedProps}
        className="flex items-center gap-2 px-3 py-2 rounded-lg cursor-pointer opacity-60"
        style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}
      >
        <span className="text-xs shrink-0" style={{ color: STATUS.offline }}>○</span>
        <span className="text-sm truncate" style={{ color: C.textSecondary }}>
          {runtime.display_name} · {typeLabel(runtime.runtime_type)}
        </span>
        <span className="flex-1" />
        <AgentChips runtime={runtime} />
      </div>
    );
  }

  // Active: two-line card.
  const modelName = live?.served_model ?? runtime.model_identifier ?? "—";
  const parts: string[] = [modelName];
  if (sizeGb != null) parts.push(`${sizeGb.toFixed(1)} GB`);
  if (runtime.max_context_len) parts.push(`${fmtCtx(runtime.max_context_len)} ctx`);

  return (
    <div
      {...sharedProps}
      className="flex flex-col gap-1 px-3 py-2.5 rounded-lg cursor-pointer"
      style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}
    >
      <div className="flex items-center gap-2">
        <span data-testid="state-dot" className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: dotColor(state) }} />
        <span className="text-sm font-medium truncate" style={{ color: C.textPrimary }}>
          {runtime.display_name}
        </span>
      </div>
      <div className="flex items-center gap-2 pl-3.5">
        <span className="text-xs font-mono truncate" style={{ color: C.textMuted }}>
          {parts.join(" · ")}
        </span>
        {live?.drift && <DriftChip />}
        <span className="flex-1" />
        <AgentChips runtime={runtime} />
      </div>
    </div>
  );
}
