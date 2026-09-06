"use client";

/**
 * ConnectionGroup — Cockpit-Gruppe „Connection" (Spec §4.4). URL mit
 * Copy-Icon + Chip „slot", gebundene Agenten als Textlinks mit Zustandspunkt
 * (working = `current_task_id` gesetzt → STATUS.busy, sonst idle).
 *
 * `RuntimeAgentRef` (`api.runtimes.db.agents()`) trägt kein `current_task_id`
 * — nur `pending_runtime_sync`. Die volle `Agent`-Liste (`api.agents.list()`,
 * gleicher Cache-Schlüssel `["agents"]` wie überall sonst in der App) trägt
 * es aber wirklich; hier per `id` korreliert statt eines erfundenen Zustands
 * (HONESTY RULE). Copy folgt demselben Muster wie `KpiRow.tsx`: Icon-Tausch
 * statt Toast, weil die Codebase keine Toast-Komponente hat.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { Check, Copy } from "lucide-react";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api";
import { C, STATUS } from "@/lib/colors";

export function ConnectionGroup({ runtimeSlug, endpoint }: { runtimeSlug: string; endpoint: string }) {
  const t = useTranslations("runtimes.cockpit");
  const tSlot = useTranslations("runtimes.slot");
  const [copied, setCopied] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["runtime-agents", runtimeSlug],
    queryFn: () => api.runtimes.db.agents(runtimeSlug),
    enabled: !!runtimeSlug,
    staleTime: 15_000,
    retry: false,
  });
  const bound = data?.agents ?? [];

  // Task-Bindung kommt nicht aus `RuntimeAgentRef` (s.o.) — aus der vollen
  // Agentenliste per id korrelieren, nur wenn wirklich Agenten gebunden sind.
  const { data: allAgents } = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.agents.list(undefined, true, false),
    enabled: bound.length > 0,
    staleTime: 15_000,
    retry: false,
  });
  const busyIds = new Set((allAgents ?? []).filter((a) => a.current_task_id != null).map((a) => a.id));

  const onCopy = () => {
    navigator.clipboard?.writeText(endpoint).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    }).catch(() => {});
  };

  return (
    <div className="flex flex-col gap-2.5" data-testid="connection-group">
      <div className="flex items-center gap-2.5 min-w-0">
        <button
          type="button"
          onClick={onCopy}
          className="inline-flex items-center gap-2 font-mono text-[12px] min-w-0 cursor-pointer"
          style={{ color: C.textPrimary }}
          aria-label={t("copyEndpointAria")}
          data-testid="connection-copy"
        >
          {copied ? <Check size={14} style={{ color: C.online }} aria-hidden /> : <Copy size={14} style={{ color: C.textMuted }} aria-hidden />}
          <span className="truncate">{endpoint}</span>
        </button>
        <span
          className="shrink-0 font-mono uppercase rounded-sm px-1.5 py-0.5"
          style={{ fontSize: "9px", letterSpacing: "0.1em", color: C.accent, border: `1px solid ${C.borderAccent}`, background: C.accentSubtle }}
        >
          {tSlot("chip")}
        </span>
      </div>
      {copied && (
        <span className="sr-only" role="status" data-testid="connection-copied">{t("copied")}</span>
      )}

      <div className="flex flex-wrap gap-4 text-[13px]" data-testid="connection-agents">
        {isLoading && <span className="text-[12px]" style={{ color: C.textMuted }}>…</span>}
        {!isLoading && bound.length === 0 && (
          <span className="text-[12px]" style={{ color: C.textMuted }}>{t("noAgents")}</span>
        )}
        {bound.map((a) => {
          const busy = busyIds.has(a.id);
          return (
            <Link
              key={a.id}
              href={`/agents/${a.id}`}
              className="inline-flex items-center gap-1.5"
              style={{ color: C.textSecondary, borderBottom: `1px solid ${C.borderActive}` }}
              data-testid="connection-agent-link"
              data-busy={busy ? "true" : "false"}
              title={busy ? t("agentWorking") : t("agentIdle")}
            >
              <span
                aria-hidden
                className="inline-block rounded-full"
                style={{ width: 6, height: 6, background: busy ? STATUS.busy : STATUS.idle }}
              />
              {a.name}
            </Link>
          );
        })}
      </div>
    </div>
  );
}
