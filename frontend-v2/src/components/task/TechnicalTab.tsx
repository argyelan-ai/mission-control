"use client";

/**
 * TechnicalTab — everything that used to be stacked above the tabs, now as
 * collapsed groups: Description · Briefing · Properties · Relations ·
 * Checklist · References · Git · Workspace · E2E · Transcript · Timeline ·
 * History. Nothing was removed — it is just no longer in front.
 *
 * Open/closed state per group is remembered in localStorage (a group you
 * open stays open the next time).
 */

import { useCallback, useEffect, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { useQuery } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import { ChevronRight, Square, CheckSquare, AlertCircle } from "lucide-react";
import { api } from "@/lib/api";
import { timeAgo } from "@/lib/utils";
import { C, LANE } from "@/lib/colors";
import { TaskDescription } from "./TaskDescription";
import { TaskHistory } from "./TaskHistory";
import { TaskTimeline } from "./TaskTimeline";
import { TaskTranscript } from "./TaskTranscript";
import { E2ETab } from "./E2ETab";
import { WorkspaceTab } from "./WorkspaceTab";
import { GitPanel } from "./GitPanel";
import { TaskReferences } from "./TaskReferences";
import { PropertyMenuCell } from "./TaskDetailMenus";
import type { Agent, Task, TaskChecklistItem, TaskEvent, TaskGitInfo } from "@/lib/types";

const STORAGE_KEY = "mc.taskTechnical.open";

function readOpen(): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Record<string, boolean>) : {};
  } catch {
    return {};
  }
}

export type TechnicalGroup =
  | "description"
  | "briefing"
  | "properties"
  | "relations"
  | "checklist"
  | "references"
  | "git"
  | "workspace"
  | "e2e"
  | "transcript"
  | "timeline"
  | "history";

