"use client";

/**
 * JobModal — create / edit a scheduled job.
 *
 * Sections (top → bottom):
 *   0. (create only) Template chips
 *   1. Job Meta
 *   2. Trigger (TriggerEditor + firing preview)
 *   3. Task-Vorlage (collapsed by default, embeds TaskFormFields)
 *   4. Erweitert (collapsed)
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import {
  X,
  ChevronDown,
  ChevronRight,
  Sparkles,
  Settings2,
  Loader2,
  AlertTriangle,
} from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type {
  Agent,
  Board,
  ScheduledJob,
  ScheduledJobCreate,
} from "@/lib/types";
import {
  TaskFormFields,
  EMPTY_TASK_FORM_PAYLOAD,
  type TaskFormPayload,
} from "@/components/shared/TaskFormFields";
import { TriggerEditor, type TriggerEditorValue } from "./TriggerEditor";
import { JOB_TEMPLATES, type JobTemplate } from "./jobTemplates";
import { C, STATUS_TEXT } from "@/lib/colors";

interface JobModalProps {
  open: boolean;
  onClose: () => void;
  job?: ScheduledJob;
  activeBoardId: string;
  agents: Agent[];
  boards: Board[];
  onSuccess: (job: ScheduledJob) => void;
}

interface FormState {
  name: string;
  description: string;
  enabled: boolean;
  tags: string;
  schedule_type: string;
  schedule_time: string | null;
  schedule_cron: string | null;
  schedule_weekdays: number[] | null;
  schedule_interval_hours: number | null;
  start_date: string | null;
  end_date: string | null;
  retry_max: number;
  retry_delay_minutes: number;
  depends_on_job_id: string | null;
  notify_on_failure: boolean;
  discord_channel_id: string | null;
}

function jobToForm(job: ScheduledJob): FormState {
  return {
    name: job.name,
    description: job.description ?? "",
    enabled: job.enabled,
    tags: (job.tags ?? []).join(", "),
    schedule_type: job.schedule_type,
    schedule_time: job.schedule_time,
    schedule_cron: job.schedule_cron ?? null,
    schedule_weekdays: job.schedule_weekdays ?? null,
    schedule_interval_hours: job.schedule_interval_hours,
    start_date: job.start_date ?? null,
    end_date: job.end_date ?? null,
    retry_max: job.retry_max,
    retry_delay_minutes: job.retry_delay_minutes,
    depends_on_job_id: job.depends_on_job_id,
    notify_on_failure: job.notify_on_failure,
    discord_channel_id: job.discord_channel_id,
  };
}

const DEFAULT_FORM: FormState = {
  name: "",
  description: "",
  enabled: true,
  tags: "",
  schedule_type: "daily",
  schedule_time: "09:00",
  schedule_cron: null,
  schedule_weekdays: null,
  schedule_interval_hours: null,
  start_date: null,
  end_date: null,
  retry_max: 0,
  retry_delay_minutes: 5,
  depends_on_job_id: null,
  notify_on_failure: false,
  discord_channel_id: null,
};

export function JobModal({
  open,
  onClose,
  job,
  activeBoardId,
  agents,
  boards: _boards,
  onSuccess,
}: JobModalProps) {
  const editing = !!job;
  const qc = useQueryClient();

  const [form, setForm] = useState<FormState>(() =>
    job ? jobToForm(job) : DEFAULT_FORM,
  );
  const [taskPayload, setTaskPayload] = useState<TaskFormPayload>(() => {
    if (job?.task_payload) {
      return {
        ...EMPTY_TASK_FORM_PAYLOAD,
        ...(job.task_payload as Partial<TaskFormPayload>),
        title: job.task_title ?? "",
        // Authoritative source for skip_review is the top-level job column
        skipReview: job.task_skip_review ?? false,
      };
    }
    return {
      ...EMPTY_TASK_FORM_PAYLOAD,
      title: job?.task_title ?? "",
      priority: job?.task_priority ?? "medium",
      skipReview: job?.task_skip_review ?? false,
    };
  });

  const [taskExpanded, setTaskExpanded] = useState(false);
  const [advancedExpanded, setAdvancedExpanded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset form when reopened
  useEffect(() => {
    if (!open) return;
    setError(null);
    if (job) {
      setForm(jobToForm(job));
      setTaskPayload({
        ...EMPTY_TASK_FORM_PAYLOAD,
        ...((job.task_payload ?? {}) as Partial<TaskFormPayload>),
        title: job.task_title ?? "",
        priority: job.task_priority ?? "medium",
        // Authoritative source for skip_review is the top-level job column
        skipReview: job.task_skip_review ?? false,
      });
      setTaskExpanded(true);
    } else {
      setForm(DEFAULT_FORM);
      setTaskPayload(EMPTY_TASK_FORM_PAYLOAD);
      setTaskExpanded(false);
    }
    setAdvancedExpanded(false);
  }, [open, job]);

  // Patcher for trigger editor
  const patchTrigger = (p: Partial<TriggerEditorValue>) => {
    setForm((prev) => ({
      ...prev,
      ...(p.schedule_type !== undefined ? { schedule_type: p.schedule_type } : {}),
      ...(p.schedule_time !== undefined ? { schedule_time: p.schedule_time ?? null } : {}),
      ...(p.schedule_cron !== undefined ? { schedule_cron: p.schedule_cron ?? null } : {}),
      ...(p.schedule_weekdays !== undefined ? { schedule_weekdays: p.schedule_weekdays ?? null } : {}),
      ...(p.schedule_interval_hours !== undefined ? { schedule_interval_hours: p.schedule_interval_hours ?? null } : {}),
      ...(p.start_date !== undefined ? { start_date: p.start_date ?? null } : {}),
      ...(p.end_date !== undefined ? { end_date: p.end_date ?? null } : {}),
    }));
  };

  // Firing preview (debounced via React Query staleTime)
  const previewParams = useMemo(() => {
    if (form.schedule_type === "cron" && form.schedule_cron?.trim()) {
      return {
        cron: form.schedule_cron.trim(),
        schedule_type: "cron",
        count: 5,
      };
    }
    if (form.schedule_type === "daily" && form.schedule_time) {
      return {
        schedule_type: "daily",
        schedule_time: form.schedule_time,
        count: 5,
      };
    }
    if (
      (form.schedule_type === "weekdays" || form.schedule_type === "weekly_custom") &&
      form.schedule_time
    ) {
      return {
        schedule_type: form.schedule_type,
        schedule_time: form.schedule_time,
        schedule_weekdays: form.schedule_weekdays ?? undefined,
        count: 5,
      };
    }
    if (form.schedule_type === "interval" && form.schedule_interval_hours) {
      return {
        schedule_type: "interval",
        schedule_interval_hours: form.schedule_interval_hours,
        count: 5,
      };
    }
    return null;
  }, [
    form.schedule_type,
    form.schedule_cron,
    form.schedule_time,
    form.schedule_weekdays,
    form.schedule_interval_hours,
  ]);

  const [debouncedParams, setDebouncedParams] = useState(previewParams);
  useEffect(() => {
    const t = setTimeout(() => setDebouncedParams(previewParams), 500);
    return () => clearTimeout(t);
  }, [previewParams]);

  const previewQuery = useQuery({
    queryKey: ["schedulePreview", debouncedParams],
    queryFn: () =>
      api.schedule.previewFirings(
        debouncedParams as Parameters<typeof api.schedule.previewFirings>[0],
      ),
    enabled: !!debouncedParams && open,
    staleTime: 30_000,
  });

  // Fetch other jobs for "Depends on" select
  const otherJobsQuery = useQuery({
    queryKey: ["scheduleJobs"],
    queryFn: () => api.schedule.listJobs(),
    enabled: open && advancedExpanded,
  });
  const otherJobs = (otherJobsQuery.data ?? []).filter((j) => j.id !== job?.id);

  // ── Mutations ──
  const buildPayload = (): ScheduledJobCreate => {
    const tags = form.tags
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s.length > 0);

    return {
      name: form.name.trim(),
      description: form.description.trim() || undefined,
      enabled: form.enabled,
      schedule_type: form.schedule_type as ScheduledJobCreate["schedule_type"],
      schedule_time: form.schedule_time ?? undefined,
      schedule_interval_hours: form.schedule_interval_hours ?? undefined,
      schedule_cron: form.schedule_cron ?? null,
      schedule_weekdays: form.schedule_weekdays ?? null,
      start_date: form.start_date ?? null,
      end_date: form.end_date ?? null,
      action_type: "create_task",
      agent_id: taskPayload.selectedAgentId ?? undefined,
      task_board_id: activeBoardId,
      task_title: taskPayload.title.trim() || form.name.trim(),
      task_priority: taskPayload.priority,
      task_skip_review: taskPayload.skipReview ?? false,
      task_payload: {
        ...(taskPayload as unknown as Record<string, unknown>),
        // Forward-compat: scheduler.py reads payload["skip_review"] before
        // falling back to job.task_skip_review (the authoritative column).
        skip_review: taskPayload.skipReview ?? false,
      },
      tags,
      retry_max: form.retry_max,
      retry_delay_minutes: form.retry_delay_minutes,
      depends_on_job_id: form.depends_on_job_id ?? undefined,
      notify_on_failure: form.notify_on_failure,
      discord_channel_id: form.discord_channel_id ?? undefined,
    };
  };

  const createMutation = useMutation({
    mutationFn: (payload: ScheduledJobCreate) => api.schedule.createJob(payload),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["scheduleJobs"] });
      onSuccess(data);
      onClose();
    },
    onError: (e) => setError((e as Error).message),
  });

  const updateMutation = useMutation({
    mutationFn: (payload: ScheduledJobCreate) =>
      api.schedule.updateJob(job!.id, payload),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["scheduleJobs"] });
      onSuccess(data);
      onClose();
    },
    onError: (e) => setError((e as Error).message),
  });

  const submitting = createMutation.isPending || updateMutation.isPending;

  const handleSubmit = () => {
    setError(null);
    if (!form.name.trim()) {
      setError("Name is required.");
      return;
    }
    const payload = buildPayload();
    if (editing) updateMutation.mutate(payload);
    else createMutation.mutate(payload);
  };

  const applyTemplate = (tpl: JobTemplate) => {
    setForm((prev) => ({
      ...prev,
      name: prev.name || tpl.name,
      schedule_type: tpl.defaults.schedule_type ?? prev.schedule_type,
      schedule_time: tpl.defaults.schedule_time ?? prev.schedule_time,
      schedule_cron: tpl.defaults.schedule_cron ?? prev.schedule_cron ?? null,
      schedule_weekdays:
        tpl.defaults.schedule_weekdays ?? prev.schedule_weekdays ?? null,
      schedule_interval_hours:
        tpl.defaults.schedule_interval_hours ?? prev.schedule_interval_hours,
      tags: tpl.defaults.tags?.join(", ") ?? prev.tags,
    }));
  };

  // iOS-safe scroll lock (M4)
  useBodyScrollLock(open);

  // Esc to close
  const overlayRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !submitting) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose, submitting]);

  // Shared input class — Tailwind tokens for bg/border/text
  const inputCls = "w-full rounded-md px-3 py-2 text-sm outline-none";
  const inputStyle = {
    background: C.bgDeep,
    border: `1px solid ${C.border}`,
    color: C.textPrimary,
  };
  const inputFocusStyle = { borderColor: C.borderAccent };

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          ref={overlayRef}
          key="overlay"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
          className="fixed inset-0 z-50 flex items-end sm:items-center justify-center sm:p-4 bg-black/70"
          style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
          onClick={(e) => {
            if (e.target === overlayRef.current && !submitting) onClose();
          }}
        >
          {/* Drag indicator — mobile only */}
          <div className="sm:hidden absolute bottom-[calc(92dvh-0.5rem)] left-1/2 -translate-x-1/2 z-10 w-8 h-1 rounded-full pointer-events-none" style={{ backgroundColor: "rgba(255,255,255,0.18)" }} />

          <motion.div
            key="modal"
            role="dialog"
            aria-modal="true"
            aria-label={editing ? "Edit job" : "New job"}
            initial={{ opacity: 0, y: 32 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 32 }}
            transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
            className="relative w-full mx-2 rounded-t-2xl rounded-b-none sm:mx-0 sm:max-w-[800px] sm:rounded-2xl overflow-hidden max-h-[92dvh] sm:max-h-[90vh] flex flex-col"
            style={{
              border: `1px solid ${C.border}`,
              background: C.bgBase,
              boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
            }}
          >
            {/* Header */}
            <div className="flex items-center justify-between px-6 py-4 shrink-0" style={{ borderBottom: `1px solid ${C.border}` }}>
              <div className="flex flex-col gap-0.5">
                <h2 className="text-lg font-semibold" style={{ color: C.textPrimary }}>
                  {editing ? "Edit job" : "New job"}
                </h2>
                <p className="text-xs" style={{ color: C.textMuted }}>
                  {editing
                    ? job?.name
                    : "Choose a template or create a custom configuration."}
                </p>
              </div>
              <button
                type="button"
                onClick={onClose}
                disabled={submitting}
                className="rounded-md p-1.5 transition"
                style={{ color: C.textMuted }}
              >
                <X size={16} />
              </button>
            </div>

            {/* Body */}
            <div className="flex flex-1 flex-col gap-6 overflow-y-auto px-6 py-5">
              {/* Templates */}
              {!editing && (
                <Section title="Templates" icon={<Sparkles size={12} />}>
                  <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
                    {JOB_TEMPLATES.map((tpl) => (
                      <button
                        key={tpl.id}
                        type="button"
                        onClick={() => applyTemplate(tpl)}
                        className="group flex flex-col items-start gap-1 rounded-lg p-3 text-left transition"
                        style={{
                          border: `1px solid ${C.border}`,
                          background: C.bgSurface,
                        }}
                      >
                        <span className="text-base">{tpl.icon}</span>
                        <span className="text-sm font-medium" style={{ color: C.textPrimary }}>
                          {tpl.name}
                        </span>
                        <span className="text-[10px] leading-snug" style={{ color: C.textMuted }}>
                          {tpl.description}
                        </span>
                      </button>
                    ))}
                  </div>
                </Section>
              )}

              {/* Job Meta */}
              <Section title="Job">
                <div className="flex flex-col gap-3">
                  <Label text="Name *">
                    <input
                      type="text"
                      value={form.name}
                      onChange={(e) =>
                        setForm((p) => ({ ...p, name: e.target.value }))
                      }
                      placeholder="Daily Standup"
                      className={inputCls}
                      style={inputStyle}
                    />
                  </Label>
                  <Label text="Description">
                    <textarea
                      value={form.description}
                      onChange={(e) =>
                        setForm((p) => ({ ...p, description: e.target.value }))
                      }
                      rows={2}
                      placeholder="What does this job do?"
                      className="w-full resize-none rounded-md px-3 py-2 text-sm outline-none"
                      style={inputStyle}
                    />
                  </Label>
                  <Label text="Tags (comma-separated)">
                    <input
                      type="text"
                      value={form.tags}
                      onChange={(e) =>
                        setForm((p) => ({ ...p, tags: e.target.value }))
                      }
                      placeholder="morning-routine, monitoring"
                      className={inputCls}
                      style={inputStyle}
                    />
                  </Label>
                  <label className="flex cursor-pointer items-center gap-2 text-sm" style={{ color: C.textSecondary }}>
                    <input
                      type="checkbox"
                      checked={form.enabled}
                      onChange={(e) =>
                        setForm((p) => ({ ...p, enabled: e.target.checked }))
                      }
                      className="h-3.5 w-3.5 cursor-pointer"
                      style={{ accentColor: C.accent }}
                    />
                    Job active
                  </label>
                </div>
              </Section>

              {/* Trigger */}
              <Section title="Trigger">
                <TriggerEditor
                  schedule_type={form.schedule_type}
                  schedule_time={form.schedule_time ?? undefined}
                  schedule_cron={form.schedule_cron ?? undefined}
                  schedule_weekdays={form.schedule_weekdays ?? undefined}
                  schedule_interval_hours={form.schedule_interval_hours ?? undefined}
                  start_date={form.start_date ?? undefined}
                  end_date={form.end_date ?? undefined}
                  onChange={patchTrigger}
                  firingPreview={previewQuery.data?.firings}
                />
              </Section>

              {/* Task Template */}
              <CollapsibleSection
                title="Task Template"
                expanded={taskExpanded}
                onToggle={() => setTaskExpanded((v) => !v)}
                hint={taskPayload.title || "(not configured yet)"}
              >
                <div className="flex flex-col gap-3 pt-2">
                  <Label text="Task title (template)">
                    <input
                      type="text"
                      value={taskPayload.title}
                      onChange={(e) =>
                        setTaskPayload((p) => ({ ...p, title: e.target.value }))
                      }
                      placeholder="Compile standup briefing"
                      className={inputCls}
                      style={inputStyle}
                    />
                  </Label>
                  <TaskFormFields
                    value={taskPayload}
                    onChange={setTaskPayload}
                    activeBoardId={activeBoardId}
                    agents={agents}
                    open={taskExpanded}
                  />
                </div>
              </CollapsibleSection>

              {/* Advanced */}
              <CollapsibleSection
                title="Advanced"
                icon={<Settings2 size={12} />}
                expanded={advancedExpanded}
                onToggle={() => setAdvancedExpanded((v) => !v)}
                hint="Retry, Dependencies, Notifications"
              >
                <div className="flex flex-col gap-3 pt-2">
                  <div className="grid grid-cols-2 gap-3">
                    <Label text="Retry max (0–5)">
                      <input
                        type="number"
                        min={0}
                        max={5}
                        value={form.retry_max}
                        onChange={(e) =>
                          setForm((p) => ({
                            ...p,
                            retry_max: Math.max(
                              0,
                              Math.min(5, Number(e.target.value)),
                            ),
                          }))
                        }
                        className={inputCls}
                        style={inputStyle}
                      />
                    </Label>
                    <Label text="Retry delay (min)">
                      <input
                        type="number"
                        min={0}
                        value={form.retry_delay_minutes}
                        onChange={(e) =>
                          setForm((p) => ({
                            ...p,
                            retry_delay_minutes: Math.max(
                              0,
                              Number(e.target.value),
                            ),
                          }))
                        }
                        className={inputCls}
                        style={inputStyle}
                      />
                    </Label>
                  </div>

                  <Label text="Depends on (Job)">
                    <select
                      value={form.depends_on_job_id ?? ""}
                      onChange={(e) =>
                        setForm((p) => ({
                          ...p,
                          depends_on_job_id: e.target.value || null,
                        }))
                      }
                      className={inputCls}
                      style={inputStyle}
                    >
                      <option value="">— none —</option>
                      {otherJobs.map((j) => (
                        <option key={j.id} value={j.id}>
                          {j.name}
                        </option>
                      ))}
                    </select>
                  </Label>

                  <label className="flex cursor-pointer items-center gap-2 text-sm" style={{ color: C.textSecondary }}>
                    <input
                      type="checkbox"
                      checked={form.notify_on_failure}
                      onChange={(e) =>
                        setForm((p) => ({
                          ...p,
                          notify_on_failure: e.target.checked,
                        }))
                      }
                      className="h-3.5 w-3.5 cursor-pointer"
                      style={{ accentColor: C.accent }}
                    />
                    Telegram notification on failure
                  </label>

                  <Label text="Discord Channel ID (optional)">
                    <input
                      type="text"
                      value={form.discord_channel_id ?? ""}
                      onChange={(e) =>
                        setForm((p) => ({
                          ...p,
                          discord_channel_id: e.target.value || null,
                        }))
                      }
                      placeholder="123456789012345678"
                      className="w-full rounded-md px-3 py-2 font-mono text-xs outline-none"
                      style={inputStyle}
                    />
                  </Label>
                </div>
              </CollapsibleSection>

              {error && (
                <div
                  className="flex items-start gap-2 rounded-md border px-3 py-2 text-xs"
                  style={{
                    borderColor: `${C.error}66`,
                    background: `${C.error}14`,
                    color: STATUS_TEXT.error,
                  }}
                >
                  <AlertTriangle size={14} className="mt-0.5 shrink-0" />
                  <span>{error}</span>
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="flex items-center justify-end gap-2 px-6 py-3 shrink-0" style={{ borderTop: `1px solid ${C.border}`, paddingBottom: "calc(env(safe-area-inset-bottom) + 0.75rem)" }}>
              <button
                type="button"
                onClick={onClose}
                disabled={submitting}
                className="rounded-md px-3 py-1.5 text-sm transition"
                style={{
                  border: `1px solid ${C.borderActive}`,
                  color: C.textSecondary,
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleSubmit}
                disabled={submitting || !form.name.trim()}
                className="flex items-center gap-1.5 rounded-md px-3.5 py-1.5 text-sm font-medium transition disabled:opacity-60"
                style={{
                  background: C.accent,
                  color: C.textPrimary,
                }}
              >
                {submitting && <Loader2 size={14} className="animate-spin" />}
                {editing ? "Save changes" : "Create job"}
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

// ── Subcomponents ────────────────────────────────────────────────────

function Section({
  title,
  icon,
  children,
}: {
  title: string;
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="flex flex-col gap-2.5">
      <h3 className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider" style={{ color: C.textMuted }}>
        {icon}
        {title}
      </h3>
      {children}
    </section>
  );
}

function CollapsibleSection({
  title,
  icon,
  expanded,
  onToggle,
  children,
  hint,
}: {
  title: string;
  icon?: React.ReactNode;
  expanded: boolean;
  onToggle: () => void;
  children: React.ReactNode;
  hint?: string;
}) {
  return (
    <section
      className="flex flex-col gap-2 rounded-lg px-3 py-2.5"
      style={{
        border: `1px solid ${C.borderSubtle}`,
        background: C.bgSurface,
      }}
    >
      <button
        type="button"
        onClick={onToggle}
        className="flex items-center justify-between gap-3 text-left"
      >
        <span className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider" style={{ color: C.textSecondary }}>
          {expanded ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
          {icon}
          {title}
        </span>
        {!expanded && hint && (
          <span className="truncate text-[10px]" style={{ color: C.textDim }}>{hint}</span>
        )}
      </button>
      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.15 }}
            className="overflow-hidden"
          >
            {children}
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  );
}

function Label({
  text,
  children,
}: {
  text: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] uppercase tracking-wide" style={{ color: C.textDim }}>
        {text}
      </span>
      {children}
    </label>
  );
}
