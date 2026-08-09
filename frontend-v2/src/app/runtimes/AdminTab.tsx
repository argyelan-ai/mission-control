"use client";

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { EntityIcon } from "@/components/shared/EntityIcon";
import { HostsSection } from "./HostsSection";
import { RuntimeScheduleTab } from "./RuntimeScheduleTab";
import { CliToolsSection } from "@/components/shared/CliToolsSection";

function SectionHeader({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2 mb-3 px-0.5">
      <span
        className="text-xs font-medium tracking-wider uppercase"
        style={{ color: C.textMuted, letterSpacing: "0.07em", fontSize: "10px" }}
      >
        {label}
      </span>
      <div className="flex-1 h-px" style={{ background: C.border }} />
    </div>
  );
}

// ── KV Reset Schedule ─────────────────────────────────────────────────────────
// Moved from the old page.tsx `KvResetScheduleToggle` (~lines 1545-1644), with
// the collapse toggle removed — the Administration tab is the disclosure now,
// so this always renders expanded.

function KvResetSchedule() {
  const [resetMsg, setResetMsg] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const { data: schedules } = useQuery({
    queryKey: ["runtime-schedules", "lmstudio"],
    queryFn: () => api.runtimes.schedules.list("lmstudio"),
    refetchInterval: 30_000,
  });

  const kvResetMutation = useMutation({
    mutationFn: () => api.lmstudio.kvReset(),
    onSuccess: (data) => {
      setResetMsg(data.message);
      queryClient.invalidateQueries({ queryKey: ["lms-models"] });
    },
    onError: () => setResetMsg("KV Reset failed."),
  });

  const activeSchedule = schedules?.find((s) => s.action === "kv_reset" && s.enabled);

  return (
    <div
      className="rounded-xl overflow-hidden"
      style={{
        border: `1px solid ${C.warning}33`,
        background: `${C.warning}08`,
      }}
    >
      <div
        className="flex items-center justify-between gap-3 px-4 py-2.5"
        style={{ borderBottom: `1px solid ${C.warning}26` }}
      >
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-xs font-medium" style={{ color: C.warning }}>KV Reset Schedule</span>
          <span className="text-xs" style={{ color: C.textMuted }}>
            — remembers active models, unloads all, reloads them
          </span>
          {activeSchedule && (
            <span
              className="text-xs px-1 rounded"
              style={{ background: `${C.online}1F`, color: C.online, fontSize: "9px" }}
            >
              {activeSchedule.time_of_day}
            </span>
          )}
        </div>
        <button
          onClick={() => { setResetMsg(null); kvResetMutation.mutate(); }}
          disabled={kvResetMutation.isPending}
          className="shrink-0 flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed transition-opacity"
          style={{
            background: `${C.warning}1A`,
            border: `1px solid ${C.warning}40`,
            color: C.warning,
          }}
        >
          {kvResetMutation.isPending ? (
            <Loader2 size={11} className="animate-spin" />
          ) : <EntityIcon value="⚡" size={11} />}
          Run now
        </button>
      </div>
      {resetMsg && (
        <div
          className="mx-4 mt-3 text-xs px-3 py-2 rounded-lg"
          style={{
            background: kvResetMutation.isError ? `${C.error}14` : `${C.online}14`,
            border: `1px solid ${kvResetMutation.isError ? `${C.error}33` : `${C.online}33`}`,
            color: C.textSecondary,
          }}
        >
          {resetMsg}
        </div>
      )}
      <RuntimeScheduleTab runtimeId="lmstudio" runtimeType="lmstudio" />
    </div>
  );
}

// ── Administration Tab ───────────────────────────────────────────────────────

export function AdminTab() {
  return (
    <div>
      <div className="mb-8">
        <SectionHeader label="Hosts" />
        <HostsSection />
      </div>

      <div className="mb-8">
        <SectionHeader label="Schedules" />
        <KvResetSchedule />
      </div>

      <div>
        <SectionHeader label="CLI tools" />
        <CliToolsSection />
      </div>
    </div>
  );
}
