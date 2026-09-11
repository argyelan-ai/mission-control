"use client";

/**
 * TaskDetailBody — shared header + content of the task detail (09/2026
 * "cockpit" layout, replaces the 07/2026 stack).
 *
 * One body for both chromes (side panel + modal). The structure answers the
 * operator's questions in order — what is this, is it my turn, how far is it,
 * what was built — and hides the database fields until asked:
 *
 *   Header        title · human status (click to change) · priority · agent · ⋯ · close
 *   Glance        summary · needs-you · 3 numbers · checkpoints   (TaskGlance)
 *   Actions       run control + review                              (TaskActions)
 *   Tabs          Conversation · Changes · Results · Technical
 *
 * `wide` (modal on a desktop viewport) moves Changes + Results into a second
 * column next to the story — the diff is half the page, not a tab.
 * Everything technical (briefing, properties, relations, checklist,
 * references, git, workspace, transcript, timeline, history) lives in the
 * Technical tab as collapsed groups (TechnicalTab). Nothing was removed.
 */

import { useState } from "react";
import { useTranslations } from "next-intl";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, X } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";
import { useAppStore } from "@/lib/store";
import { humanStatus } from "@/lib/taskGlance";
import { TaskActions } from "./TaskActions";
import { TaskComments } from "./TaskComments";
import { DeliverablesTab } from "./DeliverablesTab";
import { ThreadPanel } from "./ThreadPanel";
import { TaskGlance } from "./TaskGlance";
import { ChangesPanel, useBranchDiff } from "./ChangesPanel";
import { TechnicalTab, type TechnicalGroup } from "./TechnicalTab";
import { OverflowMenu, StatusMenu } from "./TaskDetailMenus";
import type { Agent, Task, TaskChecklistItem, TaskGitInfo } from "@/lib/types";

const PRIORITY_COLORS: Record<string, string> = {
  critical: C.error,
  high: C.warning,
  medium: C.textSecondary,
  low: C.textMuted,
};

type Tab = "conversation" | "changes" | "results" | "technical";

