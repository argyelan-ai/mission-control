"use client";

/**
 * OverviewTab — host-grouped runtime overview (redesign, Task 5). Self-contained:
 * owns its own queries + selected-runtime state. Renders in order: summary line
 * → host sections (Hosts registry, ADR-048) → Cloud → Unassigned (only when
 * non-empty) → RuntimeDetailPanel.
 *
 * Fixes the old page's regression where hostless "cloud" runtimes (e.g. the
 * Anthropic API runtime) were never rendered anywhere — grouping.ts's
 * `groupRuntimes()` now explicitly buckets them into `cloud`.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { C, STATUS_TEXT } from "@/lib/colors";
import type { Runtime } from "@/lib/types";
import { groupRuntimes, summarizeStates } from "./grouping";
import { HostSection } from "./HostSection";
import { RuntimeListCard } from "./RuntimeListCard";
import { RuntimeDetailPanel } from "./RuntimeDetailPanel";

export function OverviewTab() {
  // Store the id, not the object — the runtimes list refetches on its own
  // interval (and after every Start/Stop/model-edit mutation), so a snapshot
  // object goes stale the moment the underlying state changes. Deriving the
  // selected runtime fresh from the latest query data every render keeps the
  // detail panel in sync with reality.
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["runtimes"],
    queryFn: () => api.runtimes.list(),
    refetchInterval: 15_000,
  });

  const { data: liveData } = useQuery({
    queryKey: ["runtimes", "live-status"],
    queryFn: () => api.runtimes.liveStatus(),
    refetchInterval: 30_000,
  });

  const { data: hosts } = useQuery({
    queryKey: ["hosts"],
    queryFn: api.hosts.list,
  });

  const { data: lmsData } = useQuery({
    queryKey: ["lms-models"],
    queryFn: () => api.lmstudio.list(),
    refetchInterval: 15_000,
  });

  const runtimes = data?.runtimes ?? [];
  const selected = runtimes.find((r) => r.id === selectedId) ?? null;
  const groups = groupRuntimes(runtimes, hosts ?? []);
  const counts = summarizeStates(runtimes, liveData?.live);
  const isEmpty = !isLoading && !error && runtimes.length === 0;

  const sizeGbMap = new Map((lmsData?.models ?? []).map((m) => [m.id, m.size_gb]));
  const getSizeGb = (rt: Runtime) =>
    rt.lms_identifier ? sizeGbMap.get(rt.lms_identifier) : undefined;
  const getLive = (rt: Runtime) => liveData?.live?.[rt.slug ?? rt.id];

  const renderCard = (rt: Runtime) => (
    <RuntimeListCard
      key={rt.id}
      runtime={rt}
      live={getLive(rt)}
      sizeGb={getSizeGb(rt)}
      onOpen={(r) => setSelectedId(r.id)}
    />
  );

  return (
    <div>
      {/* Summary line */}
      {!isEmpty && (
        <div className="flex items-center gap-1.5 text-xs mb-5" style={{ color: C.textMuted }}>
          <span style={{ color: C.online }}>●</span>
          <span>{counts.active} active</span>
          <span style={{ color: C.borderSubtle }}>·</span>
          <span>{counts.stopped} stopped</span>
          <span style={{ color: C.borderSubtle }}>·</span>
          <span style={{ color: counts.failed > 0 ? STATUS_TEXT.error : undefined }}>
            {counts.failed} failed
          </span>
        </div>
      )}

      {isLoading && (
        <div className="flex items-center gap-2 py-2" style={{ color: C.textMuted }}>
          <Loader2 size={13} className="animate-spin" />
          <span className="text-xs">Loading runtimes...</span>
        </div>
      )}

      {!!error && (
        <div
          className="flex items-center gap-2 text-xs px-4 py-3 rounded-xl mb-4"
          style={{ color: STATUS_TEXT.error, background: `${C.error}0F`, border: `1px solid ${C.error}26` }}
        >
          <AlertCircle size={13} />
          Failed to load runtimes.
        </div>
      )}

      {isEmpty && (
        <div className="text-xs py-8 text-center" style={{ color: C.textMuted }}>
          <div style={{ color: C.textSecondary }}>No runtimes configured.</div>
          <div className="mt-1">Add a runtime above, or check the Models tab for installed models.</div>
        </div>
      )}

      {!isEmpty && (
        <>
          {/* Host groups (ADR-048) — always rendered for enabled hosts, even with no runtimes */}
          {groups.hosts.map((group) => {
            if (!group.host.enabled) return null;
            const dimmed =
              group.host.power_managed === true &&
              group.runtimes.length > 0 &&
              group.runtimes.every((r) => (r.state ?? "unknown") !== "ready");
            // local hosts return no metrics fields — a "GPU 0%" bar there is
            // meaningless (parity with the old HostMetricsBar filter).
            const showMetrics = group.host.kind !== "local";
            return (
              <HostSection
                key={group.host.id}
                title={group.host.display_name}
                metricsHost={showMetrics ? group.host : undefined}
                dimmed={dimmed}
              >
                {group.runtimes.length === 0 ? (
                  <div className="text-xs py-2" style={{ color: C.textMuted }}>
                    No runtimes on this host.
                  </div>
                ) : (
                  <div className="flex flex-col gap-2">{group.runtimes.map(renderCard)}</div>
                )}
              </HostSection>
            );
          })}

          {/* Cloud — hosted APIs, no host binding */}
          <HostSection title="Cloud" subtitle="Hosted APIs — no local hardware">
            {groups.cloud.length === 0 ? (
              <div className="text-xs py-2" style={{ color: C.textMuted }}>
                No cloud runtimes configured.
              </div>
            ) : (
              <div className="flex flex-col gap-2">{groups.cloud.map(renderCard)}</div>
            )}
          </HostSection>

          {/* Unassigned — only shown when non-empty */}
          {groups.unassigned.length > 0 && (
            <HostSection title="Unassigned" subtitle="No host bound — bind one in Administration">
              <div className="flex flex-col gap-2">{groups.unassigned.map(renderCard)}</div>
            </HostSection>
          )}
        </>
      )}

      <RuntimeDetailPanel
        runtime={selected}
        live={selected ? getLive(selected) : undefined}
        open={selectedId != null}
        onClose={() => setSelectedId(null)}
      />
    </div>
  );
}
