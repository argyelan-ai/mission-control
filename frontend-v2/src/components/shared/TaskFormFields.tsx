"use client";

/**
 * TaskFormFields — controlled form body for creating/templating tasks.
 *
 * Extracted from CreateTaskModal so the same UX can be embedded inside
 * the Schedule v2 JobModal ("Task Template" section). The component
 * renders ONLY the form body (no modal shell, no submit button).
 *
 * State flows top-down: parent owns the `TaskFormPayload`, this
 * component fires `onChange(next)` for every edit. The Schnell/
 * Strukturiert mode + Operator-Intake collapsed/expanded state are
 * still kept inside this component (they're persistent UI prefs, not
 * part of the task payload) — unless the parent passes a `mode` prop.
 */

import { useState, useMemo, useEffect, useCallback, useId } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Globe, KeyRound, MessageSquare, Calendar,
  Bug, Sparkles, Search as SearchIcon, Zap, Settings2,
  FolderKanban, Users, ChevronDown, ChevronRight, ClipboardList,
  CircleAlert, Wand2, Paperclip, X, MousePointerClick, UserCheck, BellRing, FastForward } from "lucide-react";
import { useQueryClient, useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { formatBytes, REFERENCE_FILE_ACCEPT } from "@/lib/utils";
import type { Agent, Project, Repo } from "@/lib/types";
import { ProjectCombobox } from "./ProjectCombobox";
import { PlannerSlider } from "./PlannerSlider";
import { GitInfoBox } from "./GitInfoBox";
import { UrlListInput } from "./UrlListInput";
import { C as MC } from "@/components/homepage/colors";
import { alpha } from "@/lib/colors";

// ── Design tokens — sourced from the shared MC palette (single source, no purple)
const C = {
  deep: MC.bgDeep,
  base: MC.bgBase,
  elevated: MC.bgElevated,
  border: MC.border,
  borderSubtle: MC.borderSubtle,
  accent: MC.accent,
  online: MC.online,
  warning: MC.warning,
  error: MC.error,
  info: MC.info,
  textPrimary: MC.textPrimary,
  textSecondary: MC.textSecondary,
  textMuted: MC.textMuted,
};

// ── Static option lists ──────────────────────────────────────────────
const PRIORITY_OPTIONS = [
  { value: "low", label: "L", color: C.textMuted },
  { value: "medium", label: "M", color: C.accent },
  { value: "high", label: "H", color: C.warning },
  { value: "critical", label: "!", color: C.error },
];

const TASK_TYPE_OPTIONS = [
  { value: "story", labelKey: "typeFeature" },
  { value: "bug", labelKey: "typeBug" },
  { value: "revision", labelKey: "typeRevision" },
  { value: "chore", labelKey: "typeChore" },
];

const APPROVAL_OPTIONS = [
  { value: "", labelKey: "approvalAuto" },
  { value: "never", labelKey: "approvalNever" },
  { value: "on_plan", labelKey: "approvalOnPlan" },
  { value: "on_execution", labelKey: "approvalOnExecution" },
  { value: "on_publish", labelKey: "approvalOnPublish" },
  { value: "on_sensitive_action", labelKey: "approvalOnRisk" },
  { value: "always", labelKey: "approvalAlways" },
];

const REQUEST_KIND_OPTIONS = [
  { value: "", labelKey: "rkAuto" },
  { value: "code_change", labelKey: "rkCodeChange" },
  { value: "content_create", labelKey: "rkContentCreate" },
  { value: "research", labelKey: "rkResearch" },
  { value: "browser_task", labelKey: "rkBrowser" },
  { value: "credential_task", labelKey: "rkCredential" },
  { value: "mixed", labelKey: "rkMixed" },
];

const AUTONOMY_OPTIONS = [
  { value: "", labelKey: "autoUnset" },
  { value: "advise_only", labelKey: "autoAdvise" },
  { value: "draft_only", labelKey: "autoDraft" },
  { value: "execute_low_risk", labelKey: "autoLowRisk" },
  { value: "execute_with_approval_on_risk", labelKey: "autoApprovalOnRisk" },
  { value: "manual_dispatch_required", labelKey: "autoManual" },
];

// ── Templates (Quick-Start Chips) ────────────────────────────────────
type TemplatePrefill = {
  taskType: string;
  plannerMode?: "auto" | "with_planner" | "direct";
  requestKind?: string;
  autonomyLevel?: string;
  /** i18n keys under tasks.form */
  descriptionPlaceholder?: string;
  acceptancePlaceholder?: string;
};

const TEMPLATES: Record<string, {
  label: string;
  icon: typeof Bug;
  color: string;
  prefill: TemplatePrefill;
}> = {
  bug: {
    label: "Bug Fix",
    icon: Bug,
    color: C.error,
    prefill: {
      taskType: "bug",
      plannerMode: "direct",
      requestKind: "code_change",
      descriptionPlaceholder: "tplBugDesc",
      acceptancePlaceholder: "tplBugAccept",
    },
  },
  feature: {
    label: "Feature",
    icon: Sparkles,
    color: C.accent,
    prefill: {
      taskType: "story",
      plannerMode: "auto",
      requestKind: "code_change",
      descriptionPlaceholder: "tplFeatureDesc",
      acceptancePlaceholder: "tplFeatureAccept",
    },
  },
  research: {
    label: "Research",
    icon: SearchIcon,
    color: C.info,
    prefill: {
      taskType: "chore",
      plannerMode: "direct",
      requestKind: "research",
      autonomyLevel: "draft_only",
      descriptionPlaceholder: "tplResearchDesc",
      acceptancePlaceholder: "tplResearchAccept",
    },
  },
};

const INTAKE_LOCALSTORAGE_KEY = "mc.intake.expanded";
const MODE_LOCALSTORAGE_KEY = "mc.task.mode";

export type TaskMode = "schnell" | "strukturiert";

// ── Reference files (ADR-053) ────────────────────────────────────────
// Staged locally (NOT part of TaskFormPayload — they can't be JSON'd into
// the task-create body) and reported upward so the parent can upload them
// once the task itself exists.
export interface StagedReferenceFile {
  id: string;
  file: File;
}

// ── Payload type — exported so parents can type their state ──────────

export interface TaskFormPayload {
  // Base
  title: string;
  description: string;
  priority: string;
  selectedAgentId: string | null;

  // Project context
  projectId: string | null;
  phaseId: string | null;
  deliverableId: string | null;
  branchName: string;
  // Deprecated (ADR-052) — kept for backend/API compat, no longer set via UI.
  // Repo selection now flows entirely through `repoId` (Repo Registry).
  useSeparateRepo: boolean;
  // Registry-Repo für Ad-hoc-Tasks (ADR-052). Bei Projekt-Tasks bleibt dies
  // null — das Repo kommt dann vom Projekt.
  repoId: string | null;

  // Structured details
  taskType: string;
  plannerMode: "auto" | "with_planner" | "direct";
  acceptanceCriteria: string;
  scopeOut: string;
  dueAt: string;
  riskNotes: string;
  referenceUrls: string[];
  approvalPolicy: string;
  needsBrowser: boolean;
  e2eTestRequired: boolean;
  humanReviewRequired: boolean;
  // Skips the review gate entirely — task goes inbox→in_progress→done.
  // Mutually exclusive with humanReviewRequired (both off is valid).
  skipReview: boolean;
  blockerToOperator: boolean;
  requiresAuth: boolean;
  credentialMode: "vault" | "inline";
  credentialId: string | null;
  inlineCredentials: string;
  reportBack: boolean;
  reportChannel: string;
  reportFormats: string[];

  // Operator-Intake
  requestKind: string;
  autonomyLevel: string;
  desiredOutput: string;
  referenceNotes: string;
  publishAllowed: boolean | null;

  // Template tracking (UI only — not sent to backend, but preserved
  // so the chip stays highlighted and placeholder text persists)
  activeTemplate: keyof typeof TEMPLATES | null;
}

export const EMPTY_TASK_FORM_PAYLOAD: TaskFormPayload = {
  title: "",
  description: "",
  priority: "medium",
  selectedAgentId: null,
  projectId: null,
  phaseId: null,
  deliverableId: null,
  branchName: "",
  // Ad-hoc default is "kein eigenes Repo" (Mark, 04.07.) — ein separates Repo
  // ist Opt-in, nicht der Default für schnelle Tasks ohne Projekt-Verknüpfung.
  useSeparateRepo: false,
  repoId: null,
  taskType: "story",
  plannerMode: "auto",
  acceptanceCriteria: "",
  scopeOut: "",
  dueAt: "",
  riskNotes: "",
  referenceUrls: [],
  approvalPolicy: "",
  needsBrowser: false,
  e2eTestRequired: false,
  // Default lives here as `false` — CreateTaskModal opts new, manually
  // created tasks into `true` (Mark, 05.07.); this shared base stays
  // non-breaking for other consumers (e.g. JobModal-scheduled tasks).
  humanReviewRequired: false,
  // Skip the review gate (straight to done). Off by default; typical for
  // scheduled/report tasks. Mutually exclusive with humanReviewRequired.
  skipReview: false,
  // Opt-in per task: when true, this task's blockers skip Boss triage and
  // come straight to the operator (Mark). Default off — normal lead triage.
  blockerToOperator: false,
  requiresAuth: false,
  credentialMode: "vault",
  credentialId: null,
  inlineCredentials: "",
  reportBack: false,
  reportChannel: "discord",
  reportFormats: [],
  requestKind: "",
  autonomyLevel: "",
  desiredOutput: "",
  referenceNotes: "",
  publishAllowed: null,
  activeTemplate: null,
};

// ── Helpers ──────────────────────────────────────────────────────────

function containerWorkspacePath(hostPath: string | null, runtime: string | null): string {
  if (!hostPath) return "/workspace";
  if (runtime !== "cli-bridge") return hostPath;
  const mcMatch = hostPath.match(/^(?:\/[^/]+)+?\/\.mc\/workspaces\/[^/]+(\/.*)?$/);
  if (mcMatch) {
    const suffix = mcMatch[1] ?? "";
    return safeJoinWorkspace(suffix);
  }
  const legacy = hostPath.match(/^(?:\/[^/]+)+?\/\.openclaw\/workspace-[^/]+(\/.*)?$/);
  if (legacy) {
    const suffix = legacy[1] ?? "";
    return safeJoinWorkspace(suffix);
  }
  return hostPath;
}

function safeJoinWorkspace(suffix: string): string {
  if (!suffix) return "/workspace";
  const parts: string[] = [];
  for (const part of suffix.split("/")) {
    if (part === "" || part === ".") continue;
    if (part === "..") {
      if (parts.length === 0) return "/workspace";
      parts.pop();
    } else {
      parts.push(part);
    }
  }
  return parts.length > 0 ? `/workspace/${parts.join("/")}` : "/workspace";
}

function slugify(input: string): string {
  return input
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 40);
}

