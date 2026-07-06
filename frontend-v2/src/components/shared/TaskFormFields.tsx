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
  CircleAlert, Wand2, Paperclip, X, MousePointerClick, UserCheck, BellRing } from "lucide-react";
import { useQueryClient, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { formatBytes, REFERENCE_FILE_ACCEPT } from "@/lib/utils";
import type { Agent, Project, Repo } from "@/lib/types";
import { ProjectCombobox } from "./ProjectCombobox";
import { PlannerSlider } from "./PlannerSlider";
import { GitInfoBox } from "./GitInfoBox";
import { UrlListInput } from "./UrlListInput";
import { C as MC } from "@/components/homepage/colors";

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
  { value: "story", label: "Feature" },
  { value: "bug", label: "Bugfix" },
  { value: "revision", label: "Überarbeitung" },
  { value: "chore", label: "Wartung" },
];

const APPROVAL_OPTIONS = [
  { value: "", label: "Auto" },
  { value: "never", label: "Nie" },
  { value: "on_plan", label: "Bei Plan" },
  { value: "on_execution", label: "Bei Ausführung" },
  { value: "on_publish", label: "Bei Publish" },
  { value: "on_sensitive_action", label: "Bei Risiko" },
  { value: "always", label: "Immer" },
];

const REQUEST_KIND_OPTIONS = [
  { value: "", label: "Automatisch" },
  { value: "code_change", label: "Code-Änderung" },
  { value: "content_create", label: "Content erstellen" },
  { value: "research", label: "Recherche" },
  { value: "browser_task", label: "Browser-Automation" },
  { value: "credential_task", label: "Mit Logins / Keys" },
  { value: "mixed", label: "Gemischt" },
];

const AUTONOMY_OPTIONS = [
  { value: "", label: "Unbestimmt" },
  { value: "advise_only", label: "Nur beraten" },
  { value: "draft_only", label: "Nur Entwurf" },
  { value: "execute_low_risk", label: "Low-Risk selbst ausführen" },
  { value: "execute_with_approval_on_risk", label: "Risiko → Freigabe" },
  { value: "manual_dispatch_required", label: "Manuelles Dispatch" },
];

