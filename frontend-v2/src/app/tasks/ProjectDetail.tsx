"use client";

/**
 * ProjectDetail — the project/phase view for /tasks (review fix round 6,
 * finding 3). Extracted out of `page.tsx` so the page module keeps the
 * default-export-only contract Next.js requires for its generated route
 * types: a named `export function ProjectDetail` living in `page.tsx`
 * passed `tsc --noEmit` in a worktree with no `.next/` folder, but fails it
 * once Next's generated `.next/types/app/tasks/page.ts` exists (any tree
 * after `next dev`/`next build`) — "Property 'ProjectDetail' is
 * incompatible with index signature". Same pattern as `TaskRow.tsx` next to
 * this file.
 */

import { useState, useMemo, useEffect, useRef } from "react";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { motion, AnimatePresence } from "framer-motion";
import {
  FolderKanban,
  ChevronDown,
  ChevronRight,
  Play,
  Check,
  Plus,
  GitBranch,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Pill } from "@/components/shared/Pill";
import type { Task, Agent, Project, Tag } from "@/lib/types";
import { C, alpha } from "@/lib/colors";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { EntityIcon } from "@/components/shared/EntityIcon";
import { STATUS_CONFIG, TaskRow, TaskStatusDot } from "./TaskRow";
import { useHeadRuns } from "@/components/heads/useHeadRuns";
import type { HeadRun } from "@/lib/heads";

function TagChip({ tag, size = "sm" }: { tag: Tag; size?: "xs" | "sm" }) {
  const color = tag.color || C.accent;
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-sm font-mono font-medium",
        size === "xs" ? "text-[9px] px-1.5 py-0" : "text-[10px] px-2 py-0.5"
      )}
      style={{
        backgroundColor: alpha(color, 0.09),
        color: color,
        border: `1px solid ${alpha(color, 0.19)}`,
      }}
    >
      {tag.name}
    </span>
  );
}

// ── Tag Colors ─────────────────────────────────────────────────────────────

const TAG_COLORS = [
  C.accent,
  C.online,
  C.warning,
  C.info,
  C.error,
  C.accentHover,
];

// ── Tag Manager Popover ──────────────────────────────────────────────────────