// ── Sub-component: AgentCard ─────────────────────────────────────────

function AgentCard({
  agent, selected, onSelect,
}: { agent: Agent; selected: boolean; onSelect: () => void }) {
  const statusColor =
    agent.run_state === "running" || agent.run_state === "recovering" ? C.warning :
    agent.run_state === "aborted" || agent.run_state === "blocked" ? C.error :
    agent.status === "online" || agent.status === "idle" ? C.online :
    C.textMuted;

  const role = agent.role ?? "Agent";
  const stateLabel = agent.run_state === "idle" ? "bereit" :
                     agent.run_state === "running" ? "arbeitet" :
                     agent.run_state === "blocked" ? "blockiert" :
                     agent.run_state;

  return (
    <button
      type="button"
      onClick={onSelect}
      className="flex flex-col gap-1 p-2.5 rounded-lg text-left cursor-pointer transition-all relative"
      style={{
        // System A: „selected" trägt über Fläche + Rahmen. Kein Glow mehr — ein
        // weisser Halo wäre auf Schwarz das lauteste Element der Seite.
        background: selected ? `${alpha(C.accent, 0.06)}` : C.deep,
        border: `1px solid ${selected ? `${alpha(C.accent, 0.4)}` : C.border}`,
        boxShadow: selected ? `0 0 0 1px ${alpha(C.accent, 0.2)}` : "none",
      }}
    >
      <div className="flex items-center gap-1.5">
        <span
          className="w-1.5 h-1.5 rounded-full shrink-0"
          style={{ background: statusColor, boxShadow: `0 0 6px ${alpha(statusColor, 0.67)}` }}
        />
        <span
          className="text-[12px] font-semibold truncate"
          // Textfarbe bleibt konstant — der Zustand steht in Fläche/Rahmen.
          style={{ color: C.textPrimary }}
        >
          {agent.name}
        </span>
      </div>
      <div className="flex items-baseline justify-between gap-1 min-w-0">
        <span
          className="text-[10px] truncate"
          style={{ color: C.textSecondary }}
        >
          {role}
        </span>
        <span
          className="text-[9px] shrink-0"
          style={{ color: C.textMuted }}
        >
          {stateLabel}
        </span>
      </div>
    </button>
  );
}

// ── Main component ───────────────────────────────────────────────────

export interface TaskFormFieldsProps {
  value: TaskFormPayload;
  onChange: (value: TaskFormPayload) => void;
  activeBoardId: string | null;
  agents: Agent[] | undefined;
  /** Optional override — when provided, parent controls the mode toggle */
  mode?: TaskMode;
  onModeChange?: (mode: TaskMode) => void;
  /** Whether this form is rendered inside an open container (for query enable) */
  open?: boolean;
  /** Disable all inputs (parent submitting) */
  disabled?: boolean;
  /** Wrapping ref/className passthrough */
  className?: string;
  /** Layout variant: "stacked" (default, JobModal) or "two-pane" (CreateTaskModal redesign) */
  layout?: "stacked" | "two-pane";
  /** Refs so parents can implement focus + auto-resize like CreateTaskModal does */
  titleRef?: React.RefObject<HTMLInputElement | null>;
  descriptionRef?: React.RefObject<HTMLTextAreaElement | null>;
  /** Cmd+Enter handler from parent (submit) */
  onSubmitShortcut?: () => void;
  /** Esc handler from parent (close) */
  onEscape?: () => void;
  /** Show the "Reference files" section (ADR-053). Default off — the
   *  Schedule JobModal's Task-Vorlage doesn't support attachments yet. */
  enableReferenceFiles?: boolean;
  /** Fired whenever the staged reference files or their shared note change.
   *  The parent mirrors this into its own state and uploads the files once
   *  the task has been created (they need a task_id, which doesn't exist yet). */
  onStagedReferenceFilesChange?: (files: StagedReferenceFile[], note: string) => void;
}

