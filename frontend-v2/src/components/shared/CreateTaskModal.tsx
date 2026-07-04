"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import { X, Send, Plus, Bug, Sparkles, Search as SearchIcon, AlertTriangle } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import type { Agent, Task } from "@/lib/types";
import {
  TaskFormFields,
  EMPTY_TASK_FORM_PAYLOAD,
  type TaskFormPayload,
  type StagedReferenceFile,
} from "./TaskFormFields";
import { C as MC } from "@/components/homepage/colors";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";

// ── Design tokens — sourced from the shared MC palette (single source, no purple)
const C = {
  deep: MC.bgDeep,
  elevated: MC.bgElevated,
  border: MC.border,
  borderSubtle: MC.borderSubtle,
  accent: MC.accent,
  accentHover: MC.accentHover,
  info: MC.info,
  error: MC.error,
  warning: MC.warning,
  textPrimary: MC.textPrimary,
  textMuted: MC.textMuted,
};


// Template metadata duplicated here only for the header chip — the actual
// template-prefill logic lives in TaskFormFields. Keep in sync with the
// `TEMPLATES` map there.
const TEMPLATE_CHIP_META: Record<string, { label: string; icon: typeof Bug; color: string }> = {
  bug:      { label: "Bug Fix",  icon: Bug,        color: C.error },
  feature:  { label: "Feature",  icon: Sparkles,   color: C.accent },
  research: { label: "Research", icon: SearchIcon, color: C.info },
};

interface CreateTaskModalProps {
  activeBoardId: string | null;
  agents: Agent[] | undefined;
}