function TagManager({
  projectId,
  assignedTags,
  onClose,
}: {
  projectId: string;
  assignedTags: Tag[];
  onClose: () => void;
}) {
  // iOS-safe scroll lock while popover is open (M4)
  useBodyScrollLock(true);

  const t = useTranslations("tasks");
  const qc = useQueryClient();
  const ref = useRef<HTMLDivElement>(null);
  const [newTagName, setNewTagName] = useState("");
  const [selectedColor, setSelectedColor] = useState(TAG_COLORS[0]);

  const { data: allTags = [] } = useQuery({
    queryKey: ["tags"],
    queryFn: api.tags.list,
  });

  const assignedIds = new Set(assignedTags.map((t) => t.id));

  const assignMutation = useMutation({
    mutationFn: (tagId: string) =>
      api.tags.assignToProject(projectId, { tag_id: tagId }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["all-project-tags"] }),
  });

  const removeMutation = useMutation({
    mutationFn: (tagId: string) =>
      api.tags.removeFromProject(projectId, tagId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["all-project-tags"] }),
  });

  const createMutation = useMutation({
    mutationFn: (data: { name: string; color: string }) =>
      api.tags.assignToProject(projectId, { name: data.name, color: data.color }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["all-project-tags"] });
      qc.invalidateQueries({ queryKey: ["tags"] });
      setNewTagName("");
    },
  });

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [onClose]);

  function handleToggle(tagId: string) {
    if (assignedIds.has(tagId)) removeMutation.mutate(tagId);
    else assignMutation.mutate(tagId);
  }

  function handleCreate() {
    const name = newTagName.trim();
    if (!name) return;
    createMutation.mutate({ name, color: selectedColor });
  }

  return (
    <div
      ref={ref}
      className="absolute top-full left-0 mt-1 w-56 rounded-md shadow-xl z-50 overflow-hidden"
      role="dialog"
      aria-modal="true"
      aria-label={t("manageTags")}
      style={{
        backgroundColor: C.bgBase,
        border: `1px solid ${C.border}`,
        boxShadow: "var(--shadow-elevated)",
      }}
    >
      {/* Existing tags */}
      <div className="max-h-48 overflow-y-auto py-1">
        {allTags.length === 0 && (
          <div className="px-3 py-2 text-xs" style={{ color: C.textMuted }}>
            {t("noTagsYet")}
          </div>
        )}
        {allTags.map((tag) => (
          <button
            key={tag.id}
            onClick={() => handleToggle(tag.id)}
            className="w-full flex items-center gap-2 px-3 py-1.5 text-xs transition-colors cursor-pointer hover:bg-[var(--color-bg-hover)]"
          >
            <span
              className="w-2.5 h-2.5 rounded-full shrink-0"
              style={{ backgroundColor: tag.color || C.accent }}
            />
            <span className="flex-1 text-left truncate" style={{ color: C.textPrimary }}>
              {tag.name}
            </span>
            {assignedIds.has(tag.id) && (
              <Check size={12} style={{ color: C.online }} />
            )}
          </button>
        ))}
      </div>

      {/* New tag input */}
      <div className="border-t px-3 py-2" style={{ borderColor: C.border }}>
        <div className="flex items-center gap-1.5">
          <input
            value={newTagName}
            onChange={(e) => setNewTagName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleCreate();
              if (e.key === "Escape") onClose();
            }}
            placeholder={t("newTagPlaceholder")}
            autoFocus
            aria-label={t("createNewTag")}
            className="flex-1 text-xs px-2 py-1 rounded-sm outline-none min-w-0"
            style={{
              backgroundColor: C.bgSurface,
              color: C.textPrimary,
              border: `1px solid ${C.border}`,
            }}
          />
        </div>
        <div className="flex items-center gap-1.5 mt-1.5">
          {TAG_COLORS.map((c) => (
            <button
              key={c}
              onClick={() => setSelectedColor(c)}
              className="w-4 h-4 rounded-full transition-transform cursor-pointer"
              style={{
                backgroundColor: c,
                outline: selectedColor === c ? `2px solid ${c}` : "none",
                outlineOffset: "1px",
                transform: selectedColor === c ? "scale(1.15)" : "scale(1)",
              }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Phase Section ──────────────────────────────────────────────────────────────

function PhaseSection({
  phase,
  subtasks,
  agents,
  boardId,
  previousPhase,
  onTaskClick,
  repoUrl,
  headByTask,
}: {
  phase: Task;
  subtasks: Task[];
  agents: Agent[];
  boardId: string;
  previousPhase?: Task;
  onTaskClick: (task: Task) => void;
  repoUrl?: string | null;
  /** The newest head run per task, from the ONE shared fetch in
   *  `ProjectDetail` (heads-sichtbar PR 3, bauplan §4: "ein Abruf für die
   *  ganze Liste") — never queried per row. */
  headByTask: Map<string, HeadRun>;
}) {
  const t = useTranslations("tasks");
  const [collapsed, setCollapsed] = useState(phase.status === "done");
  const qc = useQueryClient();

  const startPhaseMutation = useMutation({
    mutationFn: () => api.tasks.update(boardId, phase.id, { status: "in_progress" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tasks", boardId] }),
  });

  const doneCount = subtasks.filter((t) => t.status === "done").length;
  const progress = subtasks.length > 0 ? Math.round((doneCount / subtasks.length) * 100) : 0;
  const previousDone = !previousPhase || previousPhase.status === "done";
  const canStart = (phase.status === "inbox" || phase.status === "review") && previousDone;

  return (
    <div className="mb-2">
      {/* Phase header */}
      <button
        onClick={() => setCollapsed(!collapsed)}
        className="w-full flex items-center gap-2 px-3 py-2 rounded-md transition-colors hover:bg-[var(--color-bg-hover)] group cursor-pointer"
      >
        {collapsed ? (
          <ChevronRight size={14} style={{ color: C.textMuted }} />
        ) : (
          <ChevronDown size={14} style={{ color: C.textMuted }} />
        )}
        <span className="flex-1 text-left text-sm font-medium" style={{ color: C.textPrimary }}>
          {phase.title}
        </span>
        <div className="flex items-center gap-2">
          {/* Branch badge */}
          {phase.branch_name && (
            repoUrl ? (
              <a
                href={`${repoUrl}/tree/${phase.branch_name}`}
                target="_blank"
                rel="noopener noreferrer"
                onClick={(e) => e.stopPropagation()}
                className="flex items-center gap-1 px-1.5 py-0.5 rounded-sm text-[10px] font-mono hover:opacity-80 transition-opacity"
                style={{
                  background: C.accentSubtle,
                  color: C.accent,
                  border: `1px solid ${C.borderAccent}`,
                }}
                title={phase.branch_name}
              >
                <GitBranch size={9} />
                <span className="max-w-[100px] truncate">{phase.branch_name}</span>
              </a>
            ) : (
              <span
                className="flex items-center gap-1 px-1.5 py-0.5 rounded-sm text-[10px] font-mono"
                style={{
                  background: C.accentSubtle,
                  color: C.textMuted,
                  border: `1px solid ${C.borderSubtle}`,
                }}
                title={phase.branch_name}
              >
                <GitBranch size={9} />
                <span className="max-w-[100px] truncate">{phase.branch_name}</span>
              </span>
            )
          )}
          {subtasks.length > 0 && (
            <span className="text-xs" style={{ color: C.textMuted }}>
              {doneCount}/{subtasks.length}
            </span>
          )}
          <Pill color={STATUS_CONFIG[phase.status].color} size="sm">
            {t(STATUS_CONFIG[phase.status].labelKey)}
          </Pill>
        </div>
      </button>

      <AnimatePresence>
        {!collapsed && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="overflow-hidden"
          >
            <div
              className="ml-6 border-l pl-2 space-y-1"
              style={{ borderColor: C.border }}
            >
              {subtasks.length === 0 && (
                <div className="py-2 px-3 text-xs" style={{ color: C.textMuted }}>
                  {t("noSubtasks")}
                </div>
              )}
              {subtasks.map((task) => (
                <TaskRow
                  key={task.id}
                  task={task}
                  agents={agents}
                  boardId={boardId}
                  headRun={headByTask.get(task.id) ?? null}
                  onClick={() => onTaskClick(task)}
                />
              ))}

              {/* Phase start button */}
              {canStart && (
                <button
                  onClick={() => startPhaseMutation.mutate()}
                  disabled={startPhaseMutation.isPending}
                  className="flex items-center gap-1.5 mx-3 my-2 px-3 py-1.5 rounded-md text-xs font-mono font-medium transition-colors cursor-pointer"
                  style={{
                    backgroundColor: C.accentSubtle,
                    color: C.accentHover,
                    border: `1px solid ${C.borderAccent}`,
                  }}
                >
                  <Play size={11} fill="currentColor" />
                  {phase.status === "review"
                    ? t("finishPhaseStartNext")
                    : t("startPhase")}
                </button>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// ── Revision Section ──────────────────────────────────────────────────────────

function RevisionSection({
  revisions,
  agents,
  boardId,
  projectId,
}: {
  revisions: Task[];
  agents: Agent[];
  boardId: string;
  projectId: string;
}) {
  const t = useTranslations("tasks");
  const [showForm, setShowForm] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState("medium");
  const [assignedAgent, setAssignedAgent] = useState("");
  const queryClient = useQueryClient();

  const createRevision = useMutation({
    mutationFn: () =>
      api.tasks.create(boardId, {
        title,
        description: description || undefined,
        priority,
        task_type: "revision",
        project_id: projectId,
        assigned_agent_id: assignedAgent || undefined,
      } as Partial<Task>),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      setShowForm(false);
      setTitle("");
      setDescription("");
      setPriority("medium");
      setAssignedAgent("");
    },
  });

  return (
    <div className="mt-6 border-t pt-4" style={{ borderColor: C.border }}>
      <div className="flex items-center justify-between mb-3">
        <h3 className="label-sys flex items-center gap-2">
          {t("revisions")}
          {revisions.length > 0 && (
            <span
              className="text-[10px] font-mono px-1.5 py-0.5 rounded-sm"
              style={{ backgroundColor: C.bgElevated }}
            >
              {revisions.length}
            </span>
          )}
        </h3>
        <button
          onClick={() => setShowForm(!showForm)}
          className="text-xs transition-colors cursor-pointer"
          style={{ color: C.textMuted }}
        >
          {showForm ? t("cancel") : t("plusNew")}
        </button>
      </div>

      {/* Create form */}
      {showForm && (
        <div
          className="mb-4 p-3 rounded-md space-y-2"
          style={{ backgroundColor: C.bgElevated, border: `1px solid ${C.border}` }}
        >
          <input
            type="text"
            placeholder={t("revisionTitlePlaceholder")}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            aria-label={t("revisionTitle")}
            className="w-full bg-transparent rounded-sm px-2 py-1.5 text-sm focus:outline-none"
            style={{ border: `1px solid ${C.border}`, color: C.textPrimary }}
            autoFocus
          />
          <textarea
            placeholder={t("detailsOptional")}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={2}
            aria-label={t("revisionDescription")}
            className="w-full bg-transparent rounded-sm px-2 py-1.5 text-sm focus:outline-none resize-none"
            style={{ border: `1px solid ${C.border}`, color: C.textPrimary }}
          />
          <div className="flex gap-2 items-center">
            <select
              value={priority}
              onChange={(e) => setPriority(e.target.value)}
              aria-label={t("selectPriority")}
              className="rounded-sm px-2 py-1 text-xs cursor-pointer"
              style={{
                backgroundColor: C.bgDeep,
                border: `1px solid ${C.border}`,
                color: C.textSecondary,
              }}
            >
              <option value="low">{t("priorityLow")}</option>
              <option value="medium">{t("priorityMedium")}</option>
              <option value="high">{t("priorityHigh")}</option>
              <option value="critical">{t("priorityCritical")}</option>
            </select>
            <select
              value={assignedAgent}
              onChange={(e) => setAssignedAgent(e.target.value)}
              aria-label={t("assignAgent")}
              className="rounded-sm px-2 py-1 text-xs flex-1 cursor-pointer"
              style={{
                backgroundColor: C.bgDeep,
                border: `1px solid ${C.border}`,
                color: C.textSecondary,
              }}
            >
              <option value="">{t("assignAgentPlaceholder")}</option>
              {agents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.emoji} {a.name}
                </option>
              ))}
            </select>
            <button
              onClick={() => createRevision.mutate()}
              disabled={!title.trim() || createRevision.isPending}
              className="px-3 py-1 text-xs font-mono font-medium rounded-sm transition-colors cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
              style={{
                backgroundColor: C.accentSubtle,
                color: C.accentHover,
              }}
            >
              {createRevision.isPending ? "..." : t("create")}
            </button>
          </div>
        </div>
      )}

      {/* Revision list */}
      {revisions.length === 0 && !showForm && (
        <p className="text-xs italic" style={{ color: C.textMuted }}>
          {t("noRevisions")}
        </p>
      )}
      <div className="space-y-1">
        {revisions.map((rev) => {
          const agent = agents.find((a) => a.id === rev.assigned_agent_id);
          return (
            <div
              key={rev.id}
              className="flex items-center gap-3 px-3 py-2 rounded-md transition-colors group bg-[var(--color-bg-surface)] hover:bg-[var(--color-bg-hover)]"
              style={{ border: `1px solid ${C.border}` }}
            >
              <TaskStatusDot status={rev.status} />
              <span
                className="text-sm flex-1 truncate"
                style={{ color: C.textPrimary }}
              >
                {rev.title}
              </span>
              {agent && (
                <span className="text-xs inline-flex items-center gap-1" style={{ color: C.textMuted }}>
                  <EntityIcon value={agent.emoji} size={12} />
                  {agent.name}
                </span>
              )}
              <Pill color={STATUS_CONFIG[rev.status].color} size="sm">
                {t(STATUS_CONFIG[rev.status].labelKey)}
              </Pill>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Project Detail ─────────────────────────────────────────────────────────────

// Exported for its own focused test (ProjectDetail.headLine.test.tsx, review
// fix round 5) — the `headByTask.get(task.id)` lookup lines below (phases'
// subtasks AND the standalone list) had no test at all; `TaskRow`'s own test
// only ever supplied `headRun` as a plain prop, never exercising this file's
// own lookup.
export function ProjectDetail({
  project,
  tasks,
  agents,
  boardId,
  tags,
  onTaskClick,
}: {
  project: Project | null;
  tasks: Task[];
  agents: Agent[];
  boardId: string;
  tags: Tag[];
  onTaskClick: (task: Task) => void;
}) {
  const t = useTranslations("tasks");
  const [showTagManager, setShowTagManager] = useState(false);
  // One shared fetch for the whole project view (heads-sichtbar PR 3,
  // bauplan §4: "ein Abruf für die ganze Liste") — every phase's subtasks
  // and the standalone list below read from this one map, never per row.
  const { byTask: headByTask } = useHeadRuns();

  const regularTasks = useMemo(
    () => tasks.filter((t) => t.task_type !== "revision"),
    [tasks]
  );
  const revisionTasks = useMemo(
    () => tasks.filter((t) => t.task_type === "revision"),
    [tasks]
  );

  const phases = useMemo(() => {
    const parentIds = new Set(
      regularTasks.filter((t) => t.parent_task_id).map((t) => t.parent_task_id!)
    );
    return regularTasks.filter((t) => !t.parent_task_id && parentIds.has(t.id));
  }, [regularTasks]);

  const standaloneWithProject = useMemo(() => {
    const phaseIds = new Set(phases.map((p) => p.id));
    return regularTasks.filter((t) => !t.parent_task_id && !phaseIds.has(t.id));
  }, [regularTasks, phases]);

  const subtasksFor = (phaseId: string) =>
    regularTasks.filter((t) => t.parent_task_id === phaseId);

  const totalTasks = regularTasks.filter(
    (t) => t.parent_task_id || phases.length === 0
  ).length;
  const doneTasks = regularTasks.filter(
    (t) =>
      (t.parent_task_id || phases.length === 0) && t.status === "done"
  ).length;
  const progress = totalTasks > 0 ? Math.round((doneTasks / totalTasks) * 100) : 0;

  if (!project) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-center">
          <FolderKanban
            size={32}
            className="mx-auto mb-3 opacity-20"
            style={{ color: C.textMuted }}
          />
          <div className="label-sys">{t("selectProject")}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col min-h-0">
      {/* Project Header */}
      <div
        className="px-6 py-4 border-b shrink-0"
        style={{ borderColor: C.border }}
      >
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2 flex-wrap min-w-0 relative">
            <h2
              className="display text-lg font-semibold"
              style={{ color: C.textPrimary }}
            >
              {project.name}
            </h2>
            {/* GitHub repo badge */}
            {project.github_repo_url && (
              <a
                href={project.github_repo_url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-1 px-2 py-0.5 rounded-sm text-[10px] font-medium shrink-0 hover:opacity-80 transition-opacity"
                style={{
                  background: C.bgSurface,
                  color: C.textMuted,
                  border: `1px solid ${C.borderSubtle}`,
                  fontFamily: "var(--font-mono)",
                }}
                title={project.github_repo_url}
              >
                <svg width="10" height="10" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                  <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/>
                </svg>
                {project.github_repo_name ?? project.github_repo_url.split("/").slice(-2).join("/")}
              </a>
            )}
            {tags.length > 0 && (
              <div className="flex items-center gap-1 flex-wrap">
                {tags.map((tag) => (
                  <TagChip key={tag.id} tag={tag} />
                ))}
              </div>
            )}
            <button
              onClick={() => setShowTagManager(!showTagManager)}
              className="w-5 h-5 rounded-sm flex items-center justify-center transition-colors hover:bg-[var(--color-bg-hover)] cursor-pointer"
              style={{ color: C.textMuted }}
              title={t("manageTags")}
            >
              <Plus size={13} />
            </button>
            {showTagManager && (
              <TagManager
                projectId={project.id}
                assignedTags={tags}
                onClose={() => setShowTagManager(false)}
              />
            )}
          </div>
          <span
            className="text-sm font-mono font-semibold shrink-0"
            style={{
              color: progress === 100 ? C.online : C.accent,
            }}
          >
            {progress}%
          </span>
        </div>

        {/* Progress bar */}
        <div
          className="h-1.5 rounded-sm overflow-hidden"
          style={{ backgroundColor: C.bgElevated }}
        >
          <div
            className="h-full rounded-sm transition-all duration-500"
            style={{
              width: `${progress}%`,
              backgroundColor: progress === 100 ? C.online : C.accent,
            }}
          />
        </div>

        {project.description && (
          <p className="mt-2 text-xs" style={{ color: C.textMuted }}>
            {project.description}
          </p>
        )}
      </div>

      {/* Task list */}
      <div className="flex-1 overflow-y-auto p-4">
        {tasks.length === 0 && (
          <div
            className="text-sm text-center py-8"
            style={{ color: C.textMuted }}
          >
            {t("noTasksInProject")}
          </div>
        )}

        {/* Phases */}
        {phases.map((phase, index) => (
          <PhaseSection
            key={phase.id}
            phase={phase}
            subtasks={subtasksFor(phase.id)}
            agents={agents}
            boardId={boardId}
            previousPhase={index > 0 ? phases[index - 1] : undefined}
            onTaskClick={onTaskClick}
            repoUrl={project.github_repo_url}
            headByTask={headByTask}
          />
        ))}

        {/* Standalone tasks */}
        {standaloneWithProject.length > 0 && (
          <div className="space-y-1">
            {phases.length > 0 && (
              <div className="label-sys px-3 py-1 mb-1 mt-3">
                {t("moreTasks")}
              </div>
            )}
            {standaloneWithProject.map((task) => (
              <TaskRow
                key={task.id}
                task={task}
                agents={agents}
                boardId={boardId}
                headRun={headByTask.get(task.id) ?? null}
                onClick={() => onTaskClick(task)}
              />
            ))}
          </div>
        )}

        {/* Revisions */}
        <RevisionSection
          revisions={revisionTasks}
          agents={agents}
          boardId={boardId}
          projectId={project.id}
        />
      </div>
    </div>
  );
}
