"use client";

/**
 * RuntimePill — compact runtime indicator (Phase 15 T3.4).
 *
 * Two variants:
 *   - "default": full pill with display_name + model_identifier (used in
 *     AgentDetailPage header / overview).
 *   - "compact": just the type-color dot + display_name (used in AgentCard
 *     mini grid on /agents to keep the card under 200px tall).
 *
 * Host (Boss) and openclaw (Henry) agents fall back to a muted scope chip.
 */

import { useQuery } from "@tanstack/react-query";
import { Lock } from "lucide-react";
import { api } from "@/lib/api";
import type { Agent } from "@/lib/types";
import { C } from "@/lib/colors";

export const RUNTIME_TYPE_COLOR: Record<string, string> = {
  lmstudio: C.info,          // #2E6FD8 — local API, info-blue
  vllm_docker: C.online,     // #2B9A4A — running container, online-green
  unsloth: C.warning,        // #B8870A — fine-tune, warm-amber
  openai_compatible: C.accent, // #0FA3A3 — was lila #A855F7, migrated
  cloud: C.textDim,          // #6E6E6E — external, neutral
  // Phase 24 (Hermes) — distinct teal variant, intentionally NOT the same as
  // C.accent so single-instance host workers stand out from cli-bridge runtimes.
  hermes: C.accentHover, // hermes — helle Teal-Stufe
};

interface Props {
  agent: Agent;
  variant?: "default" | "compact";
}

export function RuntimePill({ agent, variant = "default" }: Props) {
  const { data } = useQuery({
    queryKey: ["runtimes-list"],
    queryFn: () => api.runtimes.list(),
    staleTime: 60_000,
    enabled: !!agent.runtime_id,
  });

  const rt = data?.runtimes.find(
    (r) => r.id === agent.runtime_id || r.slug === agent.runtime_id,
  );

  const isCompact = variant === "compact";

  if (rt) {
    const color = RUNTIME_TYPE_COLOR[rt.runtime_type] ?? C.textDim;
    const isLocked = rt.single_instance === true;
    const lockTitle = isLocked ? " · single-instance (locked)" : "";
    if (isCompact) {
      return (
        <span
          className="inline-flex items-center gap-1.5 font-mono text-[10px] px-1.5 py-0.5 rounded-md"
          style={{
            backgroundColor: `${color}14`,
            color: "var(--color-text-secondary)",
            border: `1px solid ${color}33`,
          }}
          title={`${rt.display_name} · ${rt.endpoint}${rt.model_identifier ? ` · ${rt.model_identifier}` : ""}${lockTitle}`}
        >
          <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: color }} />
          <span className="truncate max-w-[120px]">{rt.display_name}</span>
          {isLocked && (
            <Lock
              size={10}
              data-testid="runtime-lock-icon"
              aria-label="single-instance"
              style={{ color }}
            />
          )}
        </span>
      );
    }
    return (
      <span
        className="inline-flex items-center gap-1.5 font-mono text-[12px] px-2 py-0.5 rounded-md max-sm:flex-wrap max-sm:text-[11px]"
        style={{
          backgroundColor: `${color}14`,
          color: "var(--color-text-secondary)",
          border: `1px solid ${color}33`,
        }}
        title={`Runtime: ${rt.display_name} · Endpoint: ${rt.endpoint}${lockTitle}`}
      >
        <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ backgroundColor: color }} />
        <span className="max-sm:break-words">{rt.display_name}</span>
        {rt.model_identifier ? (
          <span
            className="text-[var(--color-text-muted)]"
            style={{ overflowWrap: "anywhere" }}
          >
            · {rt.model_identifier}
          </span>
        ) : null}
        {isLocked && (
          <Lock
            size={12}
            data-testid="runtime-lock-icon"
            aria-label="single-instance"
            style={{ color }}
          />
        )}
      </span>
    );
  }

  // host (Boss, claude binary) — runtime managed outside MC.
  if (agent.agent_runtime === "host") {
    const scope = "host";
    return (
      <span
        className={`inline-flex items-center gap-1.5 font-mono px-${isCompact ? "1.5" : "2"} py-0.5 rounded-md ${isCompact ? "text-[10px]" : "text-[12px]"}`}
        style={{
          backgroundColor: `${C.textDim}1F`,
          color: "var(--color-text-muted)",
          border: `1px solid ${C.textDim}47`,
        }}
        title={`Runtime wird ausserhalb MC verwaltet (${scope})`}
      >
        {agent.model?.split("/").pop() ?? "—"}
        <span className="text-[9px] uppercase tracking-wide">{scope}</span>
      </span>
    );
  }

  // cli-bridge without runtime_id — shouldn't happen after migration 0079
  return (
    <span className={`font-mono text-[var(--color-text-muted)] ${isCompact ? "text-[10px]" : "text-[12px]"}`}>
      no runtime
    </span>
  );
}