export function CreateTaskModal({ activeBoardId, agents }: CreateTaskModalProps) {
  const qc = useQueryClient();

  // Modal state
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);

  // Refs for a11y: focus-trap + initial focus target
  const dialogRef = useRef<HTMLDivElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);
  const prefersReducedMotion = useReducedMotion();
  const titleRef = useRef<HTMLInputElement>(null);
  const descriptionRef = useRef<HTMLTextAreaElement>(null);

  // Single payload state — was 30+ individual useState calls before.
  const [payload, setPayload] = useState<TaskFormPayload>(EMPTY_TASK_FORM_PAYLOAD);

  // Reference files (ADR-053) — staged in TaskFormFields, mirrored here so
  // handleSubmit can upload them once the task (and its task_id) exists.
  const [stagedReferenceFiles, setStagedReferenceFiles] = useState<StagedReferenceFile[]>([]);
  const [referenceNote, setReferenceNote] = useState("");
  const [referenceUploadErrors, setReferenceUploadErrors] = useState<string[]>([]);

  // Auto-resize description textarea on open (matches old behavior).
  useEffect(() => {
    if (!open) return;
    const t = setTimeout(() => {
      const el = descriptionRef.current;
      if (el) {
        el.style.height = "auto";
        el.style.height = `${el.scrollHeight}px`;
      }
    }, 10);
    return () => clearTimeout(t);
  }, [open]);

  // Focus management: capture currently-focused element, focus title input on open,
  // restore focus on close.
  useEffect(() => {
    if (!open) return;
    previouslyFocused.current = document.activeElement as HTMLElement | null;
    const t = setTimeout(() => titleRef.current?.focus(), 80);
    return () => {
      clearTimeout(t);
      previouslyFocused.current?.focus?.();
    };
  }, [open]);

  // Minimal focus trap: keep Tab/Shift-Tab inside the dialog when open.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const root = dialogRef.current;
      if (!root) return;
      const focusables = root.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
      );
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  // No Schnell/Strukturiert toggle anymore — derive intake mode from whether
  // the user actually engaged structured/advanced fields.
  const isStructured =
    payload.plannerMode === "with_planner" ||
    !!payload.acceptanceCriteria.trim() ||
    !!payload.scopeOut.trim() ||
    !!payload.requestKind ||
    !!payload.autonomyLevel ||
    !!payload.desiredOutput.trim();

  const resetForm = useCallback(() => {
    setPayload(EMPTY_TASK_FORM_PAYLOAD);
    setStagedReferenceFiles([]);
    setReferenceNote("");
    setReferenceUploadErrors([]);
    if (descriptionRef.current) descriptionRef.current.style.height = "auto";
    setOpen(false);
  }, []);

  const handleSubmit = useCallback(async () => {
    if (!activeBoardId || !payload.title.trim() || loading) return;
    setLoading(true);
    try {
      const apiPayload: Partial<Task> = {
        title: payload.title.trim(),
        ...(payload.description.trim() && { description: payload.description.trim() }),
        status: "inbox" as Task["status"],
        priority: payload.priority as Task["priority"],
        task_type: payload.taskType as Task["task_type"],
        planner_mode: payload.plannerMode,
        intake_mode: (isStructured ? "structured" : "quick") as Task["intake_mode"],
        use_separate_repo: payload.projectId ? false : payload.useSeparateRepo,
        ...(!payload.projectId && payload.repoId && { repo_id: payload.repoId }),
        ...(payload.selectedAgentId && { assigned_agent_id: payload.selectedAgentId }),
        ...(payload.projectId && { project_id: payload.projectId }),
        ...(payload.phaseId && { phase_id: payload.phaseId }),
        ...(payload.branchName && { branch_name: payload.branchName }),
        ...(payload.deliverableId && { triggered_by_deliverable_id: payload.deliverableId }),
        ...(payload.acceptanceCriteria.trim() && { acceptance_criteria: payload.acceptanceCriteria.trim() }),
        ...(payload.scopeOut.trim() && { scope_out: payload.scopeOut.trim() }),
        ...(payload.dueAt && { due_at: new Date(payload.dueAt).toISOString() }),
        ...(payload.riskNotes.trim() && { risk_notes: payload.riskNotes.trim() }),
        ...(payload.referenceUrls.length > 0 && { reference_urls: payload.referenceUrls }),
        ...(payload.approvalPolicy && { approval_policy: payload.approvalPolicy as Task["approval_policy"] }),
        ...(payload.needsBrowser && { needs_browser: true }),
        ...(payload.requiresAuth && { requires_auth: true }),
        ...(payload.requiresAuth && payload.credentialMode === "vault" && payload.credentialId && { credential_id: payload.credentialId }),
        ...(payload.requiresAuth && payload.credentialMode === "inline" && payload.inlineCredentials.trim() && { credentials: payload.inlineCredentials.trim() }),
        ...(payload.reportBack && {
          report_back_required: true,
          ...(payload.reportChannel && { report_back_channel: payload.reportChannel }),
          ...(payload.reportFormats.length > 0 && { report_back_requirements: payload.reportFormats.join(",") }),
        }),
        ...(payload.requestKind && { request_kind: payload.requestKind as Task["request_kind"] }),
        ...(payload.autonomyLevel && { autonomy_level: payload.autonomyLevel as Task["autonomy_level"] }),
        ...(payload.desiredOutput.trim() && { desired_output: payload.desiredOutput.trim() }),
        ...(payload.referenceNotes.trim() && { reference_notes: payload.referenceNotes.trim() }),
        ...(payload.publishAllowed !== null && { publish_allowed: payload.publishAllowed }),
      };

      const created = await api.tasks.create(activeBoardId, apiPayload);
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["pipeline"] });
      notify.success("Task created");

      if (stagedReferenceFiles.length > 0) {
        const failures: string[] = [];
        for (const staged of stagedReferenceFiles) {
          try {
            await api.references.upload({ taskId: created.id }, staged.file, referenceNote.trim() || undefined);
          } catch (err) {
            const msg = err instanceof Error && err.message ? err.message : "Upload failed";
            failures.push(`${staged.file.name}: ${msg}`);
          }
        }
        qc.invalidateQueries({ queryKey: ["references", "task", created.id] });
        if (failures.length > 0) {
          // Task already exists — keep the modal open so the operator sees
          // which uploads failed instead of silently closing on them.
          setReferenceUploadErrors(failures);
          return;
        }
      }

      resetForm();
    } catch (err) {
      const msg = err instanceof Error && err.message ? err.message : "Failed to create";
      notify.error(msg);
    } finally {
      setLoading(false);
    }
  }, [activeBoardId, payload, loading, isStructured, qc, resetForm, stagedReferenceFiles, referenceNote]);

  // iOS-safe scroll lock (M4)
  useBodyScrollLock(open);

  const currentTemplate = payload.activeTemplate ? TEMPLATE_CHIP_META[payload.activeTemplate] : null;

  return (
    <>
      {/* Trigger — unchanged style, icon-only (just the +); accessible label + tooltip. */}
      <button
        onClick={() => setOpen(true)}
        disabled={!activeBoardId}
        aria-label="New task"
        title="New task"
        className="flex items-center justify-center min-h-touch min-w-touch rounded-lg transition-all cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
        style={{
          color: C.accent,
          border: `1px solid ${C.accent}44`,
          backgroundColor: `${C.accent}0A`,
        }}
      >
        <Plus size={14} />
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={prefersReducedMotion ? false : { opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={prefersReducedMotion ? { opacity: 1 } : { opacity: 0 }}
            transition={{ duration: prefersReducedMotion ? 0 : 0.15 }}
            className="fixed inset-0 z-50 flex items-end sm:items-center justify-center sm:p-4"
            style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
            onClick={(e) => { if (e.target === e.currentTarget) resetForm(); }}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.preventDefault();
                resetForm();
              } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                handleSubmit();
              }
            }}
          >
            <div
              className="absolute inset-0"
              style={{ backgroundColor: "rgba(0,0,0,0.6)" }}
            />

            {/* Drag indicator — mobile only */}
            <div className="sm:hidden absolute bottom-[calc(92dvh-0.5rem)] left-1/2 -translate-x-1/2 z-10 w-8 h-1 rounded-full" style={{ backgroundColor: "rgba(255,255,255,0.2)" }} />

            <motion.div
              ref={dialogRef}
              role="dialog"
              aria-modal="true"
              aria-labelledby="create-task-title"
              initial={prefersReducedMotion ? false : { opacity: 0, y: 24 }}
              animate={{ opacity: 1, y: 0 }}
              exit={prefersReducedMotion ? { opacity: 1 } : { opacity: 0, y: 24 }}
              transition={{ duration: prefersReducedMotion ? 0 : 0.22, ease: [0.16, 1, 0.3, 1] }}
              className="relative w-full mx-2 rounded-t-2xl rounded-b-none sm:mx-0 sm:max-w-[880px] sm:rounded-2xl overflow-hidden max-h-[92dvh] sm:max-h-[88vh] flex flex-col"
              style={{
                background: C.elevated,
                border: "1px solid rgba(255,255,255,0.08)",
                boxShadow: `0 0 60px rgba(15,163,163,0.10), 0 25px 80px rgba(0,0,0,0.6)`,
              }}
            >
              {/* Top edge highlight */}
              <div className="absolute top-0 left-0 right-0 h-px" style={{ background: "linear-gradient(90deg, transparent, rgba(255,255,255,0.12), transparent)" }} />

              {/* Header */}
              <div className="flex items-center justify-between px-5 py-3.5 shrink-0" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
                <div className="flex items-center gap-2">
                  <span id="create-task-title" className="text-sm font-semibold" style={{ color: C.textPrimary }}>New task</span>
                  {currentTemplate && (
                    <span
                      className="flex items-center gap-1 px-1.5 py-0.5 rounded text-[9px] font-medium"
                      style={{
                        color: currentTemplate.color,
                        background: `${currentTemplate.color}18`,
                        border: `1px solid ${currentTemplate.color}33`,
                      }}
                    >
                      <currentTemplate.icon size={9} />
                      {currentTemplate.label}
                    </span>
                  )}
                </div>
                <button onClick={resetForm} aria-label="Close" className="cursor-pointer hover:opacity-80 transition-opacity" style={{ color: C.textMuted }}>
                  <X size={16} />
                </button>
              </div>

              {/* Reference upload banner — task already exists at this point,
                  so we keep the modal open instead of silently discarding it. */}
              {referenceUploadErrors.length > 0 && (
                <div
                  className="flex items-start gap-2 px-5 py-2.5 text-[11px] shrink-0"
                  style={{ background: `${C.warning}12`, borderBottom: `1px solid ${C.warning}33`, color: C.warning }}
                >
                  <AlertTriangle size={12} className="shrink-0 mt-0.5" />
                  <span>
                    Task created, but {referenceUploadErrors.length} reference upload{referenceUploadErrors.length > 1 ? "s" : ""} failed: {referenceUploadErrors.join("; ")}
                  </span>
                </div>
              )}

              {/* Body — delegated to TaskFormFields */}
              <div className="p-5 overflow-y-auto flex-1">
                <TaskFormFields
                  value={payload}
                  onChange={setPayload}
                  activeBoardId={activeBoardId}
                  agents={agents}
                  layout="two-pane"
                  open={open}
                  disabled={loading}
                  titleRef={titleRef}
                  descriptionRef={descriptionRef}
                  onSubmitShortcut={handleSubmit}
                  onEscape={resetForm}
                  enableReferenceFiles
                  onStagedReferenceFilesChange={(files, note) => {
                    setStagedReferenceFiles(files);
                    setReferenceNote(note);
                  }}
                />
              </div>

              {/* Footer */}
              <div className="flex items-center justify-between px-5 py-3.5 shrink-0" style={{ borderTop: `1px solid ${C.borderSubtle}` }}>
                <span className="text-[10px]" style={{ color: C.textMuted }}>
                  Cmd+Enter = create · Esc = close
                </span>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={resetForm}
                    className="px-3.5 py-1.5 text-[11px] rounded-lg cursor-pointer transition-colors"
                    style={{ color: C.textMuted, border: `1px solid ${C.border}` }}
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={handleSubmit}
                    disabled={!payload.title.trim() || loading}
                    className="flex items-center gap-1.5 px-3.5 py-1.5 text-[11px] font-semibold rounded-lg cursor-pointer transition-all disabled:opacity-30 disabled:cursor-not-allowed"
                    style={{
                      background: `linear-gradient(135deg, ${C.accentHover}, ${C.accent})`,
                      color: "#fff",
                      boxShadow: `0 0 16px ${C.accent}30`,
                    }}
                  >
                    <Send size={11} />
                    {loading ? "..." : "Create task"}
                  </button>
                </div>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}