export function TaskFormFields({
  value,
  onChange,
  activeBoardId,
  agents,
  mode: modeProp,
  onModeChange,
  open = true,
  disabled = false,
  className,
  layout = "stacked",
  titleRef,
  descriptionRef,
  onSubmitShortcut,
  onEscape,
  enableReferenceFiles = false,
  onStagedReferenceFilesChange,
}: TaskFormFieldsProps) {
  const qc = useQueryClient();
  const t = useTranslations("tasks.form");
  const fieldId = useId();

  // ── Reference files (ADR-053) — local-only, reported upward via callback ──
  const [stagedReferenceFiles, setStagedReferenceFiles] = useState<StagedReferenceFile[]>([]);
  const [referenceNote, setReferenceNote] = useState("");

  // Note: these read `stagedReferenceFiles`/`referenceNote` from the closure
  // (not a setState functional updater) and call the parent callback as a
  // plain synchronous side effect of the event handler — calling a *different*
  // component's setState from inside a setState updater is timing-fragile
  // (the updater can run outside the normal commit, deferring the parent
  // update unpredictably).
  const handleReferenceFilesPicked = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(e.target.files ?? []);
    e.target.value = "";
    if (picked.length === 0) return;
    const next = [
      ...stagedReferenceFiles,
      ...picked.map((file) => ({
        id: `${file.name}-${file.size}-${Date.now()}-${Math.random().toString(36).slice(2)}`,
        file,
      })),
    ];
    setStagedReferenceFiles(next);
    onStagedReferenceFilesChange?.(next, referenceNote);
  }, [stagedReferenceFiles, onStagedReferenceFilesChange, referenceNote]);

  const removeStagedReferenceFile = useCallback((id: string) => {
    const next = stagedReferenceFiles.filter((f) => f.id !== id);
    setStagedReferenceFiles(next);
    onStagedReferenceFilesChange?.(next, referenceNote);
  }, [stagedReferenceFiles, onStagedReferenceFilesChange, referenceNote]);

  const updateReferenceNote = useCallback((note: string) => {
    setReferenceNote(note);
    onStagedReferenceFilesChange?.(stagedReferenceFiles, note);
  }, [stagedReferenceFiles, onStagedReferenceFilesChange]);

  // Local mode toggle when parent doesn't control it
  const [localMode, setLocalMode] = useState<TaskMode>("schnell");
  useEffect(() => {
    if (modeProp !== undefined) return; // parent-controlled
    try {
      const saved = localStorage.getItem(MODE_LOCALSTORAGE_KEY);
      if (saved === "strukturiert") setLocalMode("strukturiert");
    } catch { /* ignore */ }
  }, [modeProp]);

  const mode = modeProp ?? localMode;
  const toggleMode = useCallback((next: TaskMode) => {
    if (onModeChange) {
      onModeChange(next);
    } else {
      setLocalMode(next);
      try { localStorage.setItem(MODE_LOCALSTORAGE_KEY, next); } catch { /* ignore */ }
    }
  }, [onModeChange]);

  // Operator-Intake collapsed state (UI-only, persistent)
  const [intakeExpanded, setIntakeExpanded] = useState(false);
  useEffect(() => {
    try {
      const saved = localStorage.getItem(INTAKE_LOCALSTORAGE_KEY);
      if (saved === "true") setIntakeExpanded(true);
    } catch { /* ignore */ }
  }, []);
  const toggleIntake = useCallback(() => {
    setIntakeExpanded((v) => {
      const next = !v;
      try { localStorage.setItem(INTAKE_LOCALSTORAGE_KEY, String(next)); } catch { /* ignore */ }
      return next;
    });
  }, []);

  const [initLoading, setInitLoading] = useState(false);
  const [advancedExpanded, setAdvancedExpanded] = useState(false);

  // Helper to patch the payload
  const patch = useCallback((p: Partial<TaskFormPayload>) => {
    onChange({ ...value, ...p });
  }, [value, onChange]);

  // ── Data queries ──
  const { data: projects } = useQuery({
    queryKey: ["projects", activeBoardId],
    queryFn: () => api.projects.list(activeBoardId!),
    enabled: !!activeBoardId && open,
  });
  const { data: phases } = useQuery({
    queryKey: ["phases", value.projectId],
    queryFn: () => api.projects.phases(value.projectId!),
    enabled: !!value.projectId,
  });
  const { data: gitInfo, isLoading: gitInfoLoading, refetch: refetchGitInfo } = useQuery({
    queryKey: ["projectGitInfo", activeBoardId, value.projectId],
    queryFn: () => api.projects.gitInfo(activeBoardId!, value.projectId!),
    enabled: !!activeBoardId && !!value.projectId,
  });
  const { data: deliverables } = useQuery({
    queryKey: ["projectDeliverables", activeBoardId, value.projectId],
    queryFn: () => api.projects.deliverables(activeBoardId!, value.projectId!),
    enabled: !!activeBoardId && !!value.projectId,
  });
  const { data: vaultCredentials } = useQuery({
    queryKey: ["credentials"],
    queryFn: () => api.credentials.list(),
    enabled: open,
  });
  const { data: repos } = useQuery({
    queryKey: ["repos"],
    queryFn: () => api.repos.list(),
    enabled: open,
  });

  // ── Derived ──
  const availableAgents = useMemo(
    () => (agents ?? []).filter((a) => {
      if (a.status === "archived") return false;
      if (a.board_id == null) return true;
      if (a.board_id === activeBoardId) return true;
      return false;
    }),
    [agents, activeBoardId]
  );
  const selectedAgent = useMemo(
    () => availableAgents.find((a) => a.id === value.selectedAgentId) ?? null,
    [availableAgents, value.selectedAgentId]
  );
  const selectedProject = useMemo(
    () => (projects ?? []).find((p) => p.id === value.projectId) ?? null,
    [projects, value.projectId]
  );

  const autoSlug = useMemo(() => slugify(value.title), [value.title]);

  const workspacePreview = useMemo(() => {
    if (!selectedAgent) return null;
    const base = containerWorkspacePath(selectedAgent.workspace_path, selectedAgent.agent_runtime);
    if (selectedProject && selectedProject.name) {
      const projectSlug = slugify(selectedProject.name);
      const taskSlug = autoSlug || "neue-aufgabe";
      return `${base}/projects/${projectSlug}/.worktrees/${taskSlug}/`;
    }
    if (autoSlug) {
      return `${base}/${autoSlug}/ (ad-hoc)`;
    }
    return `${base}/`;
  }, [selectedAgent, selectedProject, autoSlug]);

  // ── Template apply ──
  const applyTemplate = useCallback((key: keyof typeof TEMPLATES) => {
    const { prefill } = TEMPLATES[key];
    const next: TaskFormPayload = {
      ...value,
      // Reset template-controlled fields first
      taskType: prefill.taskType,
      plannerMode: prefill.plannerMode ?? "auto",
      requestKind: prefill.requestKind ?? "",
      autonomyLevel: prefill.autonomyLevel ?? "",
      activeTemplate: key,
    };
    onChange(next);
    if (prefill.requestKind || prefill.autonomyLevel) setIntakeExpanded(true);
  }, [value, onChange]);

  const handleCreateProject = async (name: string, projectType: string): Promise<Project> => {
    if (!activeBoardId) throw new Error("No board");
    const project = await api.projects.create(activeBoardId, { name, project_type: projectType } as Partial<Project>);
    qc.invalidateQueries({ queryKey: ["projects", activeBoardId] });
    return project;
  };

  const handleInitRepo = async () => {
    if (!activeBoardId || !value.projectId || initLoading) return;
    setInitLoading(true);
    try {
      await api.projects.initRepo(activeBoardId, value.projectId);
      refetchGitInfo();
    } finally {
      setInitLoading(false);
    }
  };

  // ADR-052: single canonical repo-creation path from the task mask —
  // creates + registers a brand-new private GitHub repo.
  const handleCreateRepo = useCallback(async (name: string): Promise<Repo> => {
    try {
      const repo = await api.repos.createNew(name);
      qc.invalidateQueries({ queryKey: ["repos"] });
      return repo;
    } catch (err) {
      const msg = err instanceof Error && err.message ? err.message : "Repo-Erstellung fehlgeschlagen";
      notify.error(msg);
      throw err;
    }
  }, [qc]);

  // Link an existing registry repo to the currently selected project.
  const handleLinkRepo = useCallback(async (repoId: string) => {
    if (!value.projectId) return;
    try {
      await api.repos.linkProject(repoId, value.projectId);
      qc.invalidateQueries({ queryKey: ["repos"] });
      refetchGitInfo();
      notify.success(t("repoLinked"));
    } catch (err) {
      const msg = err instanceof Error && err.message ? err.message : t("repoLinkFailed");
      notify.error(msg);
      throw err;
    }
  }, [qc, value.projectId, refetchGitInfo]);

  const toggleReportFormat = (fmt: string) => {
    patch({
      reportFormats: value.reportFormats.includes(fmt)
        ? value.reportFormats.filter((f) => f !== fmt)
        : [...value.reportFormats, fmt],
    });
  };

  const currentTemplate = value.activeTemplate ? TEMPLATES[value.activeTemplate] : null;
  const descriptionPlaceholder = t(currentTemplate?.prefill.descriptionPlaceholder ?? "descriptionPlaceholder");
  const acceptancePlaceholder = t(currentTemplate?.prefill.acceptancePlaceholder ?? "acceptancePlaceholder");

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") onEscape?.();
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) onSubmitShortcut?.();
  };

  // ── TWO-PANE LAYOUT (CreateTaskModal redesign) ──────────────────────
  // Hero (title + description) on the left, compact metadata rail on the
  // right; rarely-used controls live behind an "Erweitert" disclosure.
  // Shares all state/queries/handlers with the stacked layout below.
  if (layout === "two-pane") {
    const selCls = "w-full text-[11px] px-2.5 py-2 rounded-lg outline-none cursor-pointer transition-colors";
    const selStyle = (active: boolean): React.CSSProperties => ({
      background: C.deep,
      border: `1px solid ${active ? `${alpha(C.accent, 0.33)}` : C.border}`,
      color: active ? C.textPrimary : C.textMuted,
    });
    const pill = (active: boolean, color: string): React.CSSProperties => ({
      backgroundColor: active ? `${alpha(color, 0.13)}` : "transparent",
      color: active ? color : C.textMuted,
      border: `1px solid ${active ? `${alpha(color, 0.33)}` : C.border}`,
    });
    // Panel grammar (13.09.): sentence-case section title, no eyebrow, no rule.
    const sectionHead = (label: string) => (
      <h3 className="text-[13px] font-medium" style={{ color: C.textSecondary }}>{label}</h3>
    );

    return (
      <div className={className} style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
        {/* Templates (full width) */}
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[10px] shrink-0" style={{ color: C.textMuted }}>
            <Wand2 size={10} className="inline mr-1 mb-0.5" />{t("templateLabel")}
          </span>
          {(Object.keys(TEMPLATES) as Array<keyof typeof TEMPLATES>).map((key) => {
            const t = TEMPLATES[key]; const active = value.activeTemplate === key; const Icon = t.icon;
            return (
              <button key={key} type="button"
                onClick={() => (active ? patch({ activeTemplate: null }) : applyTemplate(key))}
                className="flex items-center gap-1 px-2.5 py-1 text-[10px] font-mono font-medium rounded-md transition-all cursor-pointer"
                style={{ color: active ? t.color : C.textMuted, background: active ? `${alpha(t.color, 0.08)}` : "transparent", border: `1px solid ${active ? `${alpha(t.color, 0.33)}` : C.border}` }}>
                <Icon size={10} />{t.label}
              </button>
            );
          })}
          {value.activeTemplate && (
            <button type="button" onClick={() => patch({ activeTemplate: null })} className="text-[10px] ml-1 cursor-pointer hover:underline" style={{ color: C.textMuted }}>zuruecksetzen</button>
          )}
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_300px] gap-x-7 gap-y-6">
          {/* ── MAIN: hero ── */}
          <div className="flex flex-col gap-5 min-w-0">
            <div className="flex flex-col gap-1.5">
              <label htmlFor={`${fieldId}-title`} className="text-[11px] font-medium" style={{ color: C.textMuted }}>{t("title")} <span style={{ color: C.error }}>*</span></label>
              <input id={`${fieldId}-title`} ref={titleRef} type="text" required value={value.title}
                onChange={(e) => patch({ title: e.target.value })} onKeyDown={handleKeyDown}
                placeholder={t("titlePlaceholder")}
                className="w-full text-[15px] outline-none px-3.5 py-3 rounded-xl transition-all"
                style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }}
                onFocus={(e) => { e.target.style.borderColor = `${alpha(C.accent, 0.4)}`; e.target.style.boxShadow = `0 0 0 3px ${alpha(C.accent, 0.1)}`; }}
                onBlur={(e) => { e.target.style.borderColor = C.border; e.target.style.boxShadow = "none"; }}
                disabled={disabled} />
            </div>
            <div className="flex flex-col gap-1.5 flex-1">
              <label htmlFor={`${fieldId}-description`} className="text-[11px] font-medium" style={{ color: C.textMuted }}>{t("description")} <span style={{ color: C.textMuted }}>{t("optional")}</span></label>
              <textarea id={`${fieldId}-description`} ref={descriptionRef} value={value.description}
                onChange={(e) => patch({ description: e.target.value })}
                onKeyDown={handleKeyDown} placeholder={descriptionPlaceholder}
                className="w-full flex-1 text-sm outline-none px-3.5 py-3 rounded-xl resize-none transition-all"
                style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep, minHeight: "200px" }}
                onFocus={(e) => { e.target.style.borderColor = `${alpha(C.accent, 0.4)}`; e.target.style.boxShadow = `0 0 0 3px ${alpha(C.accent, 0.1)}`; }}
                onBlur={(e) => { e.target.style.borderColor = C.border; e.target.style.boxShadow = "none"; }}
                disabled={disabled} />
            </div>
            <AnimatePresence>
              {value.plannerMode === "with_planner" && (
                <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} transition={{ duration: 0.2 }} className="flex flex-col gap-3 overflow-hidden">
                  <div className="flex flex-col gap-1.5">
                    <label htmlFor={`${fieldId}-acceptance`} className="text-[11px] font-medium" style={{ color: C.textMuted }}>{t("acceptance")}</label>
                    <textarea id={`${fieldId}-acceptance`} aria-label={t("acceptance")} value={value.acceptanceCriteria} onChange={(e) => patch({ acceptanceCriteria: e.target.value })}
                      placeholder={acceptancePlaceholder} rows={2}
                      className="w-full text-[12px] outline-none px-3 py-2 rounded-xl resize-none" style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }} />
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <label htmlFor={`${fieldId}-scope`} className="text-[11px] font-medium" style={{ color: C.textMuted }}>{t("scopeOut")}</label>
                    <textarea id={`${fieldId}-scope`} aria-label={t("scopeOut")} value={value.scopeOut} onChange={(e) => patch({ scopeOut: e.target.value })}
                      placeholder={t("scopeOutPlaceholder")} rows={2}
                      className="w-full text-[12px] outline-none px-3 py-2 rounded-xl resize-none" style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }} />
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>

          {/* ── RAIL: metadata ── */}
          <div className="flex flex-col gap-5 min-w-0 lg:pl-7 lg:border-l" style={{ borderColor: C.borderSubtle }}>
            {/* Projekt & Repo — direkt nach Titel/Beschreibung, vor den sekundären
                Zuweisungs-/Ausführungs-Optionen (Mark, 04.07.: Repo-Wahl ist wichtig
                genug, um nicht unter Agent/Priorität zu verschwinden). */}
            <div className="flex flex-col gap-3">
              {sectionHead(t("sectionProject"))}
              <ProjectCombobox projects={projects ?? []} value={value.projectId}
                onChange={(id) => patch({ projectId: id, phaseId: null, deliverableId: null, branchName: "", repoId: null })}
                onCreateProject={handleCreateProject} accent={C.accent} textPrimary={C.textPrimary} textMuted={C.textMuted} textSecondary={C.textSecondary} border={C.border} deep={C.deep} />
              {value.projectId && phases && phases.length > 0 && (
                <div className="flex flex-col gap-1.5">
                  <label htmlFor={`${fieldId}-phase`} className="text-[10px]" style={{ color: C.textMuted }}>Phase</label>
                  <select id={`${fieldId}-phase`} aria-label={t("phase")} value={value.phaseId ?? ""} onChange={(e) => patch({ phaseId: e.target.value || null })} className={selCls} style={selStyle(!!value.phaseId)}>
                    <option value="">{t("noPhase")}</option>
                    {phases.filter((p) => p.status === "active" || p.status === "pending").map((p) => (<option key={p.id} value={p.id}>{p.status === "active" ? "● " : "○ "}{p.title}</option>))}
                  </select>
                </div>
              )}
              {value.projectId && deliverables && deliverables.length > 0 && (
                <div className="flex flex-col gap-1.5">
                  <label htmlFor={`${fieldId}-deliverable`} className="text-[10px]" style={{ color: C.textMuted }}>{t("basedOn")}</label>
                  <select id={`${fieldId}-deliverable`} aria-label={t("basedOnAria")} value={value.deliverableId ?? ""} onChange={(e) => patch({ deliverableId: e.target.value || null })} className={selCls} style={selStyle(!!value.deliverableId)}>
                    <option value="">{t("noDeliverable")}</option>
                    {deliverables.map((d) => (<option key={d.id} value={d.id}>{d.title} ({d.deliverable_type})</option>))}
                  </select>
                </div>
              )}
              {value.projectId ? (
                <GitInfoBox gitInfo={gitInfo} isLoading={gitInfoLoading} autoSlug={autoSlug} branchName={value.branchName} onBranchNameChange={(name) => patch({ branchName: name })} onInitRepo={handleInitRepo} initLoading={initLoading} repos={repos ?? []} onLinkRepo={handleLinkRepo} accent={C.accent} textPrimary={C.textPrimary} textMuted={C.textMuted} textSecondary={C.textSecondary} border={C.border} deep={C.deep} warning={C.warning} online={C.online} />
              ) : (
                <GitInfoBox gitInfo={null} isLoading={false} autoSlug={autoSlug} branchName={value.branchName} onBranchNameChange={(name) => patch({ branchName: name })} onInitRepo={() => {}} initLoading={false} adHocMode repos={repos ?? []} repoId={value.repoId} onRepoIdChange={(id) => patch({ repoId: id })} onCreateRepo={handleCreateRepo} accent={C.accent} textPrimary={C.textPrimary} textMuted={C.textMuted} textSecondary={C.textSecondary} border={C.border} deep={C.deep} warning={C.warning} />
              )}
            </div>

            {/* Reference files (ADR-053) */}
            {enableReferenceFiles && (
              <div className="flex flex-col gap-3">
                {sectionHead(t("sectionReferenceFiles"))}
                <label
                  className="flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-[11px] cursor-pointer transition-colors self-start"
                  style={{ border: `1px dashed ${C.border}`, color: C.textMuted }}
                >
                  <Paperclip size={11} />
                  {t("addFiles")}
                  <input
                    type="file"
                    multiple
                    accept={REFERENCE_FILE_ACCEPT}
                    onChange={handleReferenceFilesPicked}
                    className="hidden"
                    disabled={disabled}
                  />
                </label>
                {stagedReferenceFiles.length > 0 && (
                  <div className="flex flex-col gap-1.5">
                    {stagedReferenceFiles.map((f) => (
                      <div
                        key={f.id}
                        className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-[11px]"
                        style={{ background: C.deep, border: `1px solid ${C.border}` }}
                      >
                        <span className="truncate flex-1" style={{ color: C.textPrimary }}>{f.file.name}</span>
                        <span className="shrink-0" style={{ color: C.textMuted }}>{formatBytes(f.file.size)}</span>
                        <button
                          type="button"
                          onClick={() => removeStagedReferenceFile(f.id)}
                          disabled={disabled}
                          aria-label={`Remove ${f.file.name}`}
                          className="shrink-0 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
                          style={{ color: C.textMuted }}
                        >
                          <X size={11} />
                        </button>
                      </div>
                    ))}
                    <textarea
                      aria-label={t("referenceNoteAria")}
                      value={referenceNote}
                      onChange={(e) => updateReferenceNote(e.target.value)}
                      placeholder={t("referenceNotePlaceholder")}
                      rows={2}
                      disabled={disabled}
                      className="w-full text-[11px] px-2.5 py-2 rounded-lg outline-none resize-none"
                      style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }}
                    />
                  </div>
                )}
              </div>
            )}

            {/* Zuweisung */}
            <div className="flex flex-col gap-3">
              {sectionHead(t("sectionAssignment"))}
              <div className="flex flex-col gap-1.5">
                <label htmlFor={`${fieldId}-agent`} className="text-[10px]" style={{ color: C.textMuted }}><Users size={10} className="inline mr-1" />Agent</label>
                <div className="flex items-center gap-2">
                  {selectedAgent && <span className="w-2 h-2 rounded-full shrink-0" style={{ background: C.online }} />}
                  <select id={`${fieldId}-agent`} aria-label="Agent" value={value.selectedAgentId ?? ""} onChange={(e) => patch({ selectedAgentId: e.target.value || null })} className={selCls} style={selStyle(!!selectedAgent)}>
                    <option value="">{t("agentAuto")}</option>
                    {availableAgents.map((a) => (<option key={a.id} value={a.id}>{a.name} — {a.role ?? "Agent"}</option>))}
                  </select>
                </div>
              </div>
              <div className="flex flex-col gap-1.5">
                <span className="text-[10px]" style={{ color: C.textMuted }}>{t("priority")}</span>
                <div className="flex items-center gap-1">
                  {PRIORITY_OPTIONS.map((opt) => (
                    <button key={opt.value} type="button" onClick={() => patch({ priority: opt.value })} aria-label={t("priorityAria", { label: opt.label })} aria-pressed={value.priority === opt.value}
                      className="w-7 h-7 flex items-center justify-center rounded-lg text-[10px] font-bold transition-all cursor-pointer" style={pill(value.priority === opt.value, opt.color)}>
                      {opt.label}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            {/* Ausführung */}
            <div className="flex flex-col gap-3">
              {sectionHead(t("sectionExecution"))}
              <div className="flex flex-col gap-1.5">
                <span className="text-[10px]" style={{ color: C.textMuted }}>{t("type")}</span>
                <div className="flex items-center gap-1.5 flex-wrap">
                  {TASK_TYPE_OPTIONS.map((opt) => (
                    <button key={opt.value} type="button" onClick={() => patch({ taskType: opt.value })} aria-pressed={value.taskType === opt.value}
                      className="px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer" style={pill(value.taskType === opt.value, C.accent)}>
                      {t(opt.labelKey)}
                    </button>
                  ))}
                </div>
              </div>
              <div className="flex flex-col gap-1.5">
                <label htmlFor={`${fieldId}-deadline`} className="text-[10px]" style={{ color: C.textMuted }}><Calendar size={10} className="inline mr-1" />{t("deadline")}</label>
                <input id={`${fieldId}-deadline`} type="date" aria-label={t("deadline")} value={value.dueAt} onChange={(e) => patch({ dueAt: e.target.value })}
                  className="w-full text-[11px] px-2.5 py-1.5 rounded-lg outline-none cursor-pointer" style={{ background: C.deep, border: `1px solid ${value.dueAt ? `${alpha(C.accent, 0.33)}` : C.border}`, color: value.dueAt ? C.textPrimary : C.textMuted, colorScheme: "dark" }} />
              </div>
              <PlannerSlider value={value.plannerMode} onChange={(m) => patch({ plannerMode: m })} accent={C.accent} textMuted={C.textMuted} textSecondary={C.textSecondary} border={C.border} />
            </div>

            {workspacePreview && (
              <div className="flex items-start gap-2 px-3 py-2 rounded-lg font-mono text-[10px]" style={{ background: `${alpha(C.accent, 0.03)}`, border: `1px solid ${alpha(C.accent, 0.13)}` }}>
                <FolderKanban size={11} style={{ color: C.accent, flexShrink: 0, marginTop: 1 }} />
                <div className="min-w-0">
                  <span style={{ color: C.textMuted }}>{t("workspace")}: </span>
                  <span style={{ color: C.textPrimary, wordBreak: "break-all" }}>{workspacePreview}</span>
                </div>
              </div>
            )}

            {/* Erweitert (progressive disclosure replaces the Schnell/Strukturiert toggle) */}
            <div className="flex flex-col gap-3" style={{ borderTop: `1px solid ${C.borderSubtle}`, paddingTop: "14px" }}>
              <button type="button" onClick={() => setAdvancedExpanded((v) => !v)} aria-expanded={advancedExpanded} aria-controls={`${fieldId}-advanced`}
                className="flex items-center gap-1.5 text-[12px] font-medium transition-colors cursor-pointer self-start" style={{ color: advancedExpanded ? C.textPrimary : C.textSecondary }}>
                {advancedExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                <Settings2 size={11} />{t("advanced")}
                {!advancedExpanded && (<span className="font-normal" style={{ color: C.textMuted }}>{t("advancedHint")}</span>)}
              </button>
              <AnimatePresence>
                {advancedExpanded && (
                  <motion.div id={`${fieldId}-advanced`} initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} transition={{ duration: 0.2 }} className="flex flex-col gap-4 overflow-hidden">
                    <div className="flex flex-col gap-1.5">
                      <label htmlFor={`${fieldId}-approval`} className="text-[10px]" style={{ color: C.textMuted }}>{t("approval")}</label>
                      <select id={`${fieldId}-approval`} aria-label={t("approvalAria")} value={value.approvalPolicy} onChange={(e) => patch({ approvalPolicy: e.target.value })} className={selCls} style={selStyle(!!value.approvalPolicy)}>
                        {APPROVAL_OPTIONS.map((opt) => (<option key={opt.value} value={opt.value}>{t(opt.labelKey)}</option>))}
                      </select>
                    </div>
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <button type="button" onClick={() => patch({ needsBrowser: !value.needsBrowser })} aria-pressed={value.needsBrowser} className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer" style={pill(value.needsBrowser, C.info)}><Globe size={11} />Browser</button>
                      <button type="button" onClick={() => patch({ requiresAuth: !value.requiresAuth })} aria-pressed={value.requiresAuth} className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer" style={pill(value.requiresAuth, C.warning)}><KeyRound size={11} />Auth</button>
                      <button type="button" onClick={() => patch({ reportBack: !value.reportBack })} aria-pressed={value.reportBack} className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer" style={pill(value.reportBack, C.online)}><MessageSquare size={11} />Report-Back</button>
                      <button type="button" onClick={() => patch({ e2eTestRequired: !value.e2eTestRequired })} aria-pressed={value.e2eTestRequired} title="After review, a tester agent drives the real user flows in a browser before the task can complete" className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer" style={pill(value.e2eTestRequired, C.accent)}><MousePointerClick size={11} />E2E test</button>
                      <button type="button" onClick={() => patch({ humanReviewRequired: !value.humanReviewRequired, ...(value.humanReviewRequired ? {} : { skipReview: false }) })} aria-pressed={value.humanReviewRequired} title="You review this task yourself instead of a review agent" className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer" style={pill(value.humanReviewRequired, C.accent)}><UserCheck size={11} />Human review</button>
                      <button type="button" onClick={() => patch({ skipReview: !value.skipReview, ...(value.skipReview ? {} : { humanReviewRequired: false }) })} aria-pressed={value.skipReview} title="Task goes straight to done — no review stage (typical for scheduled/report jobs)" className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer" style={pill(value.skipReview, C.accent)}><FastForward size={11} />Skip review</button>
                      <button type="button" onClick={() => patch({ blockerToOperator: !value.blockerToOperator })} aria-pressed={value.blockerToOperator} title="Blockers on this task come straight to you instead of going to Boss first" className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer" style={pill(value.blockerToOperator, C.warning)}><BellRing size={11} />Blocker to me</button>
                    </div>
                    <AnimatePresence>
                      {value.requiresAuth && (
                        <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} transition={{ duration: 0.15 }} className="flex flex-col gap-2 overflow-hidden pl-2" style={{ borderLeft: `2px solid ${alpha(C.warning, 0.2)}` }}>
                          <div className="flex items-center gap-2">
                            <button type="button" onClick={() => patch({ credentialMode: "vault" })} className="px-2 py-0.5 text-[10px] font-mono rounded-md cursor-pointer" style={pill(value.credentialMode === "vault", C.warning)}>{t("fromVault")}</button>
                            <button type="button" onClick={() => patch({ credentialMode: "inline" })} className="px-2 py-0.5 text-[10px] font-mono rounded-md cursor-pointer" style={pill(value.credentialMode === "inline", C.warning)}>{t("enterOnce")}</button>
                          </div>
                          {value.credentialMode === "vault" && (
                            <select aria-label={t("credentialAria")} value={value.credentialId ?? ""} onChange={(e) => patch({ credentialId: e.target.value || null })} className={selCls} style={selStyle(!!value.credentialId)}>
                              <option value="">{t("credentialChoose")}</option>
                              {(vaultCredentials ?? []).map((c) => (<option key={c.id} value={c.id}>{c.name} ({c.credential_type})</option>))}
                            </select>
                          )}
                          {value.credentialMode === "inline" && (
                            <textarea aria-label={t("inlineCredentialsAria")} value={value.inlineCredentials} onChange={(e) => patch({ inlineCredentials: e.target.value })} placeholder="Username: admin&#10;Password: ..." rows={2} className="w-full text-[11px] px-3 py-2 rounded-xl outline-none resize-none font-mono" style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }} />
                          )}
                        </motion.div>
                      )}
                    </AnimatePresence>
                    <AnimatePresence>
                      {value.reportBack && (
                        <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} transition={{ duration: 0.15 }} className="flex flex-col gap-2 overflow-hidden pl-2" style={{ borderLeft: `2px solid ${alpha(C.online, 0.2)}` }}>
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="text-[10px]" style={{ color: C.textMuted }}>{t("channel")}</span>
                            {["discord", "telegram"].map((ch) => (<button key={ch} type="button" onClick={() => patch({ reportChannel: ch })} className="px-2 py-0.5 text-[10px] font-mono rounded-md cursor-pointer" style={pill(value.reportChannel === ch, C.online)}>{ch.charAt(0).toUpperCase() + ch.slice(1)}</button>))}
                          </div>
                          <div className="flex items-center gap-1.5 flex-wrap">
                            <span className="text-[10px]" style={{ color: C.textMuted }}>{t("format")}</span>
                            {[{ value: "summary", label: "Summary" }, { value: "screenshot", label: "Screenshot" }, { value: "before_after", label: "Before/After" }].map((fmt) => (<button key={fmt.value} type="button" onClick={() => toggleReportFormat(fmt.value)} className="px-2 py-0.5 text-[10px] font-mono rounded-md cursor-pointer" style={pill(value.reportFormats.includes(fmt.value), C.online)}>{fmt.label}</button>))}
                          </div>
                        </motion.div>
                      )}
                    </AnimatePresence>
                    <div className="flex flex-col gap-1.5">
                      <span className="text-[10px]" style={{ color: C.textMuted }}>{t("referenceUrls")}</span>
                      <UrlListInput value={value.referenceUrls} onChange={(urls) => patch({ referenceUrls: urls })} textPrimary={C.textPrimary} textMuted={C.textMuted} border={C.border} deep={C.deep} accent={C.accent} />
                    </div>
                    <div className="flex flex-col gap-3" style={{ borderTop: `1px solid ${C.borderSubtle}`, paddingTop: "10px" }}>
                      <span className="text-[10px] font-medium flex items-center gap-1.5" style={{ color: C.textMuted }}><ClipboardList size={11} />{t("operatorIntake")}</span>
                      <div className="flex flex-col gap-1.5">
                        <label htmlFor={`${fieldId}-requestkind`} className="text-[10px]" style={{ color: C.textMuted }}>{t("requestKind")}</label>
                        <select id={`${fieldId}-requestkind`} aria-label={t("requestKind")} value={value.requestKind} onChange={(e) => patch({ requestKind: e.target.value })} className={selCls} style={selStyle(!!value.requestKind)}>
                          {REQUEST_KIND_OPTIONS.map((opt) => (<option key={opt.value} value={opt.value}>{t(opt.labelKey)}</option>))}
                        </select>
                      </div>
                      <div className="flex flex-col gap-1.5">
                        <label htmlFor={`${fieldId}-autonomy`} className="text-[10px]" style={{ color: C.textMuted }}>{t("autonomy")}</label>
                        <select id={`${fieldId}-autonomy`} aria-label={t("autonomyAria")} value={value.autonomyLevel} onChange={(e) => patch({ autonomyLevel: e.target.value })} className={selCls} style={selStyle(!!value.autonomyLevel)}>
                          {AUTONOMY_OPTIONS.map((opt) => (<option key={opt.value} value={opt.value}>{t(opt.labelKey)}</option>))}
                        </select>
                      </div>
                      <textarea aria-label={t("desiredOutputAria")} value={value.desiredOutput} onChange={(e) => patch({ desiredOutput: e.target.value })} placeholder={t("desiredOutputPlaceholder")} rows={2} className="w-full text-[12px] outline-none px-3 py-2 rounded-xl resize-none" style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }} />
                      <textarea aria-label={t("referenceNotesAria")} value={value.referenceNotes} onChange={(e) => patch({ referenceNotes: e.target.value })} placeholder={t("referenceNotesPlaceholder")} rows={2} className="w-full text-[12px] outline-none px-3 py-2 rounded-xl resize-none" style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }} />
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-[10px]" style={{ color: C.textMuted }}>{t("publishing")}</span>
                        <button type="button" onClick={() => patch({ publishAllowed: value.publishAllowed === true ? null : true })} className="px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer" style={pill(value.publishAllowed === true, C.online)}>{t("publishAllowed")}</button>
                        <button type="button" onClick={() => patch({ publishAllowed: value.publishAllowed === false ? null : false })} className="px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer" style={pill(value.publishAllowed === false, C.warning)}>{t("publishDraftOnly")}</button>
                        {value.publishAllowed === null && (<span className="text-[10px]" style={{ color: C.textMuted }}>{t("publishAgentDecides")}</span>)}
                      </div>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={className} style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
      {/* ── Templates (Quick-Start Chips) ── */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[10px] shrink-0" style={{ color: C.textMuted }}>
          <Wand2 size={10} className="inline mr-1 mb-0.5" />
          {t("templateLabel")}
        </span>
        {(Object.keys(TEMPLATES) as Array<keyof typeof TEMPLATES>).map((key) => {
          const t = TEMPLATES[key];
          const active = value.activeTemplate === key;
          const Icon = t.icon;
          return (
            <button
              key={key}
              type="button"
              onClick={() => (active ? patch({ activeTemplate: null }) : applyTemplate(key))}
              className="flex items-center gap-1 px-2.5 py-1 text-[10px] font-mono font-medium rounded-md transition-all cursor-pointer"
              style={{
                color: active ? t.color : C.textMuted,
                background: active ? `${alpha(t.color, 0.08)}` : "transparent",
                border: `1px solid ${active ? `${alpha(t.color, 0.33)}` : C.border}`,
              }}
            >
              <Icon size={10} />
              {t.label}
            </button>
          );
        })}
        {value.activeTemplate && (
          <button
            type="button"
            onClick={() => patch({ activeTemplate: null })}
            className="text-[10px] ml-1 cursor-pointer hover:underline"
            style={{ color: C.textMuted }}
          >
            zuruecksetzen
          </button>
        )}
      </div>

      {/* ── Title ── */}
      <div className="flex flex-col gap-1">
        <label htmlFor={`${fieldId}-title`} className="text-[10px] font-medium" style={{ color: C.textMuted }}>
          {t("title")} <span style={{ color: C.error }}>*</span>
        </label>
        <input
          id={`${fieldId}-title`}
          ref={titleRef}
          type="text"
          required
          value={value.title}
          onChange={(e) => patch({ title: e.target.value })}
          onKeyDown={handleKeyDown}
          placeholder={t("titlePlaceholder")}
          className="w-full text-sm outline-none px-3 py-2.5 rounded-xl transition-all"
          style={{
            border: `1px solid ${C.border}`,
            color: C.textPrimary,
            backgroundColor: C.deep,
          }}
          onFocus={(e) => { e.target.style.borderColor = `${alpha(C.accent, 0.4)}`; e.target.style.boxShadow = `0 0 16px ${alpha(C.accent, 0.08)}`; }}
          onBlur={(e) => { e.target.style.borderColor = C.border; e.target.style.boxShadow = "none"; }}
          disabled={disabled}
        />
      </div>

      {/* ── Description ── */}
      <div className="flex flex-col gap-1">
        <label htmlFor={`${fieldId}-description`} className="text-[10px] font-medium" style={{ color: C.textMuted }}>
          {t("description")} <span style={{ color: C.textMuted }}>{t("optional")}</span>
        </label>
        <textarea
          id={`${fieldId}-description`}
          ref={descriptionRef}
          value={value.description}
          onChange={(e) => {
            patch({ description: e.target.value });
            const el = descriptionRef?.current;
            if (el) {
              el.style.height = "auto";
              el.style.height = `${el.scrollHeight}px`;
            }
          }}
          onKeyDown={handleKeyDown}
          placeholder={descriptionPlaceholder}
          className="w-full text-sm outline-none px-3 py-2.5 rounded-xl resize-none transition-all"
          style={{
            border: `1px solid ${C.border}`,
            color: C.textPrimary,
            backgroundColor: C.deep,
            minHeight: "96px",
            overflowY: "hidden",
          }}
          onFocus={(e) => { e.target.style.borderColor = `${alpha(C.accent, 0.4)}`; e.target.style.boxShadow = `0 0 16px ${alpha(C.accent, 0.08)}`; }}
          onBlur={(e) => { e.target.style.borderColor = C.border; e.target.style.boxShadow = "none"; }}
          disabled={disabled}
        />
      </div>

      {/* ── Priority + Mode-Toggle (Row) ── */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <span className="text-[10px]" style={{ color: C.textMuted }}>{t("priority")}:</span>
          <div className="flex items-center gap-1">
            {PRIORITY_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                type="button"
                onClick={() => patch({ priority: opt.value })}
                className="w-6 h-6 flex items-center justify-center rounded-lg text-[10px] font-bold transition-all cursor-pointer"
                style={{
                  backgroundColor: value.priority === opt.value ? `${alpha(opt.color, 0.13)}` : "transparent",
                  color: value.priority === opt.value ? opt.color : C.textMuted,
                  border: value.priority === opt.value ? `1px solid ${alpha(opt.color, 0.4)}` : "1px solid transparent",
                }}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>

        {/* Mode toggle */}
        <div
          className="flex items-center rounded-lg p-0.5"
          style={{ background: C.deep, border: `1px solid ${C.border}` }}
        >
          {(["schnell", "strukturiert"] as TaskMode[]).map((m) => {
            const active = mode === m;
            const Icon = m === "schnell" ? Zap : Settings2;
            return (
              <button
                key={m}
                type="button"
                onClick={() => toggleMode(m)}
                className="flex items-center gap-1 px-2.5 py-1 text-[10px] font-mono font-medium rounded-md transition-all cursor-pointer"
                style={{
                  color: active ? C.accent : C.textMuted,
                  background: active ? `${alpha(C.accent, 0.08)}` : "transparent",
                }}
              >
                <Icon size={10} />
                {m === "schnell" ? t("modeQuick") : t("modeStructured")}
              </button>
            );
          })}
        </div>
      </div>

      {/* ── Agent Grid ── */}
      {availableAgents.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center justify-between">
            <span className="text-[10px]" style={{ color: C.textMuted }}>
              <Users size={10} className="inline mr-1" />
              Agent {selectedAgent ? `: ${selectedAgent.name}` : "(auto wenn leer)"}
            </span>
            {value.selectedAgentId && (
              <button
                type="button"
                onClick={() => patch({ selectedAgentId: null })}
                className="text-[9px] cursor-pointer hover:underline"
                style={{ color: C.textMuted }}
              >
                zuruecksetzen
              </button>
            )}
          </div>
          <div className="grid grid-cols-3 gap-1.5">
            {availableAgents.map((a) => (
              <AgentCard
                key={a.id}
                agent={a}
                selected={value.selectedAgentId === a.id}
                onSelect={() =>
                  patch({ selectedAgentId: value.selectedAgentId === a.id ? null : a.id })
                }
              />
            ))}
          </div>
        </div>
      )}

      {/* ── Project ── */}
      <ProjectCombobox
        projects={projects ?? []}
        value={value.projectId}
        onChange={(id) => {
          patch({
            projectId: id,
            phaseId: null,
            deliverableId: null,
            branchName: "",
            repoId: null,
          });
        }}
        onCreateProject={handleCreateProject}
        accent={C.accent}
        textPrimary={C.textPrimary}
        textMuted={C.textMuted}
        textSecondary={C.textSecondary}
        border={C.border}
        deep={C.deep}
      />

      {/* ── Workspace-Path Preview ── */}
      {workspacePreview && (
        <motion.div
          initial={{ opacity: 0, y: -4 }}
          animate={{ opacity: 1, y: 0 }}
          className="flex items-center gap-2 px-3 py-2 rounded-lg font-mono text-[10px]"
          style={{
            background: `${alpha(C.accent, 0.03)}`,
            border: `1px solid ${alpha(C.accent, 0.13)}`,
            color: C.textSecondary,
          }}
        >
          <FolderKanban size={11} style={{ color: C.accent, flexShrink: 0 }} />
          <span style={{ color: C.textMuted }}>{t("workspace")}:</span>
          <code style={{ color: C.textPrimary }}>{workspacePreview}</code>
        </motion.div>
      )}

      {/* ── SCHNELL-MODE: Kompakter Skip-Review-Toggle (haeufigster Job-Fall) ── */}
      {mode === "schnell" && (
        <button
          type="button"
          onClick={() => patch({ skipReview: !value.skipReview, ...(value.skipReview ? {} : { humanReviewRequired: false }) })}
          aria-pressed={value.skipReview}
          title="Task goes straight to done — no review stage (typical for scheduled/report jobs)"
          className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer self-start"
          style={{
            backgroundColor: value.skipReview ? `${alpha(C.accent, 0.13)}` : "transparent",
            color: value.skipReview ? C.accent : C.textMuted,
            border: value.skipReview ? `1px solid ${alpha(C.accent, 0.4)}` : `1px solid ${C.border}`,
          }}
        >
          <FastForward size={11} />
          Skip review
        </button>
      )}

      {/* ── SCHNELL-MODE: Kompakter Auth-Toggle ── */}
      {mode === "schnell" && (vaultCredentials ?? []).length > 0 && (
        <div className="flex flex-col gap-2">
          <button
            type="button"
            onClick={() => patch({ requiresAuth: !value.requiresAuth })}
            className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer self-start"
            style={{
              backgroundColor: value.requiresAuth ? `${alpha(C.warning, 0.13)}` : "transparent",
              color: value.requiresAuth ? C.warning : C.textMuted,
              border: value.requiresAuth ? `1px solid ${alpha(C.warning, 0.4)}` : `1px solid ${C.border}`,
            }}
          >
            <KeyRound size={11} />
            Auth
            {value.requiresAuth && value.credentialId && (() => {
              const cred = (vaultCredentials ?? []).find((c) => c.id === value.credentialId);
              return cred ? <span style={{ opacity: 0.75 }}>· {cred.name}</span> : null;
            })()}
          </button>
          <AnimatePresence>
            {value.requiresAuth && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.15 }}
                className="flex flex-col gap-2 overflow-hidden pl-2"
                style={{ borderLeft: `2px solid ${alpha(C.warning, 0.2)}` }}
              >
                <select
                  value={value.credentialId ?? ""}
                  onChange={(e) => patch({ credentialId: e.target.value || null, credentialMode: "vault" })}
                  className="text-[11px] px-2.5 py-1.5 rounded-lg outline-none cursor-pointer"
                  style={{
                    background: C.deep,
                    border: `1px solid ${value.credentialId ? `${alpha(C.warning, 0.4)}` : C.border}`,
                    color: value.credentialId ? C.warning : C.textMuted,
                  }}
                >
                  <option value="">{t("credentialChoose")}</option>
                  {(vaultCredentials ?? []).map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name} ({c.credential_type})
                    </option>
                  ))}
                </select>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      )}

      {/* ── STRUKTURIERT-ONLY CONTENT ── */}
      {mode === "strukturiert" && (
        <div className="flex flex-col gap-4">
          {/* Phase + Deliverable-Ref */}
          {value.projectId && phases && phases.length > 0 && (
            <div className="flex items-center gap-2">
              <span className="text-[10px] shrink-0 w-20" style={{ color: C.textMuted }}>Phase:</span>
              <select
                aria-label={t("phase")}
                value={value.phaseId ?? ""}
                onChange={(e) => patch({ phaseId: e.target.value || null })}
                className="flex-1 text-[11px] px-2.5 py-1.5 rounded-lg outline-none cursor-pointer"
                style={{
                  background: C.deep,
                  border: `1px solid ${value.phaseId ? `${alpha(C.accent, 0.4)}` : C.border}`,
                  color: value.phaseId ? C.accent : C.textMuted,
                }}
              >
                <option value="">{t("noPhase")}</option>
                {phases
                  .filter((p) => p.status === "active" || p.status === "pending")
                  .map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.status === "active" ? "●" : "○"} {p.title}
                    </option>
                  ))}
              </select>
            </div>
          )}

          {value.projectId && deliverables && deliverables.length > 0 && (
            <div className="flex items-center gap-2">
              <span className="text-[10px] shrink-0 w-20" style={{ color: C.textMuted }}>{t("basedOn")}:</span>
              <select
                aria-label={t("basedOnAria")}
                value={value.deliverableId ?? ""}
                onChange={(e) => patch({ deliverableId: e.target.value || null })}
                className="flex-1 text-[11px] px-2.5 py-1.5 rounded-lg outline-none cursor-pointer"
                style={{
                  background: C.deep,
                  border: `1px solid ${value.deliverableId ? `${alpha(C.accent, 0.4)}` : C.border}`,
                  color: value.deliverableId ? C.accent : C.textMuted,
                }}
              >
                <option value="">{t("noDeliverable")}</option>
                {deliverables.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.title} ({d.deliverable_type})
                  </option>
                ))}
              </select>
            </div>
          )}

          {/* Git Info */}
          {value.projectId ? (
            <GitInfoBox
              gitInfo={gitInfo}
              isLoading={gitInfoLoading}
              autoSlug={autoSlug}
              branchName={value.branchName}
              onBranchNameChange={(name) => patch({ branchName: name })}
              onInitRepo={handleInitRepo}
              initLoading={initLoading}
              repos={repos ?? []}
              onLinkRepo={handleLinkRepo}
              accent={C.accent}
              textPrimary={C.textPrimary}
              textMuted={C.textMuted}
              textSecondary={C.textSecondary}
              border={C.border}
              deep={C.deep}
              warning={C.warning}
              online={C.online}
            />
          ) : (
            <GitInfoBox
              gitInfo={null}
              isLoading={false}
              autoSlug={autoSlug}
              branchName={value.branchName}
              onBranchNameChange={(name) => patch({ branchName: name })}
              onInitRepo={() => {}}
              initLoading={false}
              adHocMode
              repos={repos ?? []}
              repoId={value.repoId}
              onRepoIdChange={(id) => patch({ repoId: id })}
              onCreateRepo={handleCreateRepo}
              accent={C.accent}
              textPrimary={C.textPrimary}
              textMuted={C.textMuted}
              textSecondary={C.textSecondary}
              border={C.border}
              deep={C.deep}
              warning={C.warning}
            />
          )}

          {/* Task-Type */}
          <div className="flex items-center gap-2">
            <span className="text-[10px] shrink-0 w-20" style={{ color: C.textMuted }}>{t("type")}:</span>
            <div className="flex items-center gap-1.5">
              {TASK_TYPE_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => patch({ taskType: opt.value })}
                  className="px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer"
                  style={{
                    backgroundColor: value.taskType === opt.value ? `${alpha(C.accent, 0.13)}` : "transparent",
                    color: value.taskType === opt.value ? C.accent : C.textMuted,
                    border: `1px solid ${value.taskType === opt.value ? `${alpha(C.accent, 0.4)}` : C.border}`,
                  }}
                >
                  {t(opt.labelKey)}
                </button>
              ))}
            </div>
          </div>

          {/* PlannerSlider */}
          <PlannerSlider
            value={value.plannerMode}
            onChange={(m) => patch({ plannerMode: m })}
            accent={C.accent}
            textMuted={C.textMuted}
            textSecondary={C.textSecondary}
            border={C.border}
          />

          {/* Planner details */}
          <AnimatePresence>
            {value.plannerMode === "with_planner" && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.2 }}
                className="flex flex-col gap-3 overflow-hidden"
              >
                <textarea
                  aria-label={t("acceptance")}
                  value={value.acceptanceCriteria}
                  onChange={(e) => patch({ acceptanceCriteria: e.target.value })}
                  placeholder={acceptancePlaceholder}
                  rows={2}
                  className="w-full text-[12px] outline-none px-3 py-2 rounded-xl resize-none"
                  style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }}
                />
                <textarea
                  aria-label={t("scopeOut")}
                  value={value.scopeOut}
                  onChange={(e) => patch({ scopeOut: e.target.value })}
                  placeholder={t("scopeOutPlaceholder")}
                  rows={2}
                  className="w-full text-[12px] outline-none px-3 py-2 rounded-xl resize-none"
                  style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }}
                />
              </motion.div>
            )}
          </AnimatePresence>

          {/* Deadline */}
          <div className="flex items-center gap-2">
            <span className="text-[10px] shrink-0 w-20" style={{ color: C.textMuted }}>
              <Calendar size={10} className="inline mr-1" />
              Deadline:
            </span>
            <input
              type="date"
              aria-label={t("deadline")}
              value={value.dueAt}
              onChange={(e) => patch({ dueAt: e.target.value })}
              className="text-[11px] px-2.5 py-1.5 rounded-lg outline-none cursor-pointer"
              style={{
                background: C.deep,
                border: `1px solid ${value.dueAt ? `${alpha(C.accent, 0.4)}` : C.border}`,
                color: value.dueAt ? C.textPrimary : C.textMuted,
                colorScheme: "dark",
              }}
            />
          </div>

          <textarea
            aria-label={t("risks")}
            value={value.riskNotes}
            onChange={(e) => patch({ riskNotes: e.target.value })}
            placeholder={t("risksPlaceholder")}
            rows={2}
            className="w-full text-[12px] outline-none px-3 py-2 rounded-xl resize-none"
            style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }}
          />

          <div className="flex flex-col gap-1">
            <span className="text-[10px]" style={{ color: C.textMuted }}>{t("referenceUrls")}:</span>
            <UrlListInput
              value={value.referenceUrls}
              onChange={(urls) => patch({ referenceUrls: urls })}
              textPrimary={C.textPrimary}
              textMuted={C.textMuted}
              border={C.border}
              deep={C.deep}
              accent={C.accent}
            />
          </div>

          {/* Approval */}
          <div className="flex items-center gap-2">
            <span className="text-[10px] shrink-0 w-20" style={{ color: C.textMuted }}>{t("approval")}:</span>
            <select
              aria-label={t("approvalAria")}
              value={value.approvalPolicy}
              onChange={(e) => patch({ approvalPolicy: e.target.value })}
              className="text-[11px] px-2.5 py-1.5 rounded-lg outline-none cursor-pointer"
              style={{
                background: C.deep,
                border: `1px solid ${value.approvalPolicy ? `${alpha(C.accent, 0.4)}` : C.border}`,
                color: value.approvalPolicy ? C.accent : C.textMuted,
              }}
            >
              {APPROVAL_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{t(opt.labelKey)}</option>
              ))}
            </select>
          </div>

          {/* Toggles row */}
          <div className="flex items-center gap-2 flex-wrap">
            <button
              type="button"
              onClick={() => patch({ needsBrowser: !value.needsBrowser })}
              className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer"
              style={{
                backgroundColor: value.needsBrowser ? `${alpha(C.info, 0.13)}` : "transparent",
                color: value.needsBrowser ? C.info : C.textMuted,
                border: value.needsBrowser ? `1px solid ${alpha(C.info, 0.4)}` : `1px solid ${C.border}`,
              }}
            >
              <Globe size={11} />
              Browser
            </button>
            <button
              type="button"
              onClick={() => patch({ e2eTestRequired: !value.e2eTestRequired })}
              title="After review, a tester agent drives the real user flows in a browser before the task can complete"
              className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer"
              style={{
                backgroundColor: value.e2eTestRequired ? `${alpha(C.accent, 0.13)}` : "transparent",
                color: value.e2eTestRequired ? C.accent : C.textMuted,
                border: value.e2eTestRequired ? `1px solid ${alpha(C.accent, 0.4)}` : `1px solid ${C.border}`,
              }}
            >
              <MousePointerClick size={11} />
              E2E test
            </button>
            <button
              type="button"
              onClick={() => patch({ humanReviewRequired: !value.humanReviewRequired, ...(value.humanReviewRequired ? {} : { skipReview: false }) })}
              title="You review this task yourself instead of a review agent"
              aria-pressed={value.humanReviewRequired}
              className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer"
              style={{
                backgroundColor: value.humanReviewRequired ? `${alpha(C.accent, 0.13)}` : "transparent",
                color: value.humanReviewRequired ? C.accent : C.textMuted,
                border: value.humanReviewRequired ? `1px solid ${alpha(C.accent, 0.4)}` : `1px solid ${C.border}`,
              }}
            >
              <UserCheck size={11} />
              Human review
            </button>
            <button
              type="button"
              onClick={() => patch({ skipReview: !value.skipReview, ...(value.skipReview ? {} : { humanReviewRequired: false }) })}
              aria-pressed={value.skipReview}
              title="Task goes straight to done — no review stage (typical for scheduled/report jobs)"
              className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer"
              style={{
                backgroundColor: value.skipReview ? `${alpha(C.accent, 0.13)}` : "transparent",
                color: value.skipReview ? C.accent : C.textMuted,
                border: value.skipReview ? `1px solid ${alpha(C.accent, 0.4)}` : `1px solid ${C.border}`,
              }}
            >
              <FastForward size={11} />
              Skip review
            </button>
            <button
              type="button"
              onClick={() => patch({ blockerToOperator: !value.blockerToOperator })}
              title="Blockers on this task come straight to you instead of going to Boss first"
              className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer"
              style={{
                backgroundColor: value.blockerToOperator ? `${alpha(C.warning, 0.13)}` : "transparent",
                color: value.blockerToOperator ? C.warning : C.textMuted,
                border: value.blockerToOperator ? `1px solid ${alpha(C.warning, 0.4)}` : `1px solid ${C.border}`,
              }}
            >
              <BellRing size={11} />
              Blocker to me
            </button>
            <button
              type="button"
              onClick={() => patch({ requiresAuth: !value.requiresAuth })}
              className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer"
              style={{
                backgroundColor: value.requiresAuth ? `${alpha(C.warning, 0.13)}` : "transparent",
                color: value.requiresAuth ? C.warning : C.textMuted,
                border: value.requiresAuth ? `1px solid ${alpha(C.warning, 0.4)}` : `1px solid ${C.border}`,
              }}
            >
              <KeyRound size={11} />
              Auth
            </button>
            <button
              type="button"
              onClick={() => patch({ reportBack: !value.reportBack })}
              className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer"
              style={{
                backgroundColor: value.reportBack ? `${alpha(C.online, 0.13)}` : "transparent",
                color: value.reportBack ? C.online : C.textMuted,
                border: value.reportBack ? `1px solid ${alpha(C.online, 0.4)}` : `1px solid ${C.border}`,
              }}
            >
              <MessageSquare size={11} />
              Report-Back
            </button>
          </div>

          {/* Auth details */}
          <AnimatePresence>
            {value.requiresAuth && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.15 }}
                className="flex flex-col gap-2 overflow-hidden pl-2"
                style={{ borderLeft: `2px solid ${alpha(C.warning, 0.2)}` }}
              >
                <div className="flex items-center gap-3">
                  <button
                    type="button"
                    onClick={() => patch({ credentialMode: "vault" })}
                    className="px-2 py-0.5 text-[10px] font-mono rounded-md cursor-pointer"
                    style={{
                      backgroundColor: value.credentialMode === "vault" ? `${alpha(C.warning, 0.13)}` : "transparent",
                      color: value.credentialMode === "vault" ? C.warning : C.textMuted,
                      border: `1px solid ${value.credentialMode === "vault" ? `${alpha(C.warning, 0.4)}` : C.border}`,
                    }}
                  >
                    {t("fromVault")}
                  </button>
                  <button
                    type="button"
                    onClick={() => patch({ credentialMode: "inline" })}
                    className="px-2 py-0.5 text-[10px] font-mono rounded-md cursor-pointer"
                    style={{
                      backgroundColor: value.credentialMode === "inline" ? `${alpha(C.warning, 0.13)}` : "transparent",
                      color: value.credentialMode === "inline" ? C.warning : C.textMuted,
                      border: `1px solid ${value.credentialMode === "inline" ? `${alpha(C.warning, 0.4)}` : C.border}`,
                    }}
                  >
                    {t("enterOnce")}
                  </button>
                </div>

                {value.credentialMode === "vault" && (
                  <select
                    aria-label={t("credentialAria")}
                    value={value.credentialId ?? ""}
                    onChange={(e) => patch({ credentialId: e.target.value || null })}
                    className="text-[11px] px-2.5 py-1.5 rounded-lg outline-none cursor-pointer"
                    style={{
                      background: C.deep,
                      border: `1px solid ${value.credentialId ? `${alpha(C.warning, 0.4)}` : C.border}`,
                      color: value.credentialId ? C.warning : C.textMuted,
                    }}
                  >
                    <option value="">{t("credentialChoose")}</option>
                    {(vaultCredentials ?? []).map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name} ({c.credential_type})
                      </option>
                    ))}
                  </select>
                )}

                {value.credentialMode === "inline" && (
                  <textarea
                    aria-label={t("inlineCredentialsAria")}
                    value={value.inlineCredentials}
                    onChange={(e) => patch({ inlineCredentials: e.target.value })}
                    placeholder="Username: admin&#10;Password: ..."
                    rows={2}
                    className="w-full text-[11px] px-3 py-2 rounded-xl outline-none resize-none font-mono"
                    style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }}
                  />
                )}
              </motion.div>
            )}
          </AnimatePresence>

          {/* Report-Back details */}
          <AnimatePresence>
            {value.reportBack && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.15 }}
                className="flex flex-col gap-2 overflow-hidden pl-2"
                style={{ borderLeft: `2px solid ${alpha(C.online, 0.2)}` }}
              >
                <div className="flex items-center gap-3">
                  <span className="text-[10px]" style={{ color: C.textMuted }}>{t("channel")}</span>
                  {["discord", "telegram"].map((ch) => (
                    <button
                      key={ch}
                      type="button"
                      onClick={() => patch({ reportChannel: ch })}
                      className="px-2 py-0.5 text-[10px] font-mono rounded-md cursor-pointer"
                      style={{
                        backgroundColor: value.reportChannel === ch ? `${alpha(C.online, 0.13)}` : "transparent",
                        color: value.reportChannel === ch ? C.online : C.textMuted,
                        border: `1px solid ${value.reportChannel === ch ? `${alpha(C.online, 0.4)}` : C.border}`,
                      }}
                    >
                      {ch.charAt(0).toUpperCase() + ch.slice(1)}
                    </button>
                  ))}
                </div>
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-[10px]" style={{ color: C.textMuted }}>{t("format")}</span>
                  {[
                    { value: "summary", label: "Summary" },
                    { value: "screenshot", label: "Screenshot" },
                    { value: "before_after", label: "Before/After" },
                  ].map((fmt) => (
                    <button
                      key={fmt.value}
                      type="button"
                      onClick={() => toggleReportFormat(fmt.value)}
                      className="px-2 py-0.5 text-[10px] font-mono rounded-md cursor-pointer"
                      style={{
                        backgroundColor: value.reportFormats.includes(fmt.value) ? `${alpha(C.online, 0.13)}` : "transparent",
                        color: value.reportFormats.includes(fmt.value) ? C.online : C.textMuted,
                        border: `1px solid ${value.reportFormats.includes(fmt.value) ? `${alpha(C.online, 0.4)}` : C.border}`,
                      }}
                    >
                      {fmt.label}
                    </button>
                  ))}
                </div>
              </motion.div>
            )}
          </AnimatePresence>

          {/* Operator-Intake (collapsed sub-section) */}
          <div style={{ borderTop: `1px solid ${C.borderSubtle}`, paddingTop: "12px", marginTop: "4px" }}>
            <button
              type="button"
              onClick={toggleIntake}
              aria-expanded={intakeExpanded}
              aria-controls="operator-intake-panel"
              className="flex items-center gap-1.5 text-[10px] font-medium transition-colors cursor-pointer"
              style={{ color: intakeExpanded ? C.accent : C.textMuted }}
            >
              {intakeExpanded ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
              <ClipboardList size={11} />
              {t("operatorIntake")}
              {!intakeExpanded && (
                <span style={{ color: C.textMuted, fontWeight: 400 }}>
                  {t("operatorIntakeHint")}
                </span>
              )}
            </button>

            <AnimatePresence>
              {intakeExpanded && (
                <motion.div
                  id="operator-intake-panel"
                  role="region"
                  aria-label={t("operatorIntakeDetailsAria")}
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: "auto" }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: 0.15 }}
                  className="flex flex-col gap-3 overflow-hidden pt-3"
                >
                  <div className="flex items-center gap-2">
                    <span className="text-[10px] shrink-0 w-20" style={{ color: C.textMuted }}>{t("requestKind")}:</span>
                    <select
                      aria-label={t("requestKind")}
                      value={value.requestKind}
                      onChange={(e) => patch({ requestKind: e.target.value })}
                      className="flex-1 text-[11px] px-2.5 py-1.5 rounded-lg outline-none cursor-pointer"
                      style={{
                        background: C.deep,
                        border: `1px solid ${value.requestKind ? `${alpha(C.accent, 0.4)}` : C.border}`,
                        color: value.requestKind ? C.accent : C.textMuted,
                      }}
                    >
                      {REQUEST_KIND_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>{t(opt.labelKey)}</option>
                      ))}
                    </select>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-[10px] shrink-0 w-20" style={{ color: C.textMuted }}>{t("autonomy")}:</span>
                    <select
                      aria-label={t("autonomyAria")}
                      value={value.autonomyLevel}
                      onChange={(e) => patch({ autonomyLevel: e.target.value })}
                      className="flex-1 text-[11px] px-2.5 py-1.5 rounded-lg outline-none cursor-pointer"
                      style={{
                        background: C.deep,
                        border: `1px solid ${value.autonomyLevel ? `${alpha(C.accent, 0.4)}` : C.border}`,
                        color: value.autonomyLevel ? C.accent : C.textMuted,
                      }}
                    >
                      {AUTONOMY_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>{t(opt.labelKey)}</option>
                      ))}
                    </select>
                  </div>
                  <textarea
                    aria-label={t("desiredOutputAria")}
                  value={value.desiredOutput}
                    onChange={(e) => patch({ desiredOutput: e.target.value })}
                    placeholder={t("desiredOutputPlaceholder")}
                    rows={2}
                    className="w-full text-[12px] outline-none px-3 py-2 rounded-xl resize-none"
                    style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }}
                  />
                  <textarea
                    aria-label={t("referenceNotesAria")}
                  value={value.referenceNotes}
                    onChange={(e) => patch({ referenceNotes: e.target.value })}
                    placeholder={t("referenceNotesPlaceholder")}
                    rows={2}
                    className="w-full text-[12px] outline-none px-3 py-2 rounded-xl resize-none"
                    style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }}
                  />
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[10px] shrink-0" style={{ color: C.textMuted }}>Veroeffentlichung:</span>
                    <button
                      type="button"
                      onClick={() => patch({ publishAllowed: value.publishAllowed === true ? null : true })}
                      className="px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer"
                      style={{
                        backgroundColor: value.publishAllowed === true ? `${alpha(C.online, 0.13)}` : "transparent",
                        color: value.publishAllowed === true ? C.online : C.textMuted,
                        border: `1px solid ${value.publishAllowed === true ? `${alpha(C.online, 0.4)}` : C.border}`,
                      }}
                    >
                      {t("publishAllowed")}
                    </button>
                    <button
                      type="button"
                      onClick={() => patch({ publishAllowed: value.publishAllowed === false ? null : false })}
                      className="px-2.5 py-1 text-[11px] font-mono font-medium rounded-md transition-all cursor-pointer"
                      style={{
                        backgroundColor: value.publishAllowed === false ? `${alpha(C.warning, 0.13)}` : "transparent",
                        color: value.publishAllowed === false ? C.warning : C.textMuted,
                        border: `1px solid ${value.publishAllowed === false ? `${alpha(C.warning, 0.4)}` : C.border}`,
                      }}
                    >
                      {t("publishDraftOnly")}
                    </button>
                    {value.publishAllowed === null && (
                      <span className="text-[10px]" style={{ color: C.textMuted }}>{t("publishAgentDecides")}</span>
                    )}
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </div>
      )}

      {/* Schnell-Mode hint when structured fields are being asked for */}
      {mode === "schnell" && (value.acceptanceCriteria.trim() || value.scopeOut.trim() || value.riskNotes.trim()) && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="flex items-center gap-1.5 text-[10px]"
          style={{ color: C.warning }}
        >
          <CircleAlert size={11} />
          Du hast strukturierte Felder angefasst — wechsle in den Strukturiert-Modus um sie zu bearbeiten.
        </motion.div>
      )}
    </div>
  );
}