export function TaskDetailBody({
  task,
  agents,
  boardId,
  onClose,
  wide = false,
}: {
  task: Task;
  agents: Agent[];
  boardId: string;
  onClose: () => void;
  wide?: boolean;
}) {
  const t = useTranslations("tasks");
  const qc = useQueryClient();
  const [activeTab, setActiveTab] = useState<Tab>("conversation");
  const [conversationView, setConversationView] = useState<"comments" | "thread">("comments");
  const [forceOpen, setForceOpen] = useState<TechnicalGroup | null>(null);

  const agent = agents.find((a) => a.id === task.assigned_agent_id);
  const isActive = task.status === "in_progress" || task.status === "review";
  const currentUser = useAppStore((s) => s.currentUser);

  // ── Mutations ──────────────────────────────────────────────────────────────

  const updateMutation = useMutation({
    mutationFn: (data: Partial<Task>) => api.tasks.update(boardId, task.id, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks", boardId] });
      qc.invalidateQueries({ queryKey: ["pipeline", boardId] });
      qc.invalidateQueries({ queryKey: ["task", boardId, task.id] });
    },
    onError: (e: Error) => notify.error(t("updateFailed", { msg: e.message })),
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.tasks.delete(boardId, task.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks", boardId] });
      qc.invalidateQueries({ queryKey: ["pipeline", boardId] });
      onClose();
    },
    onError: (e: Error) => notify.error(t("deleteFailed", { msg: e.message })),
  });

  // ── Queries (shared by glance + technical) ────────────────────────────────

  const { data: deliverables } = useQuery({
    queryKey: ["deliverables", boardId, task.id, "include_subtasks"],
    queryFn: () => api.tasks.deliverables.list(boardId, task.id, { includeSubtasks: true, depth: 2 }),
    enabled: wide || activeTab === "results",
  });

  // Shared query key with TaskComments — the newest comment feeds the
  // "needs you" line; it also decides whether the E2E group shows up.
  const { data: comments } = useQuery({
    queryKey: ["task-comments", task.id],
    queryFn: () => api.tasks.comments.list(boardId, task.id),
  });
  const commentList = Array.isArray(comments) ? comments : [];
  const hasE2EResult = commentList.some((c) => /\*\*Result:\*\*\s*TEST_(PASS|FAIL)/.test(c.content));
  const latestAgentComment = [...commentList].reverse().find((c) => c.author_type === "agent") ?? null;

  const { data: gitInfo } = useQuery<TaskGitInfo>({
    queryKey: ["task-git-info", boardId, task.id],
    queryFn: () => api.tasks.gitInfo(boardId, task.id),
    enabled: !!task.workspace_path,
    refetchInterval: 30_000,
  });

  const { data: checklist = [] } = useQuery<TaskChecklistItem[]>({
    queryKey: ["task-checklist", boardId, task.id],
    queryFn: () => api.tasks.checklist.list(boardId, task.id),
    refetchInterval: 15_000,
  });

  const { data: hierarchy } = useQuery({
    queryKey: ["task-hierarchy", boardId, task.id],
    queryFn: () => api.tasks.hierarchy(boardId, task.id),
  });

  const { data: dependencies } = useQuery({
    queryKey: ["task-dependencies", task.id],
    queryFn: () => api.tasks.dependencies(boardId, task.id),
  });

  const { data: projects = [] } = useQuery({
    queryKey: ["projects", boardId],
    queryFn: () => api.projects.list(boardId),
    enabled: !!boardId,
  });

  const { data: usersList } = useQuery({
    queryKey: ["users-list"],
    queryFn: () => api.auth.users.list(),
    enabled: !!task.created_by_user_id && task.created_by_user_id !== currentUser?.id,
    staleTime: 60_000,
  });
  const creatorName = task.created_by_user_id
    ? task.created_by_user_id === currentUser?.id
      ? currentUser.name
      : (usersList?.find((u) => u.id === task.created_by_user_id)?.name ?? t("userFallback"))
    : null;

  const { data: branchDiff } = useBranchDiff(task, boardId);

  // ── Derived ────────────────────────────────────────────────────────────────

  const children: { id: string; title: string; status: string }[] = Array.isArray(hierarchy?.children) ? hierarchy!.children : [];
  const subtasks = children.length
    ? { done: children.filter((c) => c.status === "done").length, total: children.length }
    : null;
  const changes = branchDiff && Array.isArray(branchDiff.files)
    ? { commits: branchDiff.commits, files: branchDiff.stats.files, additions: branchDiff.stats.additions, deletions: branchDiff.stats.deletions }
    : null;
  const deliverableList = Array.isArray(deliverables) ? deliverables : [];
  const showE2E = !!task.e2e_test_required || hasE2EResult;

  const tabs: { key: Tab; label: string; count?: number }[] = [
    { key: "conversation", label: t("tabConversation"), count: commentList.length || undefined },
    ...(wide
      ? []
      : [
          { key: "changes" as const, label: t("tabChanges"), count: changes?.files || undefined },
          { key: "results" as const, label: t("tabResults"), count: deliverableList.length || undefined },
        ]),
    { key: "technical", label: t("tabTechnical") },
  ];

  const openDescription = () => {
    setActiveTab("technical");
    setForceOpen("description");
  };

  const results = (
    <div>
      {gitInfo?.pr_url && (
        <a
          href={gitInfo.pr_url}
          target="_blank"
          rel="noreferrer"
          className="flex items-center gap-2 rounded-lg px-3 py-2 mb-2 text-xs"
          style={{ border: `1px solid ${C.border}`, color: C.textPrimary, background: C.bgSurface }}
        >
          <span className="font-medium">{t("resultsPr")}</span>
          <span className="truncate font-mono" style={{ color: C.textMuted }}>{gitInfo.pr_url.replace(/^https?:\/\/(www\.)?github\.com\//, "")}</span>
          <ExternalLink size={11} className="ml-auto shrink-0" style={{ color: C.textDim }} />
        </a>
      )}
      <DeliverablesTab deliverables={deliverableList} boardId={boardId} taskId={task.id} />
    </div>
  );

  const story = (
    <>
      {/* Tabs — mono labels, square accent underline for the active tab */}
      <div className="flex gap-0.5 px-4 tab-strip" style={{ borderBottom: `1px solid ${C.border}` }} role="tablist">
        {tabs.map((tab) => {
          const active = activeTab === tab.key;
          return (
            <button
              key={tab.key}
              role="tab"
              aria-selected={active}
              onClick={() => setActiveTab(tab.key)}
              className="px-2.5 py-2 font-mono text-[10px] uppercase tracking-[0.12em] cursor-pointer transition-colors -mb-px"
              style={{
                color: active ? C.accent : C.textMuted,
                fontWeight: active ? 500 : 400,
                borderBottom: `2px solid ${active ? C.accent : "transparent"}`,
              }}
            >
              {tab.label}
              {tab.count != null && (
                <span className="ml-1 normal-case tracking-normal" style={{ color: C.textDim }}>{tab.count}</span>
              )}
            </button>
          );
        })}
      </div>
      <div className="px-4 py-3 pb-4">
        {activeTab === "conversation" ? (
          <div>
            <div className="flex gap-1 mb-2" role="group" aria-label={t("tabConversation")}>
              {(["comments", "thread"] as const).map((v) => {
                const on = conversationView === v;
                return (
                  <button
                    key={v}
                    type="button"
                    aria-pressed={on}
                    onClick={() => setConversationView(v)}
                    className="px-2 py-0.5 rounded-md text-[11px] cursor-pointer"
                    style={{
                      color: on ? C.textPrimary : C.textMuted,
                      background: on ? C.bgElevated : "transparent",
                      border: `1px solid ${on ? C.borderActive : "transparent"}`,
                    }}
                  >
                    {v === "comments" ? t("convComments") : t("convThread")}
                  </button>
                );
              })}
            </div>
            {conversationView === "comments" ? (
              <TaskComments task={task} boardId={boardId} agents={agents} />
            ) : (
              <ThreadPanel taskId={task.id} />
            )}
          </div>
        ) : activeTab === "changes" ? (
          <ChangesPanel task={task} boardId={boardId} gitInfo={gitInfo} />
        ) : activeTab === "results" ? (
          results
        ) : (
          <TechnicalTab
            task={task}
            agents={agents}
            boardId={boardId}
            agent={agent}
            projects={projects}
            creatorName={creatorName}
            hierarchy={hierarchy}
            dependencies={Array.isArray(dependencies) ? dependencies : null}
            checklist={checklist}
            gitInfo={gitInfo}
            showE2E={showE2E}
            onAssign={(id) => updateMutation.mutate({ assigned_agent_id: id } as Partial<Task>)}
            onProject={(id) => updateMutation.mutate({ project_id: id } as Partial<Task>)}
            forceOpen={forceOpen}
          />
        )}
      </div>
    </>
  );

  return (
    <>
      {/* ── Header ── */}
      <div className="px-4 pt-4 pb-3 shrink-0" style={{ borderBottom: `1px solid ${C.border}` }}>
        <div className="label-sys label-sys--dim mb-1.5">{t("taskLabel")} · {task.id.slice(0, 8)}</div>
        <div className="flex items-start gap-3">
          <h2 className="flex-1 min-w-0 text-[15px] font-semibold leading-snug" style={{ color: C.textPrimary }}>
            {task.title}
          </h2>
          <div className="flex items-center gap-1.5 shrink-0">
            <OverflowMenu isActive={isActive} onDelete={() => deleteMutation.mutate()} deleteLoading={deleteMutation.isPending} />
            <button
              onClick={onClose}
              aria-label={t("closeTaskDetails")}
              className="w-[30px] h-[30px] rounded-md flex items-center justify-center transition-colors hover:bg-[var(--color-bg-hover)] cursor-pointer"
              style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
            >
              <X size={15} />
            </button>
          </div>
        </div>
        <div className="flex items-center gap-1.5 mt-2.5 flex-wrap">
          <StatusMenu
            status={task.status}
            label={t(humanStatus(task).key)}
            pending={updateMutation.isPending}
            onChange={(s) => updateMutation.mutate({ status: s } as Partial<Task>)}
          />
          <span
            className="inline-flex items-center rounded-md px-2 py-1 text-[11px] capitalize"
            style={{ color: PRIORITY_COLORS[task.priority] ?? C.textMuted, border: `1px solid ${C.border}` }}
          >
            {task.priority}
          </span>
          {agent && (
            <span
              className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px]"
              style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
            >
              {agent.emoji} {agent.name}
            </span>
          )}
        </div>
      </div>

      {/* ── Scrollable body ── */}
      <div
        className="flex-1 overflow-y-auto"
        style={{ overscrollBehavior: "contain", WebkitOverflowScrolling: "touch" } as React.CSSProperties}
      >
        <TaskGlance
          task={task}
          boardId={boardId}
          latestComment={latestAgentComment}
          subtasks={subtasks}
          changes={changes}
          onOpenDescription={task.description ? openDescription : undefined}
        />

        <div className="px-4 py-3" style={{ borderBottom: `1px solid ${C.border}` }}>
          <TaskActions task={task} boardId={boardId} />
        </div>

        {wide ? (
          <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1.05fr) minmax(0, 1fr)" }}>
            <div className="min-w-0">{story}</div>
            <div className="min-w-0 px-4 py-3" data-testid="cockpit-work" style={{ borderLeft: `1px solid ${C.border}` }}>
              <div className="text-[10px] font-semibold uppercase tracking-[0.07em] mb-2" style={{ color: C.textDim }}>
                {t("tabChanges")}
              </div>
              <ChangesPanel task={task} boardId={boardId} gitInfo={gitInfo} />
              <div className="text-[10px] font-semibold uppercase tracking-[0.07em] mt-5 mb-2" style={{ color: C.textDim }}>
                {t("tabResults")}
              </div>
              {results}
            </div>
          </div>
        ) : (
          story
        )}
      </div>
    </>
  );
}
