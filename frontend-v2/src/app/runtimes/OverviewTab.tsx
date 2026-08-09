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
  const [selected, setSelected] = useState<Runtime | null>(null);

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
    queryKey: ["lmstudio-models"],
    queryFn: () => api.lmstudio.list(),
    refetchInterval: 15_000,
  });

  const runtimes = data?.runtimes ?? [];
  const groups = groupRuntimes(runtimes, hosts ?? []);
  const counts = summarizeStates(runtimes);

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
      onOpen={setSelected}
    />
  );

  return (
    <div>
      {/* Summary line */}
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

      {/* Host groups (ADR-048) — always rendered, even with no runtimes */}
      {groups.hosts.map((group) => {
        const dimmed =
          group.host.power_managed === true &&
          group.runtimes.every((r) => (r.state ?? "unknown") !== "ready");
        return (
          <HostSection key={group.host.id} title={group.host.display_name} metricsHost={group.host} dimmed={dimmed}>
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

      <RuntimeDetailPanel
        runtime={selected}
        live={selected ? getLive(selected) : undefined}
        open={selected != null}
        onClose={() => setSelected(null)}
      />
    </div>
  );
}
