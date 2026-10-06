"use client";

import { useState, useMemo, useEffect, useRef, useCallback, Suspense } from "react";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { ChevronRight, Trash2 } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useAppStore } from "@/lib/store";
import { api } from "@/lib/api";
import AppShell from "@/components/layout/AppShell";
import TaskListColumn from "@/components/tasks/TaskListColumn";
import { TaskDetailBody } from "@/components/task/TaskDetailBody";
import type { Task } from "@/lib/types";
import { C, STATUS_TEXT, alpha } from "@/lib/colors";
import { isTaskTabKey } from "@/lib/taskDetail/tabs";
import { ProjectDetail } from "./ProjectDetail";

// ── Main Page (Redesign 07/2026 — task list first, projects as groups) ─────────
//
// The task is the primary unit: TaskListColumn sits next to the sidebar,
// grouped by status (operational) or project (structural, Ad-hoc first).
// The right pane switches between task detail (TaskDetailBody), the
// project/phase view (ProjectDetail via the group header link) and an
// empty state. Mobile keeps the stack navigation (list → detail).

function TasksPageContent({ onPhoneTaskChange }: { onPhoneTaskChange?: (open: boolean) => void } = {}) {
  const t = useTranslations("tasks");
  const activeBoardId = useAppStore((s) => s.activeBoardId);
  const qc = useQueryClient();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [projectViewId, setProjectViewId] = useState<string | null>(null);
  const [confirmDeleteProject, setConfirmDeleteProject] = useState(false);
  // Mobile (<md) stack navigation: which pane fills the screen. Desktop (≥md)
  // always shows the split. Default "list" = mobile lands on the task list,
  // detail only after a tap (iPhone-Befund Operator).
  const [mobileView, setMobileView] = useState<"list" | "detail">("list");

  // Task + tab live in the URL: /tasks?task=<uuid>&tab=<tab>. This reverses
  // an earlier deliberate choice (the old ?taskId= deep link was stripped on
  // read "so reload/back stays clean") — wave 3a wants reload and shared
  // links to land on the same task and tab. The legacy ?taskId= (LoopDetail-
  // Panel's "View task", VoicePreviewSheet) still works and is rewritten to
  // ?task=. A link to a task that is not on this board shows "Task not found".
  const [tabParam, setTabParam] = useState<string | null>(null);
  const [notFoundTaskId, setNotFoundTaskId] = useState<string | null>(null);
  const deepLinkReadRef = useRef(false);
  const [deepLinkTaskId, setDeepLinkTaskId] = useState<string | null>(null);

  const writeUrl = useCallback(
    (taskId: string | null, tab: string | null) => {
      const params = new URLSearchParams(searchParams.toString());
      params.delete("taskId");
      if (taskId) params.set("task", taskId);
      else params.delete("task");
      if (taskId && tab) params.set("tab", tab);
      else params.delete("tab");
      const qs = params.toString();
      router.replace(qs ? `/tasks?${qs}` : "/tasks", { scroll: false });
    },
    [searchParams, router],
  );

  useEffect(() => {
    if (deepLinkReadRef.current) return;
    deepLinkReadRef.current = true;
    const legacyId = searchParams.get("taskId");
    const id = searchParams.get("task") ?? legacyId;
    if (!id) return;
    const rawTab = searchParams.get("tab");
    // An unknown tab (old links with tab=e2e) falls back to the status
    // default in the body — drop it from the URL too, so it doesn't linger.
    const tab = isTaskTabKey(rawTab) ? rawTab : null;
    setDeepLinkTaskId(id);
    setTabParam(tab);
    if (legacyId || tab !== rawTab) writeUrl(id, tab);
  }, [searchParams, writeUrl]);

  const { data: allTasks = [], isSuccess: tasksLoaded } = useQuery({
    queryKey: ["tasks", activeBoardId],
    queryFn: () => api.tasks.list(activeBoardId!),
    enabled: !!activeBoardId,
    refetchInterval: 15_000,
  });

  // Once the tasks query has settled, try to resolve the deep-linked task.
  // Found → select it (opens TaskDetailBody). Not found → "Task not found"
  // in the detail pane. Only fires once tasksLoaded so an empty first render
  // of `allTasks` (query still in flight) doesn't read as "not found".
  useEffect(() => {
    if (!deepLinkTaskId || !tasksLoaded) return;
    const target = allTasks.find((t) => t.id === deepLinkTaskId);
    if (target) {
      setSelectedTaskId(target.id);
      setNotFoundTaskId(null);
    } else {
      setNotFoundTaskId(deepLinkTaskId);
      setDeepLinkTaskId(null);
    }
    setProjectViewId(null);
    setMobileView("detail");
  }, [deepLinkTaskId, allTasks, tasksLoaded]);

  // TaskListColumn only needs to try expanding a group once real task data is
  // in — handing it the id earlier (while `tasks` is still []) would make it
  // conclude "not found" and clear the id before the effect above ever runs.
  const focusTaskId = tasksLoaded ? deepLinkTaskId : null;

  const { data: agents = [] } = useQuery({
    queryKey: ["agents", activeBoardId],
    queryFn: () => api.agents.list(activeBoardId!),
    enabled: !!activeBoardId,
  });

  const { data: projects = [] } = useQuery({
    queryKey: ["projects", activeBoardId],
    queryFn: () => api.projects.list(activeBoardId!),
    enabled: !!activeBoardId,
  });

  // Tags only matter for the project view header
  const { data: projectTags = [] } = useQuery({
    queryKey: ["project-tags", projectViewId],
    queryFn: () => api.tags.forProject(projectViewId!),
    enabled: !!projectViewId,
  });

  const deleteProjectMutation = useMutation({
    mutationFn: (projectId: string) => api.projects.delete(activeBoardId!, projectId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects", activeBoardId] });
      qc.invalidateQueries({ queryKey: ["tasks", activeBoardId] });
      setProjectViewId(null);
      setConfirmDeleteProject(false);
      setMobileView("list");
    },
  });

  // Selected task always derives fresh from the query cache — a status change
  // in the detail header must not render against a stale snapshot.
  const selectedTask = useMemo(
    () => (selectedTaskId ? (allTasks.find((t) => t.id === selectedTaskId) ?? null) : null),
    [allTasks, selectedTaskId],
  );

  // Tell the shell when the phone shows a task: its context bar replaces the
  // app bar then. (A no-op above md — the app bar is phone-only anyway.)
  const phoneTaskOpen = mobileView === "detail" && !!selectedTask;
  useEffect(() => {
    onPhoneTaskChange?.(phoneTaskOpen);
  }, [phoneTaskOpen, onPhoneTaskChange]);
  useEffect(() => () => onPhoneTaskChange?.(false), [onPhoneTaskChange]);

  const projectView = projectViewId ? (projects.find((p) => p.id === projectViewId) ?? null) : null;
  const projectViewTasks = useMemo(
    () => (projectViewId ? allTasks.filter((t) => t.project_id === projectViewId) : []),
    [allTasks, projectViewId],
  );

  function handleSelectTask(task: Task) {
    setSelectedTaskId(task.id);
    setNotFoundTaskId(null);
    setTabParam(null);
    setProjectViewId(null);
    setMobileView("detail");
    writeUrl(task.id, null);
  }

  // Subtask/parent links inside the detail open in place.
  function handleOpenTaskId(taskId: string) {
    const target = allTasks.find((t) => t.id === taskId);
    if (target) {
      handleSelectTask(target);
      setDeepLinkTaskId(taskId); // expand its group + scroll the row into view
    } else {
      setSelectedTaskId(null);
      setNotFoundTaskId(taskId);
      setMobileView("detail");
      writeUrl(taskId, null);
    }
  }

  function handleTabChange(tab: string) {
    setTabParam(tab);
    writeUrl(selectedTaskId, tab);
  }

  function handleOpenProject(projectId: string) {
    setProjectViewId(projectId);
    setSelectedTaskId(null);
    setNotFoundTaskId(null);
    if (selectedTaskId) writeUrl(null, null);
    setConfirmDeleteProject(false);
    setMobileView("detail");
  }

  function handleCloseDetail() {
    setSelectedTaskId(null);
    setProjectViewId(null);
    setNotFoundTaskId(null);
    setTabParam(null);
    setMobileView("list");
    writeUrl(null, null);
  }

  if (!activeBoardId) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-sm" style={{ color: C.textMuted }}>
          {t("noBoardSelected")}
        </div>
      </div>
    );
  }

  const detailOpen = !!selectedTask || !!projectView || !!notFoundTaskId;

  return (
    // Die Liste liegt seit 23.08.2026 auf einer INSEL statt nackt auf dem
    // Seitengrund (Operator-Entscheid, Variante A).
    //
    // Vorgeschichte: die klebenden Gruppen-Köpfe brauchen einen Grund, damit
    // Zeilen beim Scrollen darunter verschwinden. Auf dem Seitengrund ging das
    // nicht sauber — der trägt den Ambient-Schleier (Verlauf + Korn), und jede
    // Füllung darauf blieb sichtbar: deckend als dunkles Rechteck, transluzent
    // mit Weichzeichner als glattes Rechteck in gekörnter Umgebung. Gemessen:
    // Grund 30,6 mit Körnung 0,64 gegen Band 29,2 mit Körnung ~0. Kein
    // Farbwert löst das, weil nicht der Ton stört, sondern die fehlende Körnung.
    //
    // Auf einer Insel verschwindet das Problem, statt kaschiert zu werden: die
    // Insel ist selbst eine glatte Fläche, der Kopf malt schlicht ihre Farbe,
    // und das Rechteck ist gewollt — mit Rahmen und Radius. Genau so macht es
    // die Sessions-Seite seit jeher.
    // fullHeight shell: the frame fills the column; the list and the detail
    // scroll inside. An open task on the phone runs edge to edge (K8).
    <div
      className={`flex flex-1 min-h-0 md:-m-6 md:h-dvh md:p-2 ${phoneTaskOpen ? "-mx-4 -mb-4" : ""}`}
      data-testid="tasks-frame"
    >
      <div
        className="flex flex-1 min-h-0 min-w-0 md:rounded-xl md:border md:overflow-hidden"
        style={{ background: C.bgSurface, borderColor: C.border }}
        data-testid="tasks-island"
      >
      {/* ── Task list (primary column) ── */}
      <div
        className={`${mobileView === "list" ? "flex" : "hidden"} md:flex flex-1 md:flex-none md:w-[340px] md:border-r min-h-0 min-w-0`}
        style={{ borderColor: C.border }}
      >
        <TaskListColumn
          tasks={allTasks}
          projects={projects}
          agents={agents}
          boardId={activeBoardId}
          selectedTaskId={selectedTaskId}
          onSelectTask={handleSelectTask}
          onOpenProject={handleOpenProject}
          focusTaskId={focusTaskId}
          onFocusHandled={() => setDeepLinkTaskId(null)}
        />
      </div>

      {/* ── Right pane: task detail / project view / empty state ── */}
      <div className={`${mobileView === "detail" ? "flex" : "hidden"} md:flex flex-1 flex-col min-h-0 min-w-0`}>
        {/* Mobile: back to the list — an open task carries "‹ Tasks" in its
            own context bar; the project and not-found views keep this bar. */}
        {detailOpen && !selectedTask && (
          <div
            className="flex items-center gap-3 px-4 py-3 border-b shrink-0 md:hidden"
            style={{ borderColor: C.border }}
          >
            <button
              onClick={handleCloseDetail}
              className="flex items-center gap-2 px-3 py-2 rounded-md text-sm font-medium transition-colors cursor-pointer min-h-[44px]"
              style={{
                backgroundColor: C.bgSurface,
                color: C.textSecondary,
                border: `1px solid ${C.border}`,
              }}
            >
              <ChevronRight size={14} className="rotate-180" style={{ color: C.textMuted }} />
              {t("title")}
            </button>
            {/* The task title lives in the detail header right below — repeating
                it here doubled it on phones. Only project/not-found views, which
                have no header of their own, keep a label in the bar. */}
            {!selectedTask && (
              <span className="text-sm truncate" style={{ color: C.textPrimary }}>
                {projectView?.name ?? (notFoundTaskId ? t("detail.notFoundTitle") : "")}
              </span>
            )}
          </div>
        )}

        {selectedTask ? (
          // Bewusst OHNE eigene Füllung: der Seitengrund trägt den Ambient-
          // Schleier, und eine opake Fläche stanzt dort ein flaches Rechteck
          // heraus (gemessen: 12–14 Grund gegen flache 16 mit bgBase). Die
          // Schwester-Zustände darunter (Projekt-Ansicht, Leerzustand) stehen
          // ebenfalls direkt auf dem Grund — geklebt wird hier nichts, also
          // braucht es auch keine Maskierung.
          <div
            className="flex-1 flex flex-col min-h-0 pt-safe-top"
            style={{ "--detail-bg": C.bgSurface, "--detail-raised": C.bgElevated } as React.CSSProperties}
          >
            <TaskDetailBody
              task={selectedTask}
              agents={agents}
              boardId={activeBoardId}
              onClose={handleCloseDetail}
              tab={tabParam}
              onTabChange={handleTabChange}
              onOpenTask={handleOpenTaskId}
              hideCloseOnMobile
              onBack={handleCloseDetail}
              backLabel={t("title")}
            />
          </div>
        ) : notFoundTaskId ? (
          <div className="flex-1 flex items-center justify-center" data-testid="task-not-found">
            <div className="text-center px-10 py-12 max-w-sm">
              <div className="label-sys label-sys--dim mb-3">
                {t("taskLabel")} · {notFoundTaskId.slice(0, 8)}
              </div>
              <div className="text-sm font-medium mb-1" style={{ color: C.textPrimary }} role="heading" aria-level={2}>
                {t("detail.notFoundTitle")}
              </div>
              <div className="text-xs mb-4" style={{ color: C.textSecondary }}>
                {t("detail.notFoundBody")}
              </div>
              <button
                type="button"
                onClick={handleCloseDetail}
                className="px-3 min-h-[36px] rounded-md text-xs cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
                style={{ color: C.textSecondary, border: `1px solid ${C.borderActive}` }}
              >
                {t("detail.backToList")}
              </button>
            </div>
          </div>
        ) : projectView ? (
          <div className="flex-1 flex flex-col min-h-0">
            {/* Project pane header: back to list context + delete */}
            <div
              className="hidden md:flex items-center gap-2 px-6 py-2 border-b shrink-0"
              style={{ borderColor: C.border }}
            >
              <button
                onClick={handleCloseDetail}
                className="text-[11px] font-mono px-2 py-1 rounded-sm cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
                style={{ color: C.textMuted, border: `1px solid ${C.border}` }}
              >
                ← {t("allTasks")}
              </button>
              <span className="label-sys label-sys--dim">
                {t("projectView")}
              </span>
              <span className="ml-auto flex items-center gap-2">
                {confirmDeleteProject ? (
                  <>
                    <span className="text-[11px]" style={{ color: C.warning }}>
                      {t("deleteProjectConfirm")}
                    </span>
                    <button
                      onClick={() => deleteProjectMutation.mutate(projectView.id)}
                      disabled={deleteProjectMutation.isPending}
                      className="px-2 py-1 rounded-sm text-[11px] font-semibold cursor-pointer"
                      style={{ backgroundColor: alpha(C.error, 0.15), color: STATUS_TEXT.error }}
                    >
                      {deleteProjectMutation.isPending ? "…" : t("deleteProject")}
                    </button>
                    <button
                      onClick={() => setConfirmDeleteProject(false)}
                      className="px-2 py-1 rounded-sm text-[11px] cursor-pointer"
                      style={{ color: C.textMuted }}
                    >
                      {t("cancel")}
                    </button>
                  </>
                ) : (
                  <button
                    onClick={() => setConfirmDeleteProject(true)}
                    aria-label={t("deleteProject")}
                    title={t("deleteProject")}
                    className="p-1.5 rounded-sm cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
                    style={{ color: C.textMuted }}
                  >
                    <Trash2 size={13} />
                  </button>
                )}
              </span>
            </div>
            <ProjectDetail
              project={projectView}
              tasks={projectViewTasks}
              agents={agents}
              boardId={activeBoardId}
              tags={projectTags}
              onTaskClick={handleSelectTask}
            />
          </div>
        ) : (
          <div className="flex-1 hidden md:flex items-center justify-center">
            <div className="text-center px-10 py-12">
              <div className="label-sys label-sys--dim mb-3">{t("taskDetail")}</div>
              <div className="text-sm" style={{ color: C.textSecondary }}>
                {t("selectTaskFromList")}
              </div>
              <div className="text-[11px] font-mono mt-2" style={{ color: C.textDim }}>
                {t("groupByProjectHint")}
              </div>
            </div>
          </div>
        )}
      </div>
      </div>
    </div>
  );
}

export default function TasksPage() {
  // Phone + an open task: the detail's own context bar (‹ Tasks · ⋯) takes
  // the place of the app bar (DESIGN.md K12), and its action bar (Back ·
  // main action · Reply · More, mobile nav V2) takes the place of the tab
  // bar — a pushed screen, like a chat. Full height so the detail scrolls in
  // its own body and both bars stay put.
  const [taskOnPhone, setTaskOnPhone] = useState(false);
  return (
    <AppShell fullHeight mobileChromeless={taskOnPhone}>
      <Suspense fallback={null}>
        <TasksPageContent onPhoneTaskChange={setTaskOnPhone} />
      </Suspense>
    </AppShell>
  );
}