export function TechnicalTab({
  task,
  agents,
  boardId,
  agent,
  projects,
  creatorName,
  hierarchy,
  dependencies,
  checklist,
  gitInfo,
  showE2E,
  onAssign,
  onProject,
  forceOpen,
}: {
  task: Task;
  agents: Agent[];
  boardId: string;
  agent?: Agent;
  projects: { id: string; name: string }[];
  creatorName: string | null;
  hierarchy?: { parent?: { title: string; status: string } | null; children?: { id: string; title: string; status: string }[] } | null;
  dependencies?: { task_id: string; title: string; status: string }[] | null;
  checklist: TaskChecklistItem[];
  gitInfo?: TaskGitInfo | null;
  showE2E: boolean;
  onAssign: (agentId: string) => void;
  onProject: (projectId: string | null) => void;
  /** A group the parent wants opened right now (e.g. "description" from the glance link). */
  forceOpen?: TechnicalGroup | null;
}) {
  const t = useTranslations("tasks");
  const locale = useLocale();
  const [open, setOpen] = useState<Record<string, boolean>>({});

  useEffect(() => {
    setOpen(readOpen());
  }, []);
  useEffect(() => {
    if (forceOpen) setOpen((o) => ({ ...o, [forceOpen]: true }));
  }, [forceOpen]);

  const toggle = useCallback((key: string) => {
    setOpen((o) => {
      const next = { ...o, [key]: !o[key] };
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      } catch {
        /* private mode — state is still in memory */
      }
      return next;
    });
  }, []);

  // Lazy data — only fetched once its group is open.
  const { data: events, isLoading: isEventsLoading } = useQuery({
    queryKey: ["task-events", task.id],
    queryFn: () => api.tasks.events(boardId, task.id),
    enabled: !!open.history,
  });
  const { data: timeline, isLoading: isTimelineLoading } = useQuery({
    queryKey: ["task-timeline", boardId, task.id],
    queryFn: () => api.tasks.timeline(boardId, task.id),
    enabled: !!open.timeline,
  });

  const briefingFields: { label: string; value: string | null | undefined }[] = task.intake_mode
    ? [
        { label: t("briefType"), value: task.request_kind },
        { label: t("briefOutput"), value: task.desired_output },
        { label: t("briefOutOfScope"), value: task.scope_out },
        { label: t("briefRisks"), value: task.risk_notes },
        { label: t("briefCriteria"), value: task.acceptance_criteria },
        { label: t("briefBrowser"), value: task.needs_browser ? t("yes") : null },
        { label: t("briefE2E"), value: task.e2e_test_required ? t("required") : null },
        { label: t("briefCredentials"), value: task.requires_auth ? t("yes") : null },
        { label: t("briefApproval"), value: task.approval_policy },
        { label: t("briefAutonomy"), value: task.autonomy_level },
        { label: t("briefLinks"), value: task.reference_urls?.join(", ") || null },
        { label: t("briefNotes"), value: task.reference_notes },
      ].filter((f) => f.value)
    : [];

  const checklistDone = checklist.filter((i) => i.status === "done").length;
  const projectName = task.project_id ? (projects.find((p) => p.id === task.project_id)?.name ?? t("projectFallback")) : t("adHoc");
  const children = hierarchy?.children ?? [];
  const hasRelations = !!hierarchy?.parent || children.length > 0 || (dependencies?.length ?? 0) > 0;

  const groups: { key: TechnicalGroup; label: string; count?: string; show: boolean; body: () => React.ReactNode }[] = [
    {
      key: "description",
      label: t("techDescription"),
      show: !!task.description,
      body: () => <TaskDescription description={task.description!} />,
    },
    {
      key: "briefing",
      label: t("techBriefing"),
      count: briefingFields.length ? String(briefingFields.length) : undefined,
      show: briefingFields.length > 0,
      body: () => (
        <div className="space-y-1">
          {briefingFields.map((f) => (
            <div key={f.label} className="text-xs">
              <span style={{ color: C.textMuted }}>{f.label}: </span>
              <span style={{ color: C.textPrimary }}>{f.value}</span>
            </div>
          ))}
        </div>
      ),
    },
    {
      key: "properties",
      label: t("techProperties"),
      show: true,
      body: () => (
        <div className="grid grid-cols-2 gap-px rounded-lg overflow-hidden" style={{ background: C.border, border: `1px solid ${C.border}` }}>
          <PropertyMenuCell
            label={t("assignee")}
            value={agent ? `${agent.emoji ?? ""} ${agent.name}`.trim() : t("unassigned")}
            options={agents.map((a) => ({ id: a.id, label: `${a.emoji ?? ""} ${a.name}`.trim(), active: a.id === task.assigned_agent_id }))}
            onSelect={(id) => id && onAssign(id)}
          />
          <PropertyMenuCell
            label={t("projectFallback")}
            value={projectName}
            options={[
              { id: null, label: t("adHocNoProject"), active: !task.project_id },
              ...projects.map((p) => ({ id: p.id, label: p.name, active: p.id === task.project_id })),
            ]}
            onSelect={onProject}
          />
          <Cell label={t("createdBy")} value={`${creatorName ?? "—"} · ${timeAgo(task.created_at, locale)}`} />
          <Cell label={t("started")} value={task.started_at ? timeAgo(task.started_at, locale) : "—"} />
        </div>
      ),
    },
    {
      key: "relations",
      label: t("techRelations"),
      count: children.length ? t("doneOfTotal", { done: children.filter((c) => c.status === "done").length, total: children.length }) : undefined,
      show: hasRelations,
      body: () => (
        <div className="space-y-2">
          {hierarchy?.parent && (
            <Row label={t("parent")}>
              <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: LANE[hierarchy.parent.status] ?? C.textMuted }} />
              <span className="truncate" style={{ color: C.textSecondary }} title={hierarchy.parent.title}>{hierarchy.parent.title}</span>
            </Row>
          )}
          {children.length > 0 && (
            <div>
              <div className="text-xs mb-1" style={{ color: C.textMuted }}>{t("subtasks")}</div>
              <div className="pl-1 space-y-1">
                {children.map((c) => (
                  <div key={c.id} className="flex items-center gap-1.5">
                    <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: LANE[c.status] ?? C.textMuted }} />
                    <span className="text-xs truncate" style={{ color: c.status === "done" ? C.textMuted : C.textSecondary }} title={c.title}>{c.title}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
          {(dependencies?.length ?? 0) > 0 && (
            <div>
              <div className="text-xs mb-1" style={{ color: C.textMuted }}>{t("dependsOn")}</div>
              <div className="flex flex-col gap-1">
                {dependencies!.map((dep) => (
                  <div key={dep.task_id} className="flex items-center gap-2 text-xs">
                    <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: dep.status === "done" ? C.online : C.textMuted }} />
                    <span style={{ color: dep.status === "done" ? C.textMuted : C.textPrimary }}>{dep.title}</span>
                    <span style={{ color: C.textMuted }}>({dep.status.replace("_", " ")})</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      ),
    },
    {
      key: "checklist",
      label: t("techChecklist"),
      count: checklist.length ? `${checklistDone}/${checklist.length}` : undefined,
      show: checklist.length > 0,
      body: () => (
        <div className="space-y-1">
          {checklist.map((item) => (
            <div key={item.id} className="flex items-center gap-2 text-xs">
              {item.status === "done" ? (
                <CheckSquare size={12} style={{ color: C.online, flexShrink: 0 }} />
              ) : item.status === "blocked" ? (
                <AlertCircle size={12} style={{ color: C.error, flexShrink: 0 }} />
              ) : (
                <Square size={12} style={{ color: C.textMuted, flexShrink: 0 }} />
              )}
              <span style={{ color: item.status === "done" ? C.textMuted : C.textPrimary, textDecoration: item.status === "done" ? "line-through" : "none" }}>
                {item.title}
              </span>
            </div>
          ))}
        </div>
      ),
    },
    { key: "references", label: t("techReferences"), show: true, body: () => <TaskReferences taskId={task.id} /> },
    {
      key: "git",
      label: t("techGit"),
      count: gitInfo?.commits?.length ? String(gitInfo.commits.length) : undefined,
      show: !!gitInfo?.branch,
      body: () => <GitPanel gitInfo={gitInfo!} boardId={boardId} taskId={task.id} />,
    },
    { key: "workspace", label: t("techWorkspace"), show: !!task.workspace_path, body: () => <WorkspaceTab task={task} boardId={boardId} /> },
    { key: "e2e", label: t("techE2E"), show: showE2E, body: () => <E2ETab task={task} boardId={boardId} /> },
    {
      key: "transcript",
      label: t("techTranscript"),
      show: !!(task.spawn_session_key || task.dispatched_at),
      body: () => <TaskTranscript taskId={task.id} isLive={task.status === "in_progress" || task.status === "review"} />,
    },
    {
      key: "timeline",
      label: t("techTimeline"),
      show: true,
      body: () => <TaskTimeline entries={timeline?.entries ?? []} isLoading={isTimelineLoading} truncated={timeline?.truncated} />,
    },
    {
      key: "history",
      label: t("techHistory"),
      show: true,
      body: () => <TaskHistory events={(events as TaskEvent[]) ?? []} isLoading={isEventsLoading} />,
    },
  ];

  return (
    <div>
      {groups
        .filter((g) => g.show)
        .map((g) => {
          const isOpen = !!open[g.key];
          return (
            <div key={g.key} style={{ borderTop: `1px solid ${C.border}` }}>
              <button
                type="button"
                onClick={() => toggle(g.key)}
                aria-expanded={isOpen}
                aria-controls={`tech-${g.key}`}
                className="w-full flex items-center gap-2 py-2.5 text-left cursor-pointer"
              >
                <ChevronRight
                  size={11}
                  className="transition-transform shrink-0"
                  style={{ transform: isOpen ? "rotate(90deg)" : "none", color: C.textDim }}
                />
                <span className="text-xs" style={{ color: isOpen ? C.textPrimary : C.textSecondary }}>{g.label}</span>
                {g.count && (
                  <span className="ml-auto font-mono text-[10px]" style={{ color: C.textDim }}>{g.count}</span>
                )}
              </button>
              <AnimatePresence initial={false}>
                {isOpen && (
                  <motion.div
                    id={`tech-${g.key}`}
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: "auto", opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    transition={{ duration: 0.15 }}
                    className="overflow-hidden"
                  >
                    <div className="pb-3 pl-[19px]">{g.body()}</div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          );
        })}
    </div>
  );
}

function Cell({ label, value }: { label: string; value: string }) {
  return (
    <div className="px-2.5 py-2" style={{ background: C.bgSurface }}>
      <span className="block text-[9px] font-semibold uppercase tracking-[0.07em] mb-0.5" style={{ color: C.textDim }}>{label}</span>
      <span className="text-xs" style={{ color: C.textPrimary }}>{value}</span>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="shrink-0" style={{ color: C.textMuted }}>{label}</span>
      {children}
    </div>
  );
}
