"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import { X, Send, Plus, Bug, Sparkles, Search as SearchIcon, AlertTriangle, Play } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
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
import { ConfirmDialog } from "./ConfirmDialog";
import { HeadPairPicker, usePairReason } from "@/components/heads/HeadPairPicker";
import {
  chooseInitialPair,
  headErrorKey,
  loadRememberedPair,
  pairKey,
  saveRememberedPair,
  type HeadPairsResponse,
} from "@/lib/heads";

/** The pairs endpoint answers 404 `heads_disabled` while the launcher is off
 *  (and tests stub fetch with arrays) — only a real listing shows the section. */
function asPairsResponse(data: unknown): HeadPairsResponse | null {
  if (!data || typeof data !== "object" || !Array.isArray((data as HeadPairsResponse).pairs)) return null;
  return data as HeadPairsResponse;
}

// ── Design tokens — sourced from the shared MC palette (single source, no purple)
const C = {
  deep: MC.bgDeep,
  elevated: MC.bgElevated,
  border: MC.border,
  borderSubtle: MC.borderSubtle,
  accent: MC.accent,
  onAccent: MC.onAccent,
  info: MC.info,
  error: MC.error,
  warning: MC.warning,
  textPrimary: MC.textPrimary,
  textSecondary: MC.textSecondary,
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

// New, manually created tasks default to human review ON (Mark, 05.07.) —
// he reviews his own tasks unless he opts out. TaskFormFields' own default
// stays `false` so other consumers (e.g. JobModal-scheduled tasks) are unaffected.
const INITIAL_TASK_PAYLOAD: TaskFormPayload = {
  ...EMPTY_TASK_FORM_PAYLOAD,
  humanReviewRequired: true,
};

interface CreateTaskModalProps {
  activeBoardId: string | null;
  agents: Agent[] | undefined;
  /** Opens the task detail after "Run as head" (default: /tasks?task=<id>). */
  onOpenTask?: (taskId: string) => void;
}

function openTaskPage(taskId: string) {
  window.location.assign(`/tasks?task=${encodeURIComponent(taskId)}`);
}

export function CreateTaskModal({ activeBoardId, agents, onOpenTask = openTaskPage }: CreateTaskModalProps) {
  const qc = useQueryClient();
  const t = useTranslations("tasks.createModal");
  const tHeads = useTranslations("heads");
  const pairReason = usePairReason();

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
  const [payload, setPayload] = useState<TaskFormPayload>(INITIAL_TASK_PAYLOAD);

  // Reference files (ADR-053) — staged in TaskFormFields, mirrored here so
  // handleSubmit can upload them once the task (and its task_id) exists.
  const [stagedReferenceFiles, setStagedReferenceFiles] = useState<StagedReferenceFile[]>([]);
  const [referenceNote, setReferenceNote] = useState("");
  const [referenceUploadErrors, setReferenceUploadErrors] = useState<string[]>([]);
  // Dispatch-race fix (review C2): the task is created with `defer_dispatch`
  // when files are staged, so `createdTaskId` marks "task exists, uploads
  // may still be pending" — a retry state. `uploadedFileIds` tracks which
  // staged files already made it, so a retry only repeats the failed ones
  // instead of re-uploading (or re-creating the task, review M2).
  const [createdTaskId, setCreatedTaskId] = useState<string | null>(null);
  const [uploadedFileIds, setUploadedFileIds] = useState<Set<string>>(new Set());
  const isRetry = createdTaskId != null;

  // ── Head launcher (docs/specs/head-launcher.md §8.1) ──
  // Pairs load only while the modal is open. Heads off → 404 → no section.
  const pairsQuery = useQuery({
    queryKey: ["heads", "pairs"],
    queryFn: () => api.heads.pairs(),
    enabled: open,
    retry: false,
    staleTime: 10_000,
  });
  const pairsResp = asPairsResponse(pairsQuery.data);
  const headsAvailable = pairsResp != null;
  const [rememberedPair] = useState<string | null>(() => loadRememberedPair());
  const [pickedPairKey, setPickedPairKey] = useState<string | null>(null);
  const [headStartError, setHeadStartError] = useState<string | null>(null);
  // The card was created by "Run as head": from then on nothing in this
  // modal may hand it to the fleet (no dispatchDeferred, no "only create").
  const [launchedAsHead, setLaunchedAsHead] = useState(false);
  const [loadingAs, setLoadingAs] = useState<"task" | "head" | null>(null);
  const selectedPair = pairsResp
    ? ((pickedPairKey ? pairsResp.pairs.find((p) => pairKey(p) === pickedPairKey) : undefined) ??
      chooseInitialPair(pairsResp, rememberedPair))
    : null;

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
    setPayload(INITIAL_TASK_PAYLOAD);
    setStagedReferenceFiles([]);
    setReferenceNote("");
    setReferenceUploadErrors([]);
    setCreatedTaskId(null);
    setUploadedFileIds(new Set());
    setPickedPairKey(null);
    setHeadStartError(null);
    setLaunchedAsHead(false);
    if (descriptionRef.current) descriptionRef.current.style.height = "auto";
    setOpen(false);
  }, []);

  // Anything the operator typed (or staged) that closing would throw away.
  // Once the task exists (retry state) there is no draft left to lose.
  const hasDraft =
    !isRetry &&
    (!!payload.title.trim() ||
      !!payload.description.trim() ||
      !!payload.acceptanceCriteria.trim() ||
      !!payload.scopeOut.trim() ||
      !!payload.riskNotes.trim() ||
      !!payload.desiredOutput.trim() ||
      !!payload.referenceNotes.trim() ||
      payload.referenceUrls.length > 0 ||
      stagedReferenceFiles.length > 0);

  const [confirmDiscard, setConfirmDiscard] = useState(false);

  // Every close path (Esc, backdrop, X, Cancel) goes through here: an empty
  // form closes at once, a filled one asks "Discard draft?" first.
  const requestClose = useCallback(() => {
    if (hasDraft) setConfirmDiscard(true);
    else resetForm();
  }, [hasDraft, resetForm]);

  // A head works on a registry repo (task.repo_id) — project tasks carry
  // their repo elsewhere and are not supported by the launcher in v1.
  const headRepoId = !payload.projectId ? payload.repoId : null;
  const runBlockedReason: string | null = !headsAvailable
    ? null
    : !headRepoId
      ? payload.projectId
        ? tHeads("needsRepoProject")
        : tHeads("needsRepo")
      : !selectedPair
        ? tHeads("noPairs")
        : !selectedPair.startable
          ? pairReason(selectedPair)
          : null;
  const canRunHead = headsAvailable && runBlockedReason == null;

  const handleSubmit = useCallback(async (asHead: boolean = false) => {
    // Focus stays in the form behind "Discard draft?", so a Cmd/Ctrl+Enter
    // typed there must not create the task.
    if (confirmDiscard) return;
    if (loading || !activeBoardId) return;
    if (!isRetry && !payload.title.trim()) return;
    // Once started as a head, every retry stays a head start.
    if (launchedAsHead) asHead = true;
    if (asHead && (!canRunHead || !selectedPair)) return;
    setLoading(true);
    setLoadingAs(asHead ? "head" : "task");
    setHeadStartError(null);
    try {
      let taskId = createdTaskId;

      const hasStagedFiles = stagedReferenceFiles.length > 0;

      if (!taskId) {
        const apiPayload: Partial<Task> & { defer_dispatch?: boolean } = {
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
          ...(payload.e2eTestRequired && { e2e_test_required: true }),
          human_review_required: payload.humanReviewRequired,
          skip_review: payload.skipReview,
          ...(payload.blockerToOperator && { blocker_to_operator: true }),
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
          // Review C2: defer auto-dispatch when files are staged so the agent
          // brief isn't built before the uploads land — we dispatch ourselves
          // below, once every upload has succeeded.
          // A head start never goes through the fleet dispatch either.
          ...((hasStagedFiles || asHead) && { defer_dispatch: true }),
        };

        const created = await api.tasks.create(activeBoardId, apiPayload);
        if (asHead) setLaunchedAsHead(true);
        taskId = created.id;
        setCreatedTaskId(created.id);
        qc.invalidateQueries({ queryKey: ["tasks"] });
        qc.invalidateQueries({ queryKey: ["pipeline"] });
        notify.success(t("created"));
      }

      // Review M2: only retry files that haven't already succeeded — a
      // resubmit after a partial failure must not re-create the task or
      // re-upload files that already made it.
      const pending = stagedReferenceFiles.filter((f) => !uploadedFileIds.has(f.id));
      if (pending.length > 0) {
        const newlyUploaded: string[] = [];
        const failures: string[] = [];
        for (const staged of pending) {
          try {
            await api.references.upload({ taskId }, staged.file, referenceNote.trim() || undefined);
            newlyUploaded.push(staged.id);
          } catch (err) {
            const msg = err instanceof Error && err.message ? err.message : "Upload failed";
            failures.push(`${staged.file.name}: ${msg}`);
          }
        }
        if (newlyUploaded.length > 0) {
          setUploadedFileIds((prev) => new Set([...prev, ...newlyUploaded]));
          qc.invalidateQueries({ queryKey: ["references", "task", taskId] });
        }
        if (failures.length > 0) {
          // Task already exists — keep the modal open so the operator sees
          // which uploads failed and can retry, instead of silently closing.
          setReferenceUploadErrors(failures);
          return;
        }
      }

      if (asHead && selectedPair) {
        // "Run as head" = create task (deferred) → POST /heads → open the
        // detail. A failed start keeps the modal open in the retry state:
        // the task exists, the button retries only the start.
        try {
          await api.heads.start({
            task_id: taskId,
            harness: selectedPair.harness,
            runtime_slug: selectedPair.runtime_slug,
            // the card exists only for this head — held if the start fails
            hold_on_failure: true,
          });
        } catch (err) {
          setHeadStartError(tHeads("startFailed", { message: tHeads(headErrorKey(err)) }));
          qc.invalidateQueries({ queryKey: ["heads"] });
          return;
        }
        saveRememberedPair(pairKey(selectedPair));
        qc.invalidateQueries({ queryKey: ["heads"] });
        qc.invalidateQueries({ queryKey: ["tasks"] });
        qc.invalidateQueries({ queryKey: ["pipeline"] });
        notify.success(tHeads("started"));
        resetForm();
        onOpenTask(taskId);
        return;
      }

      if (hasStagedFiles && !asHead) {
        try {
          await api.tasks.dispatchDeferred(activeBoardId, taskId);
        } catch (err) {
          const msg = err instanceof Error ? err.message : "";
          // 409 = the task already moved on some other way (no longer
          // inbox/undispatched) — not our problem, everything else is.
          if (!msg.includes("409")) throw err;
        }
      }

      resetForm();
    } catch (err) {
      const msg = err instanceof Error && err.message ? err.message : t("createFailed");
      notify.error(msg);
    } finally {
      setLoading(false);
      setLoadingAs(null);
    }
  }, [activeBoardId, payload, loading, isStructured, qc, resetForm, stagedReferenceFiles, referenceNote, createdTaskId, uploadedFileIds, isRetry, confirmDiscard, canRunHead, selectedPair, t, tHeads, onOpenTask, launchedAsHead]);

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
            onClick={(e) => { if (e.target === e.currentTarget) requestClose(); }}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.preventDefault();
                // The discard dialog handles its own Esc (= keep editing).
                if (!confirmDiscard) requestClose();
              } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                handleSubmit(false);
              }
            }}
          >
            <div
              className="absolute inset-0"
              style={{ backgroundColor: "rgba(0,0,0,0.6)" }}
              onClick={requestClose}
            />

            {/* Drag indicator — mobile only */}
            <div className="sm:hidden absolute bottom-[calc(92dvh-0.5rem)] left-1/2 -translate-x-1/2 z-10 w-8 h-1 rounded-sm" style={{ backgroundColor: "var(--color-bg-hover)" }} />

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
                border: "1px solid var(--color-border)",
                // Kein farbiger Halo mehr: unter dem hellen Akzent wäre ein
                // 60px-Schein um den Dialog das lauteste Element der Seite,
                // und Farbe ist hier reserviert für Status. Tiefe kommt allein
                // aus dem Schlagschatten.
                boxShadow: `0 25px 80px rgba(0,0,0,0.6)`,
              }}
            >
              {/* Top edge — flat hairline (DESIGN.md: no gradients) */}
              <div className="absolute top-0 left-0 right-0 h-px" style={{ background: "var(--color-bg-hover)" }} />

              {/* Header */}
              <div className="flex items-center justify-between px-5 py-3.5 shrink-0" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
                <div className="flex items-center gap-2">
                  <span id="create-task-title" className="text-sm font-semibold" style={{ color: C.textPrimary }}>{t("title")}</span>
                  {currentTemplate && (
                    <span
                      className="flex items-center gap-1 px-1.5 py-0.5 rounded-sm text-[9px] font-medium"
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
                <button onClick={requestClose} aria-label={t("close")} className="flex items-center justify-center min-h-touch min-w-touch sm:min-h-0 sm:min-w-0 cursor-pointer hover:opacity-80 transition-opacity" style={{ color: C.textMuted }}>
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
                  <div className="flex flex-col gap-0.5">
                    <span>
                      Task created, but {referenceUploadErrors.length} reference upload{referenceUploadErrors.length > 1 ? "s" : ""} failed: {referenceUploadErrors.join("; ")}
                    </span>
                    <span style={{ opacity: 0.85 }}>
                      Task was created without the failed files. It has not been dispatched yet — dispatch it from the task detail.
                    </span>
                  </div>
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
                  disabled={loading || isRetry}
                  titleRef={titleRef}
                  descriptionRef={descriptionRef}
                  onSubmitShortcut={() => handleSubmit(false)}
                  enableReferenceFiles
                  onStagedReferenceFilesChange={(files, note) => {
                    setStagedReferenceFiles(files);
                    setReferenceNote(note);
                  }}
                />

                {pairsResp && (
                  <section className="mt-5" data-testid="head-section" aria-labelledby="create-task-head-label">
                    <div className="flex items-center gap-2.5 mb-3">
                      <span id="create-task-head-label" className="label-sys shrink-0">{tHeads("section")}</span>
                      <div className="flex-1 h-px" style={{ background: C.borderSubtle }} />
                    </div>
                    <HeadPairPicker
                      pairs={pairsResp.pairs}
                      selected={selectedPair}
                      defaultKey={pairsResp.default_pair ? pairKey(pairsResp.default_pair) : null}
                      onSelect={(p) => setPickedPairKey(pairKey(p))}
                      disabled={loading}
                    />
                    {!headRepoId && (
                      <p className="mt-1.5 sm:ml-[100px] text-[11px]" style={{ color: C.textMuted }} data-testid="head-needs-repo">
                        {runBlockedReason}
                      </p>
                    )}
                    {headStartError && (
                      <p role="alert" className="mt-2 sm:ml-[100px] text-[11px] flex items-start gap-1.5" style={{ color: C.error }} data-testid="head-start-error">
                        <AlertTriangle size={12} className="shrink-0 mt-0.5" />
                        <span>
                          {headStartError}{" "}
                          <span style={{ color: C.textSecondary }} data-testid="head-start-kept">{tHeads("keptHeld")}</span>
                        </span>
                      </p>
                    )}
                  </section>
                )}
              </div>

              {/* Footer — phone (< sm): one column, the main action full width,
                  the rest as text buttons below it; the keyboard hint only on
                  devices that can hover (a keyboard is likely there). */}
              <div
                className="flex flex-col-reverse sm:flex-row sm:items-center sm:justify-between gap-2 px-5 py-3.5 shrink-0"
                style={{ borderTop: `1px solid ${C.borderSubtle}` }}
                data-testid="create-task-footer"
              >
                <span className="hidden [@media(hover:hover)]:inline text-[10px]" style={{ color: C.textMuted }} data-testid="create-task-shortcut-hint">
                  {t("shortcutHint")}
                </span>
                <div className="flex flex-col-reverse sm:flex-row sm:items-center gap-2 w-full sm:w-auto">
                  <button
                    type="button"
                    onClick={requestClose}
                    className="hidden sm:inline-flex items-center justify-center px-3.5 py-1.5 text-[11px] rounded-md cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
                    style={{ color: C.textMuted, border: `1px solid ${C.border}` }}
                  >
                    {t("cancel")}
                  </button>
                  {headsAvailable && headRepoId ? (
                    <>
                      <button
                        type="button"
                        onClick={() => (launchedAsHead ? resetForm() : handleSubmit(false))}
                        disabled={(!isRetry && !payload.title.trim()) || loading}
                        data-testid="create-task-only"
                        className="inline-flex items-center justify-center min-h-[44px] sm:min-h-0 px-3.5 py-1.5 text-[11px] rounded-md cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)] disabled:opacity-30 disabled:cursor-not-allowed sm:border"
                        style={{ color: C.textSecondary, borderColor: C.border }}
                      >
                        {launchedAsHead ? tHeads("keepTask") : loadingAs === "task" ? "..." : isRetry ? t("retryUploads") : tHeads("onlyCreate")}
                      </button>
                      <button
                        type="button"
                        onClick={() => handleSubmit(true)}
                        disabled={(!isRetry && !payload.title.trim()) || loading || !canRunHead}
                        title={runBlockedReason ?? undefined}
                        data-testid="run-as-head"
                        className="inline-flex items-center justify-center gap-1.5 w-full sm:w-auto min-h-[44px] sm:min-h-0 px-3.5 py-1.5 text-[11px] font-semibold rounded-md cursor-pointer transition-colors hover:bg-[var(--color-accent-light)] disabled:opacity-30 disabled:cursor-not-allowed"
                        style={{ background: C.accent, color: C.onAccent }}
                      >
                        <Play size={11} aria-hidden />
                        {loadingAs === "head" ? tHeads("starting") : launchedAsHead ? tHeads("retryStart") : tHeads("runAsHead")}
                      </button>
                    </>
                  ) : (
                    <>
                      {headsAvailable && (
                        <button
                          type="button"
                          disabled
                          title={runBlockedReason ?? undefined}
                          data-testid="run-as-head"
                          className="hidden sm:inline-flex items-center justify-center gap-1.5 px-3.5 py-1.5 text-[11px] rounded-md disabled:opacity-30 disabled:cursor-not-allowed"
                          style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
                        >
                          <Play size={11} aria-hidden />
                          {tHeads("runAsHead")}
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => handleSubmit(false)}
                        disabled={(!isRetry && !payload.title.trim()) || loading}
                        data-testid="create-task-submit"
                        className="inline-flex items-center justify-center gap-1.5 w-full sm:w-auto min-h-[44px] sm:min-h-0 px-3.5 py-1.5 text-[11px] font-semibold rounded-md cursor-pointer transition-colors hover:bg-[var(--color-accent-light)] disabled:opacity-30 disabled:cursor-not-allowed"
                        style={{ background: C.accent, color: C.onAccent }}
                      >
                        <Send size={11} aria-hidden />
                        {loading ? "..." : isRetry ? t("retryUploads") : t("create")}
                      </button>
                    </>
                  )}
                </div>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      <ConfirmDialog
        open={confirmDiscard}
        kicker={t("discardKicker")}
        title={t("discardTitle")}
        body={t("discardBody")}
        confirmLabel={t("discardConfirm")}
        cancelLabel={t("discardKeep")}
        onConfirm={() => {
          setConfirmDiscard(false);
          resetForm();
        }}
        onCancel={() => setConfirmDiscard(false)}
      />
    </>
  );
}
