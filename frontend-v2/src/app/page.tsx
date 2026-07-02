"use client";

import { useState, useMemo, useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { motion, AnimatePresence } from "framer-motion";
import { format } from "date-fns";
import { AlertTriangle } from "lucide-react";
import { api } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { useAgentStream, useActivityStream } from "@/lib/sse";
import { notify } from "@/lib/notify";
import { CreateTaskModal } from "@/components/shared/CreateTaskModal";
import PipelineView from "@/components/pipeline/PipelineView";
import AppShell from "@/components/layout/AppShell";
import { SystemHealthSection } from "@/components/homepage/SystemHealthSection";
import { ActivityHistoryPanel } from "@/components/homepage/ActivityHistoryPanel";
import { C, sectionVariants, getGreeting, bentoMediaStyles } from "@/components/homepage/colors";

export default function Page() {
  return (
    <AppShell>
      <style dangerouslySetInnerHTML={{ __html: bentoMediaStyles }} />
      <HomePage />
    </AppShell>
  );
}

function HomePage() {
  const qc = useQueryClient();
  const { activeBoardId, currentUser } = useAppStore();
  const [showActivityPanel, setShowActivityPanel] = useState(false);
  const greeting = useMemo(getGreeting, []);

  // ── Queries ────────────────────────────────────────────────────────────────
  const { data: systemStatus, isLoading: statusLoading } = useQuery({
    queryKey: ["system-status"],
    queryFn: api.system.status,
    refetchInterval: 30_000,
  });

  const { data: agents } = useQuery({
    queryKey: ["agents", activeBoardId],
    queryFn: () => api.agents.list(activeBoardId ?? undefined),
    enabled: !!activeBoardId,
  });

  // ── SSE Streams ────────────────────────────────────────────────────────────
  useAgentStream((event, data) => {
    qc.invalidateQueries({ queryKey: ["agents"] });
    if (event?.startsWith("task.")) {
      qc.invalidateQueries({ queryKey: ["pipeline"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
    }
    if (event === "agent.context_warning") {
      notify.warning(data?.title as string);
    }
  });

  useActivityStream((event) => {
    qc.invalidateQueries({ queryKey: ["activity"] });
    if (event?.startsWith("task.")) {
      qc.invalidateQueries({ queryKey: ["pipeline"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
    }
  });

  // ── Derived ───────────────────────────────────────────────────────────────
  const displayName = currentUser?.name?.split(" ")[0] || "Operator";

  const alerts = (agents ?? [])
    .filter((a) => a.context_max && a.context_tokens / a.context_max >= 0.9)
    .map((a) => ({
      label: `${a.name}: Context ${Math.round((a.context_tokens / a.context_max) * 100)}%`,
      color: C.error,
      href: `/agents/${a.id}`,
    }));

  if (!activeBoardId) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-center space-y-2">
          <div className="text-lg font-semibold" style={{ color: C.textPrimary }}>{greeting}, {displayName}</div>
          <div className="text-sm" style={{ color: C.textMuted }}>Kein Board ausgewaehlt</div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-5 max-w-6xl mx-auto">
      {/* Header */}
      <motion.div custom={0} variants={sectionVariants} initial="hidden" animate="visible">
        <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-2">
          <h1 className="text-xl sm:text-2xl font-bold tracking-tight" style={{ color: C.textPrimary }}>{greeting}, {displayName}</h1>
          <div className="flex items-center gap-3 shrink-0">
            <span className="text-sm font-mono" style={{ color: C.textMuted }}>{format(new Date(), "EEE, d. MMM yyyy")}</span>
            <CreateTaskModal activeBoardId={activeBoardId} agents={agents} />
          </div>
        </div>
        <div className="mt-3 rounded-full" style={{ height: 2, background: `linear-gradient(90deg, ${C.accent}, ${C.accentSubtle}, transparent)` }} />
      </motion.div>

      {/* Context warnings */}
      {alerts.length > 0 && (
        <motion.div custom={1} variants={sectionVariants} initial="hidden" animate="visible">
          <div className="px-4 py-2.5 rounded-xl" style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}>
            <div className="flex items-center gap-4 text-sm flex-wrap">
              <AlertTriangle size={14} style={{ color: C.warning }} className="shrink-0" />
              {alerts.map((alert, i) => (
                <a key={i} href={alert.href} className="text-xs font-medium transition-opacity hover:opacity-80" style={{ color: alert.color }}>{alert.label}</a>
              ))}
            </div>
          </div>
        </motion.div>
      )}

      {/* System Health */}
      <motion.div custom={2} variants={sectionVariants} initial="hidden" animate="visible">
        <SystemHealthSection status={systemStatus} loading={statusLoading} onOpenActivity={() => setShowActivityPanel(true)} />
      </motion.div>

      {/* Pipeline */}
      <motion.div custom={3} variants={sectionVariants} initial="hidden" animate="visible">
        <PipelineView boardId={activeBoardId} agents={agents} />
      </motion.div>

      {/* Activity Panel */}
      <AnimatePresence>
        {showActivityPanel && <ActivityHistoryPanel onClose={() => setShowActivityPanel(false)} />}
      </AnimatePresence>
    </div>
  );
}