// ── Templates (Quick-Start Chips) ────────────────────────────────────
type TemplatePrefill = {
  taskType: string;
  plannerMode?: "auto" | "with_planner" | "direct";
  requestKind?: string;
  autonomyLevel?: string;
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
      descriptionPlaceholder: "Was geht nicht? Wie reproduziert man's? Was sollte stattdessen passieren?",
      acceptancePlaceholder: "Bug ist weg, Reproduktions-Schritte geben keinen Fehler mehr, Test deckt den Case ab.",
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
      descriptionPlaceholder: "Was soll neu moeglich sein? Wer benutzt es? Warum?",
      acceptancePlaceholder: "Das neue Feature ist live, ein typischer User-Flow funktioniert.",
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
      descriptionPlaceholder: "Was willst du herausfinden? Welche Quellen hast du im Kopf?",
      acceptancePlaceholder: "Zusammenfassung mit 3+ Primaerquellen und klarer Empfehlung.",
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
        background: selected ? `${C.accent}0F` : C.deep,
        border: `1px solid ${selected ? `${C.accent}66` : C.border}`,
        boxShadow: selected ? `0 0 0 1px ${C.accent}22, 0 0 24px ${C.accent}14` : "none",
      }}
    >
      <div className="flex items-center gap-1.5">
        <span
          className="w-1.5 h-1.5 rounded-full shrink-0"
          style={{ background: statusColor, boxShadow: `0 0 6px ${statusColor}aa` }}
        />
        <span
          className="text-[12px] font-semibold truncate"
          style={{ color: selected ? C.accent : C.textPrimary }}
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
      notify.success("Repo verknüpft");
    } catch (err) {
      const msg = err instanceof Error && err.message ? err.message : "Repo-Verknüpfung fehlgeschlagen";
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
  const descriptionPlaceholder = currentTemplate?.prefill.descriptionPlaceholder ?? "Was soll gemacht werden?";

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
      border: `1px solid ${active ? `${C.accent}55` : C.border}`,
      color: active ? C.textPrimary : C.textMuted,
    });
    const pill = (active: boolean, color: string): React.CSSProperties => ({
      backgroundColor: active ? `${color}22` : "transparent",
      color: active ? color : C.textMuted,
      border: `1px solid ${active ? `${color}55` : C.border}`,
    });
    const sectionHead = (label: string) => (
      <div className="flex items-center gap-2.5">
        <span className="text-[10px] font-semibold uppercase tracking-[0.12em]" style={{ color: C.textMuted }}>{label}</span>
        <div className="flex-1 h-px" style={{ background: C.borderSubtle }} />
      </div>
    );

    return (
      <div className={className} style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
        {/* Templates (full width) */}
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[10px] shrink-0" style={{ color: C.textMuted }}>
            <Wand2 size={10} className="inline mr-1 mb-0.5" />Vorlage:
          </span>
          {(Object.keys(TEMPLATES) as Array<keyof typeof TEMPLATES>).map((key) => {
            const t = TEMPLATES[key]; const active = value.activeTemplate === key; const Icon = t.icon;
            return (
              <button key={key} type="button"
                onClick={() => (active ? patch({ activeTemplate: null }) : applyTemplate(key))}
                className="flex items-center gap-1 px-2.5 py-1 text-[10px] font-medium rounded-full transition-all cursor-pointer"
                style={{ color: active ? t.color : C.textMuted, background: active ? `${t.color}15` : "transparent", border: `1px solid ${active ? `${t.color}55` : C.border}` }}>
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
              <label htmlFor={`${fieldId}-title`} className="text-[11px] font-medium" style={{ color: C.textMuted }}>Titel <span style={{ color: C.error }}>*</span></label>
              <input id={`${fieldId}-title`} ref={titleRef} type="text" required value={value.title}
                onChange={(e) => patch({ title: e.target.value })} onKeyDown={handleKeyDown}
                placeholder="Kurzer, klarer Aufgabentitel"
                className="w-full text-[15px] outline-none px-3.5 py-3 rounded-xl transition-all"
                style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }}
                onFocus={(e) => { e.target.style.borderColor = `${C.accent}66`; e.target.style.boxShadow = `0 0 0 3px ${C.accent}1a`; }}
                onBlur={(e) => { e.target.style.borderColor = C.border; e.target.style.boxShadow = "none"; }}
                disabled={disabled} />
            </div>
            <div className="flex flex-col gap-1.5 flex-1">
              <label htmlFor={`${fieldId}-description`} className="text-[11px] font-medium" style={{ color: C.textMuted }}>Beschreibung <span style={{ color: C.textMuted }}>(optional)</span></label>
              <textarea id={`${fieldId}-description`} ref={descriptionRef} value={value.description}
                onChange={(e) => patch({ description: e.target.value })}
                onKeyDown={handleKeyDown} placeholder={descriptionPlaceholder}
                className="w-full flex-1 text-sm outline-none px-3.5 py-3 rounded-xl resize-none transition-all"
                style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep, minHeight: "200px" }}
                onFocus={(e) => { e.target.style.borderColor = `${C.accent}66`; e.target.style.boxShadow = `0 0 0 3px ${C.accent}1a`; }}
                onBlur={(e) => { e.target.style.borderColor = C.border; e.target.style.boxShadow = "none"; }}
                disabled={disabled} />
            </div>
            <AnimatePresence>
              {value.plannerMode === "with_planner" && (
                <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} transition={{ duration: 0.2 }} className="flex flex-col gap-3 overflow-hidden">
                  <div className="flex flex-col gap-1.5">
                    <label htmlFor={`${fieldId}-acceptance`} className="text-[11px] font-medium" style={{ color: C.textMuted }}>Acceptance Criteria</label>
                    <textarea id={`${fieldId}-acceptance`} aria-label="Acceptance Criteria" value={value.acceptanceCriteria} onChange={(e) => patch({ acceptanceCriteria: e.target.value })}
                      placeholder={currentTemplate?.prefill.acceptancePlaceholder ?? "Was muss erfuellt sein?"} rows={2}
                      className="w-full text-[12px] outline-none px-3 py-2 rounded-xl resize-none" style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }} />
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <label htmlFor={`${fieldId}-scope`} className="text-[11px] font-medium" style={{ color: C.textMuted }}>Scope-Out</label>
                    <textarea id={`${fieldId}-scope`} aria-label="Scope-Out" value={value.scopeOut} onChange={(e) => patch({ scopeOut: e.target.value })}
                      placeholder="Was gehoert NICHT dazu?" rows={2}
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
              {sectionHead("Projekt")}
              <ProjectCombobox projects={projects ?? []} value={value.projectId}
                onChange={(id) => patch({ projectId: id, phaseId: null, deliverableId: null, branchName: "", repoId: null })}
                onCreateProject={handleCreateProject} accent={C.accent} textPrimary={C.textPrimary} textMuted={C.textMuted} textSecondary={C.textSecondary} border={C.border} deep={C.deep} />
              {value.projectId && phases && phases.length > 0 && (
                <div className="flex flex-col gap-1.5">
                  <label htmlFor={`${fieldId}-phase`} className="text-[10px]" style={{ color: C.textMuted }}>Phase</label>
                  <select id={`${fieldId}-phase`} aria-label="Phase" value={value.phaseId ?? ""} onChange={(e) => patch({ phaseId: e.target.value || null })} className={selCls} style={selStyle(!!value.phaseId)}>
                    <option value="">Keine Phase (optional)</option>
                    {phases.filter((p) => p.status === "active" || p.status === "pending").map((p) => (<option key={p.id} value={p.id}>{p.status === "active" ? "● " : "○ "}{p.title}</option>))}
                  </select>
                </div>
              )}
              {value.projectId && deliverables && deliverables.length > 0 && (
                <div className="flex flex-col gap-1.5">
                  <label htmlFor={`${fieldId}-deliverable`} className="text-[10px]" style={{ color: C.textMuted }}>Basiert auf</label>
                  <select id={`${fieldId}-deliverable`} aria-label="Basiert auf Deliverable" value={value.deliverableId ?? ""} onChange={(e) => patch({ deliverableId: e.target.value || null })} className={selCls} style={selStyle(!!value.deliverableId)}>
                    <option value="">Kein Deliverable</option>
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
                {sectionHead("Reference files")}
                <label
                  className="flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-[11px] cursor-pointer transition-colors self-start"
                  style={{ border: `1px dashed ${C.border}`, color: C.textMuted }}
                >
                  <Paperclip size={11} />
                  Add files
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
                      aria-label="Note for reference files"
                      value={referenceNote}
                      onChange={(e) => updateReferenceNote(e.target.value)}
                      placeholder="Note for the agent (optional)"
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
              {sectionHead("Zuweisung")}
              <div className="flex flex-col gap-1.5">
                <label htmlFor={`${fieldId}-agent`} className="text-[10px]" style={{ color: C.textMuted }}><Users size={10} className="inline mr-1" />Agent</label>
                <div className="flex items-center gap-2">
                  {selectedAgent && <span className="w-2 h-2 rounded-full shrink-0" style={{ background: C.online }} />}
                  <select id={`${fieldId}-agent`} aria-label="Agent" value={value.selectedAgentId ?? ""} onChange={(e) => patch({ selectedAgentId: e.target.value || null })} className={selCls} style={selStyle(!!selectedAgent)}>
                    <option value="">Auto — bester verfügbarer</option>
                    {availableAgents.map((a) => (<option key={a.id} value={a.id}>{a.name} — {a.role ?? "Agent"}</option>))}
                  </select>
                </div>
              </div>
              <div className="flex flex-col gap-1.5">
                <span className="text-[10px]" style={{ color: C.textMuted }}>Priorität</span>
                <div className="flex items-center gap-1">
                  {PRIORITY_OPTIONS.map((opt) => (
                    <button key={opt.value} type="button" onClick={() => patch({ priority: opt.value })} aria-label={`Priorität ${opt.label}`} aria-pressed={value.priority === opt.value}
                      className="w-7 h-7 flex items-center justify-center rounded-lg text-[10px] font-bold transition-all cursor-pointer" style={pill(value.priority === opt.value, opt.color)}>
                      {opt.label}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            {/* Ausführung */}
            <div className="flex flex-col gap-3">
              {sectionHead("Ausführung")}
              <div className="flex flex-col gap-1.5">
                <span className="text-[10px]" style={{ color: C.textMuted }}>Typ</span>
                <div className="flex items-center gap-1.5 flex-wrap">
                  {TASK_TYPE_OPTIONS.map((opt) => (
                    <button key={opt.value} type="button" onClick={() => patch({ taskType: opt.value })} aria-pressed={value.taskType === opt.value}
                      className="px-2.5 py-1 text-[11px] font-medium rounded-full transition-all cursor-pointer" style={pill(value.taskType === opt.value, C.accent)}>
                      {opt.label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="flex flex-col gap-1.5">
                <label htmlFor={`${fieldId}-deadline`} className="text-[10px]" style={{ color: C.textMuted }}><Calendar size={10} className="inline mr-1" />Deadline</label>
                <input id={`${fieldId}-deadline`} type="date" aria-label="Deadline" value={value.dueAt} onChange={(e) => patch({ dueAt: e.target.value })}
                  className="w-full text-[11px] px-2.5 py-1.5 rounded-lg outline-none cursor-pointer" style={{ background: C.deep, border: `1px solid ${value.dueAt ? `${C.accent}55` : C.border}`, color: value.dueAt ? C.textPrimary : C.textMuted, colorScheme: "dark" }} />
              </div>
              <PlannerSlider value={value.plannerMode} onChange={(m) => patch({ plannerMode: m })} accent={C.accent} textMuted={C.textMuted} textSecondary={C.textSecondary} border={C.border} />
            </div>

            {workspacePreview && (
              <div className="flex items-start gap-2 px-3 py-2 rounded-lg font-mono text-[10px]" style={{ background: `${C.accent}08`, border: `1px solid ${C.accent}22` }}>
                <FolderKanban size={11} style={{ color: C.accent, flexShrink: 0, marginTop: 1 }} />
                <div className="min-w-0">
                  <span style={{ color: C.textMuted }}>Arbeitsplatz: </span>
                  <span style={{ color: C.textPrimary, wordBreak: "break-all" }}>{workspacePreview}</span>
                </div>
              </div>
            )}

            {/* Erweitert (progressive disclosure replaces the Schnell/Strukturiert toggle) */}
            <div className="flex flex-col gap-3" style={{ borderTop: `1px solid ${C.borderSubtle}`, paddingTop: "14px" }}>
              <button type="button" onClick={() => setAdvancedExpanded((v) => !v)} aria-expanded={advancedExpanded} aria-controls={`${fieldId}-advanced`}
                className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] transition-colors cursor-pointer self-start" style={{ color: advancedExpanded ? C.accent : C.textMuted }}>
                {advancedExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                <Settings2 size={11} />Erweitert
                {!advancedExpanded && (<span className="normal-case tracking-normal font-normal" style={{ color: C.textMuted }}>· Approval · Auth · URLs · Intake</span>)}
              </button>
              <AnimatePresence>
                {advancedExpanded && (
                  <motion.div id={`${fieldId}-advanced`} initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} transition={{ duration: 0.2 }} className="flex flex-col gap-4 overflow-hidden">
                    <div className="flex flex-col gap-1.5">
                      <label htmlFor={`${fieldId}-approval`} className="text-[10px]" style={{ color: C.textMuted }}>Approval</label>
                      <select id={`${fieldId}-approval`} aria-label="Approval-Policy" value={value.approvalPolicy} onChange={(e) => patch({ approvalPolicy: e.target.value })} className={selCls} style={selStyle(!!value.approvalPolicy)}>
                        {APPROVAL_OPTIONS.map((opt) => (<option key={opt.value} value={opt.value}>{opt.label}</option>))}
                      </select>
                    </div>
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <button type="button" onClick={() => patch({ needsBrowser: !value.needsBrowser })} aria-pressed={value.needsBrowser} className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full transition-all cursor-pointer" style={pill(value.needsBrowser, C.info)}><Globe size={11} />Browser</button>
                      <button type="button" onClick={() => patch({ requiresAuth: !value.requiresAuth })} aria-pressed={value.requiresAuth} className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full transition-all cursor-pointer" style={pill(value.requiresAuth, C.warning)}><KeyRound size={11} />Auth</button>
                      <button type="button" onClick={() => patch({ reportBack: !value.reportBack })} aria-pressed={value.reportBack} className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full transition-all cursor-pointer" style={pill(value.reportBack, C.online)}><MessageSquare size={11} />Report-Back</button>
                      <button type="button" onClick={() => patch({ e2eTestRequired: !value.e2eTestRequired })} aria-pressed={value.e2eTestRequired} title="After review, a tester agent drives the real user flows in a browser before the task can complete" className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full transition-all cursor-pointer" style={pill(value.e2eTestRequired, C.accent)}><MousePointerClick size={11} />E2E test</button>
                      <button type="button" onClick={() => patch({ humanReviewRequired: !value.humanReviewRequired })} aria-pressed={value.humanReviewRequired} title="You review this task yourself instead of a review agent" className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full transition-all cursor-pointer" style={pill(value.humanReviewRequired, C.accent)}><UserCheck size={11} />Human review</button>
                      <button type="button" onClick={() => patch({ blockerToOperator: !value.blockerToOperator })} aria-pressed={value.blockerToOperator} title="Blockers on this task come straight to you instead of going to Boss first" className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full transition-all cursor-pointer" style={pill(value.blockerToOperator, C.warning)}><BellRing size={11} />Blocker to me</button>
                    </div>
                    <AnimatePresence>
                      {value.requiresAuth && (
                        <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} transition={{ duration: 0.15 }} className="flex flex-col gap-2 overflow-hidden pl-2" style={{ borderLeft: `2px solid ${C.warning}33` }}>
                          <div className="flex items-center gap-2">
                            <button type="button" onClick={() => patch({ credentialMode: "vault" })} className="px-2 py-0.5 text-[10px] rounded-full cursor-pointer" style={pill(value.credentialMode === "vault", C.warning)}>Aus Vault</button>
                            <button type="button" onClick={() => patch({ credentialMode: "inline" })} className="px-2 py-0.5 text-[10px] rounded-full cursor-pointer" style={pill(value.credentialMode === "inline", C.warning)}>Einmalig eingeben</button>
                          </div>
                          {value.credentialMode === "vault" && (
                            <select aria-label="Credential auswählen" value={value.credentialId ?? ""} onChange={(e) => patch({ credentialId: e.target.value || null })} className={selCls} style={selStyle(!!value.credentialId)}>
                              <option value="">Credential wählen...</option>
                              {(vaultCredentials ?? []).map((c) => (<option key={c.id} value={c.id}>{c.name} ({c.credential_type})</option>))}
                            </select>
                          )}
                          {value.credentialMode === "inline" && (
                            <textarea aria-label="Inline-Credentials" value={value.inlineCredentials} onChange={(e) => patch({ inlineCredentials: e.target.value })} placeholder="Username: admin&#10;Password: ..." rows={2} className="w-full text-[11px] px-3 py-2 rounded-xl outline-none resize-none font-mono" style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }} />
                          )}
                        </motion.div>
                      )}
                    </AnimatePresence>
                    <AnimatePresence>
                      {value.reportBack && (
                        <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} transition={{ duration: 0.15 }} className="flex flex-col gap-2 overflow-hidden pl-2" style={{ borderLeft: `2px solid ${C.online}33` }}>
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="text-[10px]" style={{ color: C.textMuted }}>Kanal:</span>
                            {["discord", "telegram"].map((ch) => (<button key={ch} type="button" onClick={() => patch({ reportChannel: ch })} className="px-2 py-0.5 text-[10px] rounded-full cursor-pointer" style={pill(value.reportChannel === ch, C.online)}>{ch.charAt(0).toUpperCase() + ch.slice(1)}</button>))}
                          </div>
                          <div className="flex items-center gap-1.5 flex-wrap">
                            <span className="text-[10px]" style={{ color: C.textMuted }}>Format:</span>
                            {[{ value: "summary", label: "Summary" }, { value: "screenshot", label: "Screenshot" }, { value: "before_after", label: "Before/After" }].map((fmt) => (<button key={fmt.value} type="button" onClick={() => toggleReportFormat(fmt.value)} className="px-2 py-0.5 text-[10px] rounded-full cursor-pointer" style={pill(value.reportFormats.includes(fmt.value), C.online)}>{fmt.label}</button>))}
                          </div>
                        </motion.div>
                      )}
                    </AnimatePresence>
                    <div className="flex flex-col gap-1.5">
                      <span className="text-[10px]" style={{ color: C.textMuted }}>Referenz-URLs</span>
                      <UrlListInput value={value.referenceUrls} onChange={(urls) => patch({ referenceUrls: urls })} textPrimary={C.textPrimary} textMuted={C.textMuted} border={C.border} deep={C.deep} accent={C.accent} />
                    </div>
                    <div className="flex flex-col gap-3" style={{ borderTop: `1px solid ${C.borderSubtle}`, paddingTop: "10px" }}>
                      <span className="text-[10px] font-medium flex items-center gap-1.5" style={{ color: C.textMuted }}><ClipboardList size={11} />Operator-Intake</span>
                      <div className="flex flex-col gap-1.5">
                        <label htmlFor={`${fieldId}-requestkind`} className="text-[10px]" style={{ color: C.textMuted }}>Auftragstyp</label>
                        <select id={`${fieldId}-requestkind`} aria-label="Auftragstyp" value={value.requestKind} onChange={(e) => patch({ requestKind: e.target.value })} className={selCls} style={selStyle(!!value.requestKind)}>
                          {REQUEST_KIND_OPTIONS.map((opt) => (<option key={opt.value} value={opt.value}>{opt.label}</option>))}
                        </select>
                      </div>
                      <div className="flex flex-col gap-1.5">
                        <label htmlFor={`${fieldId}-autonomy`} className="text-[10px]" style={{ color: C.textMuted }}>Autonomie</label>
                        <select id={`${fieldId}-autonomy`} aria-label="Autonomie-Level" value={value.autonomyLevel} onChange={(e) => patch({ autonomyLevel: e.target.value })} className={selCls} style={selStyle(!!value.autonomyLevel)}>
                          {AUTONOMY_OPTIONS.map((opt) => (<option key={opt.value} value={opt.value}>{opt.label}</option>))}
                        </select>
                      </div>
                      <textarea aria-label="Gewünschtes Ergebnis" value={value.desiredOutput} onChange={(e) => patch({ desiredOutput: e.target.value })} placeholder="Was soll am Ende rauskommen? (PR, Screenshot, Deployment-URL ...)" rows={2} className="w-full text-[12px] outline-none px-3 py-2 rounded-xl resize-none" style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }} />
                      <textarea aria-label="Referenz-Notizen" value={value.referenceNotes} onChange={(e) => patch({ referenceNotes: e.target.value })} placeholder="Referenz-Notizen — Vorlagen, Inspirationen ..." rows={2} className="w-full text-[12px] outline-none px-3 py-2 rounded-xl resize-none" style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }} />
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-[10px]" style={{ color: C.textMuted }}>Veröffentlichung:</span>
                        <button type="button" onClick={() => patch({ publishAllowed: value.publishAllowed === true ? null : true })} className="px-2.5 py-1 text-[11px] font-medium rounded-full transition-all cursor-pointer" style={pill(value.publishAllowed === true, C.online)}>Erlaubt</button>
                        <button type="button" onClick={() => patch({ publishAllowed: value.publishAllowed === false ? null : false })} className="px-2.5 py-1 text-[11px] font-medium rounded-full transition-all cursor-pointer" style={pill(value.publishAllowed === false, C.warning)}>Nur Draft</button>
                        {value.publishAllowed === null && (<span className="text-[10px]" style={{ color: C.textMuted }}>(Agent entscheidet)</span>)}
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
          Vorlage:
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
              className="flex items-center gap-1 px-2.5 py-1 text-[10px] font-medium rounded-full transition-all cursor-pointer"
              style={{
                color: active ? t.color : C.textMuted,
                background: active ? `${t.color}15` : "transparent",
                border: `1px solid ${active ? `${t.color}55` : C.border}`,
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
          Titel <span style={{ color: C.error }}>*</span>
        </label>
        <input
          id={`${fieldId}-title`}
          ref={titleRef}
          type="text"
          required
          value={value.title}
          onChange={(e) => patch({ title: e.target.value })}
          onKeyDown={handleKeyDown}
          placeholder="Kurzer, klarer Aufgabentitel"
          className="w-full text-sm outline-none px-3 py-2.5 rounded-xl transition-all"
          style={{
            border: `1px solid ${C.border}`,
            color: C.textPrimary,
            backgroundColor: C.deep,
          }}
          onFocus={(e) => { e.target.style.borderColor = `${C.accent}66`; e.target.style.boxShadow = `0 0 16px ${C.accent}15`; }}
          onBlur={(e) => { e.target.style.borderColor = C.border; e.target.style.boxShadow = "none"; }}
          disabled={disabled}
        />
      </div>

      {/* ── Description ── */}
      <div className="flex flex-col gap-1">
        <label htmlFor={`${fieldId}-description`} className="text-[10px] font-medium" style={{ color: C.textMuted }}>
          Beschreibung <span style={{ color: C.textMuted }}>(optional)</span>
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
          onFocus={(e) => { e.target.style.borderColor = `${C.accent}66`; e.target.style.boxShadow = `0 0 16px ${C.accent}15`; }}
          onBlur={(e) => { e.target.style.borderColor = C.border; e.target.style.boxShadow = "none"; }}
          disabled={disabled}
        />
      </div>

      {/* ── Priority + Mode-Toggle (Row) ── */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <span className="text-[10px]" style={{ color: C.textMuted }}>Prioritaet:</span>
          <div className="flex items-center gap-1">
            {PRIORITY_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                type="button"
                onClick={() => patch({ priority: opt.value })}
                className="w-6 h-6 flex items-center justify-center rounded-full text-[10px] font-bold transition-all cursor-pointer"
                style={{
                  backgroundColor: value.priority === opt.value ? `${opt.color}22` : "transparent",
                  color: value.priority === opt.value ? opt.color : C.textMuted,
                  border: value.priority === opt.value ? `1px solid ${opt.color}66` : "1px solid transparent",
                }}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>

        {/* Mode toggle */}
        <div
          className="flex items-center rounded-full p-0.5"
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
                className="flex items-center gap-1 px-2.5 py-1 text-[10px] font-medium rounded-full transition-all cursor-pointer"
                style={{
                  color: active ? C.accent : C.textMuted,
                  background: active ? `${C.accent}14` : "transparent",
                }}
              >
                <Icon size={10} />
                {m === "schnell" ? "Schnell" : "Strukturiert"}
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
            background: `${C.accent}08`,
            border: `1px solid ${C.accent}22`,
            color: C.textSecondary,
          }}
        >
          <FolderKanban size={11} style={{ color: C.accent, flexShrink: 0 }} />
          <span style={{ color: C.textMuted }}>Arbeitsplatz:</span>
          <code style={{ color: C.textPrimary }}>{workspacePreview}</code>
        </motion.div>
      )}

      {/* ── SCHNELL-MODE: Kompakter Auth-Toggle ── */}
      {mode === "schnell" && (vaultCredentials ?? []).length > 0 && (
        <div className="flex flex-col gap-2">
          <button
            type="button"
            onClick={() => patch({ requiresAuth: !value.requiresAuth })}
            className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full transition-all cursor-pointer self-start"
            style={{
              backgroundColor: value.requiresAuth ? `${C.warning}22` : "transparent",
              color: value.requiresAuth ? C.warning : C.textMuted,
              border: value.requiresAuth ? `1px solid ${C.warning}66` : `1px solid ${C.border}`,
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
                style={{ borderLeft: `2px solid ${C.warning}33` }}
              >
                <select
                  value={value.credentialId ?? ""}
                  onChange={(e) => patch({ credentialId: e.target.value || null, credentialMode: "vault" })}
                  className="text-[11px] px-2.5 py-1.5 rounded-lg outline-none cursor-pointer"
                  style={{
                    background: C.deep,
                    border: `1px solid ${value.credentialId ? `${C.warning}66` : C.border}`,
                    color: value.credentialId ? C.warning : C.textMuted,
                  }}
                >
                  <option value="">Credential waehlen...</option>
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
                aria-label="Phase"
                value={value.phaseId ?? ""}
                onChange={(e) => patch({ phaseId: e.target.value || null })}
                className="flex-1 text-[11px] px-2.5 py-1.5 rounded-lg outline-none cursor-pointer"
                style={{
                  background: C.deep,
                  border: `1px solid ${value.phaseId ? `${C.accent}66` : C.border}`,
                  color: value.phaseId ? C.accent : C.textMuted,
                }}
              >
                <option value="">Keine Phase (optional)</option>
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
              <span className="text-[10px] shrink-0 w-20" style={{ color: C.textMuted }}>Basiert auf:</span>
              <select
                aria-label="Basiert auf Deliverable"
                value={value.deliverableId ?? ""}
                onChange={(e) => patch({ deliverableId: e.target.value || null })}
                className="flex-1 text-[11px] px-2.5 py-1.5 rounded-lg outline-none cursor-pointer"
                style={{
                  background: C.deep,
                  border: `1px solid ${value.deliverableId ? `${C.accent}66` : C.border}`,
                  color: value.deliverableId ? C.accent : C.textMuted,
                }}
              >
                <option value="">Kein Deliverable</option>
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
            <span className="text-[10px] shrink-0 w-20" style={{ color: C.textMuted }}>Typ:</span>
            <div className="flex items-center gap-1.5">
              {TASK_TYPE_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => patch({ taskType: opt.value })}
                  className="px-2.5 py-1 text-[11px] font-medium rounded-full transition-all cursor-pointer"
                  style={{
                    backgroundColor: value.taskType === opt.value ? `${C.accent}22` : "transparent",
                    color: value.taskType === opt.value ? C.accent : C.textMuted,
                    border: `1px solid ${value.taskType === opt.value ? `${C.accent}66` : C.border}`,
                  }}
                >
                  {opt.label}
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
                  aria-label="Acceptance Criteria"
                  value={value.acceptanceCriteria}
                  onChange={(e) => patch({ acceptanceCriteria: e.target.value })}
                  placeholder={currentTemplate?.prefill.acceptancePlaceholder ?? "Acceptance Criteria — was muss erfuellt sein?"}
                  rows={2}
                  className="w-full text-[12px] outline-none px-3 py-2 rounded-xl resize-none"
                  style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }}
                />
                <textarea
                  aria-label="Scope-Out"
                  value={value.scopeOut}
                  onChange={(e) => patch({ scopeOut: e.target.value })}
                  placeholder="Scope-Out — was gehoert NICHT dazu?"
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
              aria-label="Deadline"
              value={value.dueAt}
              onChange={(e) => patch({ dueAt: e.target.value })}
              className="text-[11px] px-2.5 py-1.5 rounded-lg outline-none cursor-pointer"
              style={{
                background: C.deep,
                border: `1px solid ${value.dueAt ? `${C.accent}66` : C.border}`,
                color: value.dueAt ? C.textPrimary : C.textMuted,
                colorScheme: "dark",
              }}
            />
          </div>

          <textarea
            aria-label="Risiken"
            value={value.riskNotes}
            onChange={(e) => patch({ riskNotes: e.target.value })}
            placeholder="Risiken — was darf nicht kaputtgehen?"
            rows={2}
            className="w-full text-[12px] outline-none px-3 py-2 rounded-xl resize-none"
            style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }}
          />

          <div className="flex flex-col gap-1">
            <span className="text-[10px]" style={{ color: C.textMuted }}>Referenz-URLs:</span>
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
            <span className="text-[10px] shrink-0 w-20" style={{ color: C.textMuted }}>Approval:</span>
            <select
              aria-label="Approval-Policy"
              value={value.approvalPolicy}
              onChange={(e) => patch({ approvalPolicy: e.target.value })}
              className="text-[11px] px-2.5 py-1.5 rounded-lg outline-none cursor-pointer"
              style={{
                background: C.deep,
                border: `1px solid ${value.approvalPolicy ? `${C.accent}66` : C.border}`,
                color: value.approvalPolicy ? C.accent : C.textMuted,
              }}
            >
              {APPROVAL_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>

          {/* Toggles row */}
          <div className="flex items-center gap-2 flex-wrap">
            <button
              type="button"
              onClick={() => patch({ needsBrowser: !value.needsBrowser })}
              className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full transition-all cursor-pointer"
              style={{
                backgroundColor: value.needsBrowser ? `${C.info}22` : "transparent",
                color: value.needsBrowser ? C.info : C.textMuted,
                border: value.needsBrowser ? `1px solid ${C.info}66` : `1px solid ${C.border}`,
              }}
            >
              <Globe size={11} />
              Browser
            </button>
            <button
              type="button"
              onClick={() => patch({ e2eTestRequired: !value.e2eTestRequired })}
              title="After review, a tester agent drives the real user flows in a browser before the task can complete"
              className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full transition-all cursor-pointer"
              style={{
                backgroundColor: value.e2eTestRequired ? `${C.accent}22` : "transparent",
                color: value.e2eTestRequired ? C.accent : C.textMuted,
                border: value.e2eTestRequired ? `1px solid ${C.accent}66` : `1px solid ${C.border}`,
              }}
            >
              <MousePointerClick size={11} />
              E2E test
            </button>
            <button
              type="button"
              onClick={() => patch({ humanReviewRequired: !value.humanReviewRequired })}
              title="You review this task yourself instead of a review agent"
              className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full transition-all cursor-pointer"
              style={{
                backgroundColor: value.humanReviewRequired ? `${C.accent}22` : "transparent",
                color: value.humanReviewRequired ? C.accent : C.textMuted,
                border: value.humanReviewRequired ? `1px solid ${C.accent}66` : `1px solid ${C.border}`,
              }}
            >
              <UserCheck size={11} />
              Human review
            </button>
            <button
              type="button"
              onClick={() => patch({ blockerToOperator: !value.blockerToOperator })}
              title="Blockers on this task come straight to you instead of going to Boss first"
              className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full transition-all cursor-pointer"
              style={{
                backgroundColor: value.blockerToOperator ? `${C.warning}22` : "transparent",
                color: value.blockerToOperator ? C.warning : C.textMuted,
                border: value.blockerToOperator ? `1px solid ${C.warning}66` : `1px solid ${C.border}`,
              }}
            >
              <BellRing size={11} />
              Blocker to me
            </button>
            <button
              type="button"
              onClick={() => patch({ requiresAuth: !value.requiresAuth })}
              className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full transition-all cursor-pointer"
              style={{
                backgroundColor: value.requiresAuth ? `${C.warning}22` : "transparent",
                color: value.requiresAuth ? C.warning : C.textMuted,
                border: value.requiresAuth ? `1px solid ${C.warning}66` : `1px solid ${C.border}`,
              }}
            >
              <KeyRound size={11} />
              Auth
            </button>
            <button
              type="button"
              onClick={() => patch({ reportBack: !value.reportBack })}
              className="flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full transition-all cursor-pointer"
              style={{
                backgroundColor: value.reportBack ? `${C.online}22` : "transparent",
                color: value.reportBack ? C.online : C.textMuted,
                border: value.reportBack ? `1px solid ${C.online}66` : `1px solid ${C.border}`,
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
                style={{ borderLeft: `2px solid ${C.warning}33` }}
              >
                <div className="flex items-center gap-3">
                  <button
                    type="button"
                    onClick={() => patch({ credentialMode: "vault" })}
                    className="px-2 py-0.5 text-[10px] rounded-full cursor-pointer"
                    style={{
                      backgroundColor: value.credentialMode === "vault" ? `${C.warning}22` : "transparent",
                      color: value.credentialMode === "vault" ? C.warning : C.textMuted,
                      border: `1px solid ${value.credentialMode === "vault" ? `${C.warning}66` : C.border}`,
                    }}
                  >
                    Aus Vault
                  </button>
                  <button
                    type="button"
                    onClick={() => patch({ credentialMode: "inline" })}
                    className="px-2 py-0.5 text-[10px] rounded-full cursor-pointer"
                    style={{
                      backgroundColor: value.credentialMode === "inline" ? `${C.warning}22` : "transparent",
                      color: value.credentialMode === "inline" ? C.warning : C.textMuted,
                      border: `1px solid ${value.credentialMode === "inline" ? `${C.warning}66` : C.border}`,
                    }}
                  >
                    Einmalig eingeben
                  </button>
                </div>

                {value.credentialMode === "vault" && (
                  <select
                    aria-label="Credential auswählen"
                    value={value.credentialId ?? ""}
                    onChange={(e) => patch({ credentialId: e.target.value || null })}
                    className="text-[11px] px-2.5 py-1.5 rounded-lg outline-none cursor-pointer"
                    style={{
                      background: C.deep,
                      border: `1px solid ${value.credentialId ? `${C.warning}66` : C.border}`,
                      color: value.credentialId ? C.warning : C.textMuted,
                    }}
                  >
                    <option value="">Credential waehlen...</option>
                    {(vaultCredentials ?? []).map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name} ({c.credential_type})
                      </option>
                    ))}
                  </select>
                )}

                {value.credentialMode === "inline" && (
                  <textarea
                    aria-label="Inline-Credentials"
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
                style={{ borderLeft: `2px solid ${C.online}33` }}
              >
                <div className="flex items-center gap-3">
                  <span className="text-[10px]" style={{ color: C.textMuted }}>Kanal:</span>
                  {["discord", "telegram"].map((ch) => (
                    <button
                      key={ch}
                      type="button"
                      onClick={() => patch({ reportChannel: ch })}
                      className="px-2 py-0.5 text-[10px] rounded-full cursor-pointer"
                      style={{
                        backgroundColor: value.reportChannel === ch ? `${C.online}22` : "transparent",
                        color: value.reportChannel === ch ? C.online : C.textMuted,
                        border: `1px solid ${value.reportChannel === ch ? `${C.online}66` : C.border}`,
                      }}
                    >
                      {ch.charAt(0).toUpperCase() + ch.slice(1)}
                    </button>
                  ))}
                </div>
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-[10px]" style={{ color: C.textMuted }}>Format:</span>
                  {[
                    { value: "summary", label: "Summary" },
                    { value: "screenshot", label: "Screenshot" },
                    { value: "before_after", label: "Before/After" },
                  ].map((fmt) => (
                    <button
                      key={fmt.value}
                      type="button"
                      onClick={() => toggleReportFormat(fmt.value)}
                      className="px-2 py-0.5 text-[10px] rounded-full cursor-pointer"
                      style={{
                        backgroundColor: value.reportFormats.includes(fmt.value) ? `${C.online}22` : "transparent",
                        color: value.reportFormats.includes(fmt.value) ? C.online : C.textMuted,
                        border: `1px solid ${value.reportFormats.includes(fmt.value) ? `${C.online}66` : C.border}`,
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
              Operator-Intake
              {!intakeExpanded && (
                <span style={{ color: C.textMuted, fontWeight: 400 }}>
                  (Auftragstyp, Autonomie, Ergebnis-Format)
                </span>
              )}
            </button>

            <AnimatePresence>
              {intakeExpanded && (
                <motion.div
                  id="operator-intake-panel"
                  role="region"
                  aria-label="Operator-Intake Details"
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: "auto" }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: 0.15 }}
                  className="flex flex-col gap-3 overflow-hidden pt-3"
                >
                  <div className="flex items-center gap-2">
                    <span className="text-[10px] shrink-0 w-20" style={{ color: C.textMuted }}>Auftragstyp:</span>
                    <select
                      aria-label="Auftragstyp"
                      value={value.requestKind}
                      onChange={(e) => patch({ requestKind: e.target.value })}
                      className="flex-1 text-[11px] px-2.5 py-1.5 rounded-lg outline-none cursor-pointer"
                      style={{
                        background: C.deep,
                        border: `1px solid ${value.requestKind ? `${C.accent}66` : C.border}`,
                        color: value.requestKind ? C.accent : C.textMuted,
                      }}
                    >
                      {REQUEST_KIND_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                      ))}
                    </select>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-[10px] shrink-0 w-20" style={{ color: C.textMuted }}>Autonomie:</span>
                    <select
                      aria-label="Autonomie-Level"
                      value={value.autonomyLevel}
                      onChange={(e) => patch({ autonomyLevel: e.target.value })}
                      className="flex-1 text-[11px] px-2.5 py-1.5 rounded-lg outline-none cursor-pointer"
                      style={{
                        background: C.deep,
                        border: `1px solid ${value.autonomyLevel ? `${C.accent}66` : C.border}`,
                        color: value.autonomyLevel ? C.accent : C.textMuted,
                      }}
                    >
                      {AUTONOMY_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                      ))}
                    </select>
                  </div>
                  <textarea
                    aria-label="Gewünschtes Ergebnis"
                  value={value.desiredOutput}
                    onChange={(e) => patch({ desiredOutput: e.target.value })}
                    placeholder="Was soll am Ende rauskommen? (PR, Screenshot, Deployment-URL ...)"
                    rows={2}
                    className="w-full text-[12px] outline-none px-3 py-2 rounded-xl resize-none"
                    style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }}
                  />
                  <textarea
                    aria-label="Referenz-Notizen"
                  value={value.referenceNotes}
                    onChange={(e) => patch({ referenceNotes: e.target.value })}
                    placeholder="Referenz-Notizen — Vorlagen, Inspirationen ..."
                    rows={2}
                    className="w-full text-[12px] outline-none px-3 py-2 rounded-xl resize-none"
                    style={{ border: `1px solid ${C.border}`, color: C.textPrimary, backgroundColor: C.deep }}
                  />
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[10px] shrink-0" style={{ color: C.textMuted }}>Veroeffentlichung:</span>
                    <button
                      type="button"
                      onClick={() => patch({ publishAllowed: value.publishAllowed === true ? null : true })}
                      className="px-2.5 py-1 text-[11px] font-medium rounded-full transition-all cursor-pointer"
                      style={{
                        backgroundColor: value.publishAllowed === true ? `${C.online}22` : "transparent",
                        color: value.publishAllowed === true ? C.online : C.textMuted,
                        border: `1px solid ${value.publishAllowed === true ? `${C.online}66` : C.border}`,
                      }}
                    >
                      Erlaubt
                    </button>
                    <button
                      type="button"
                      onClick={() => patch({ publishAllowed: value.publishAllowed === false ? null : false })}
                      className="px-2.5 py-1 text-[11px] font-medium rounded-full transition-all cursor-pointer"
                      style={{
                        backgroundColor: value.publishAllowed === false ? `${C.warning}22` : "transparent",
                        color: value.publishAllowed === false ? C.warning : C.textMuted,
                        border: `1px solid ${value.publishAllowed === false ? `${C.warning}66` : C.border}`,
                      }}
                    >
                      Nur Draft
                    </button>
                    {value.publishAllowed === null && (
                      <span className="text-[10px]" style={{ color: C.textMuted }}>(Agent entscheidet)</span>
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
