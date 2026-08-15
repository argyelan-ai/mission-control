"use client";

/**
 * CreateLoopDialog — create form for a new loop (ADR-051).
 *
 * Structured as the loop contract the runner actually executes:
 *   1. Auftrag       — name, board, goal
 *   2. Arbeitsquelle — backlog source as an explained radio list, the
 *                      source-specific input nested inside the selected row
 *   3. Leitplanken   — stop limits + gates, always visible (safety is not
 *                      an "advanced" concern), reporting toggle
 * A live mono summary strip above the footer restates the contract.
 */

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  FolderKanban,
  Infinity as InfinityIcon,
  ListChecks,
  Loader2,
  Tag as TagIcon,
} from "lucide-react";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { api } from "@/lib/api";
import { C, STATUS_TEXT } from "@/lib/colors";
import { useAppStore } from "@/lib/store";
import type { Board, Loop, LoopBacklogSource, LoopCreate } from "@/lib/types";

interface CreateLoopDialogProps {
  open: boolean;
  onClose: () => void;
  onCreated: (loop: Loop) => void;
}

interface FormState {
  name: string;
  boardId: string;
  goal: string;
  backlogSource: LoopBacklogSource;
  backlogMd: string;
  backlogTag: string;
  projectId: string;
  maxRounds: number;
  budgetUsd: string;
  budgetTokens: string;
  pauseOnFailedRounds: number;
  humanEveryNRounds: number;
  maxDurationMinutes: string;
  stopOnBacklogEmpty: boolean;
  telegramReports: boolean;
}

type FieldKey = "name" | "board" | "goal" | "source";

function defaultForm(boardId: string): FormState {
  return {
    name: "",
    boardId,
    goal: "",
    backlogSource: "markdown",
    backlogMd: "",
    backlogTag: "",
    projectId: "",
    maxRounds: 10,
    budgetUsd: "",
    budgetTokens: "",
    pauseOnFailedRounds: 2,
    humanEveryNRounds: 0,
    maxDurationMinutes: "",
    stopOnBacklogEmpty: true,
    telegramReports: true,
  };
}

const SOURCE_META: Record<
  LoopBacklogSource,
  { icon: typeof ListChecks; labelKey: string; descKey: string }
> = {
  markdown: { icon: ListChecks, labelKey: "backlogSourceMarkdown", descKey: "sourceMarkdownDesc" },
  project: { icon: FolderKanban, labelKey: "backlogSourceProject", descKey: "sourceProjectDesc" },
  tag: { icon: TagIcon, labelKey: "backlogSourceTag", descKey: "sourceTagDesc" },
  open_ended: { icon: InfinityIcon, labelKey: "backlogSourceOpenEnded", descKey: "sourceOpenEndedDesc" },
};

const SOURCE_ORDER: LoopBacklogSource[] = ["markdown", "project", "tag", "open_ended"];

export function CreateLoopDialog({ open, onClose, onCreated }: CreateLoopDialogProps) {
  const t = useTranslations("loops.create");
  const qc = useQueryClient();
  const activeBoardId = useAppStore((s) => s.activeBoardId);
  const [form, setForm] = useState<FormState>(() => defaultForm(activeBoardId ?? ""));
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<FieldKey, string>>>({});
  const [serverError, setServerError] = useState<string | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) {
      setForm(defaultForm(activeBoardId ?? ""));
      setFieldErrors({});
      setServerError(null);
    }
  }, [open, activeBoardId]);

  const { data: boards = [] } = useQuery({
    queryKey: ["boards"],
    queryFn: () => api.boards.list(),
    enabled: open,
  });

  const { data: projects = [] } = useQuery({
    queryKey: ["projects", form.boardId],
    queryFn: () => api.projects.list(form.boardId),
    enabled: open && form.backlogSource === "project" && !!form.boardId,
  });

  const createMutation = useMutation({
    mutationFn: (payload: LoopCreate) => api.loops.create(payload),
    onSuccess: (loop) => {
      qc.invalidateQueries({ queryKey: ["loops"] });
      onCreated(loop);
      onClose();
    },
    onError: (e) => setServerError(e instanceof Error ? e.message : t("errorCreateFailed")),
  });

  const patch = (p: Partial<FormState>) => {
    setForm((prev) => ({ ...prev, ...p }));
    // Editing a field clears its stale validation error immediately.
    const cleared: FieldKey[] = [];
    if ("name" in p) cleared.push("name");
    if ("boardId" in p) cleared.push("board");
    if ("goal" in p) cleared.push("goal");
    if ("backlogSource" in p || "backlogMd" in p || "backlogTag" in p || "projectId" in p) {
      cleared.push("source");
    }
    if (cleared.length) {
      setFieldErrors((prev) => {
        const next = { ...prev };
        for (const k of cleared) delete next[k];
        return next;
      });
    }
  };

  const validate = (): Partial<Record<FieldKey, string>> => {
    const errs: Partial<Record<FieldKey, string>> = {};
    if (!form.name.trim()) errs.name = t("errorNameRequired");
    if (!form.boardId) errs.board = t("errorBoardRequired");
    if (!form.goal.trim()) errs.goal = t("errorGoalRequired");
    if (form.backlogSource === "markdown" && !form.backlogMd.trim()) {
      errs.source = t("errorBacklogMarkdownRequired");
    }
    if (form.backlogSource === "project" && !form.projectId) {
      errs.source = t("errorProjectRequired");
    }
    if (form.backlogSource === "tag" && !form.backlogTag.trim()) {
      errs.source = t("errorTagRequired");
    }
    return errs;
  };

  const handleSubmit = () => {
    setServerError(null);
    const errs = validate();
    setFieldErrors(errs);
    if (Object.keys(errs).length > 0) {
      // Bring the first invalid field into view (fields appear in DOM order).
      bodyRef.current
        ?.querySelector("[data-field-error]")
        ?.scrollIntoView({ block: "center", behavior: "smooth" });
      return;
    }

    const payload: LoopCreate = {
      board_id: form.boardId,
      name: form.name.trim(),
      goal: form.goal.trim(),
      backlog_source: form.backlogSource,
      ...(form.backlogSource === "markdown" ? { backlog_md: form.backlogMd.trim() } : {}),
      ...(form.backlogSource === "project" ? { project_id: form.projectId } : {}),
      ...(form.backlogSource === "tag" ? { backlog_tag: form.backlogTag.trim() } : {}),
      max_rounds: form.maxRounds,
      ...(form.budgetUsd.trim() ? { budget_usd: Number(form.budgetUsd) } : {}),
      ...(form.budgetTokens.trim() ? { budget_tokens: Number(form.budgetTokens) } : {}),
      pause_on_failed_rounds: form.pauseOnFailedRounds,
      human_every_n_rounds: form.humanEveryNRounds,
      ...(form.maxDurationMinutes.trim()
        ? { max_duration_minutes: Number(form.maxDurationMinutes) }
        : {}),
      stop_on_backlog_empty: form.stopOnBacklogEmpty,
      telegram_reports: form.telegramReports,
    };
    createMutation.mutate(payload);
  };

  // ── Live contract summary ────────────────────────────────────────────────
  const summaryParts = [
    t("summaryRounds", { n: form.maxRounds }),
    ...(form.maxDurationMinutes.trim() ? [t("summaryDuration", { n: form.maxDurationMinutes.trim() })] : []),
    ...(form.budgetUsd.trim() ? [t("summaryBudgetUsd", { n: form.budgetUsd.trim() })] : []),
    ...(form.budgetTokens.trim() ? [t("summaryBudgetTokens", { n: form.budgetTokens.trim() })] : []),
    t("summaryBreaker", { n: form.pauseOnFailedRounds }),
    ...(form.humanEveryNRounds > 0 ? [t("summaryHumanGate", { n: form.humanEveryNRounds })] : []),
    ...(form.backlogSource !== "open_ended" && form.stopOnBacklogEmpty ? [t("summaryStopEmpty")] : []),
    ...(form.telegramReports ? [t("summaryReports")] : []),
  ];

  return (
    <ResponsiveModal open={open} onClose={onClose} aria-labelledby="create-loop-title">
      <div className="px-5 pt-4 pb-3 shrink-0" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
        <h2 id="create-loop-title" className="text-base font-semibold" style={{ color: C.textPrimary }}>
          {t("title")}
        </h2>
        <p className="text-xs mt-0.5" style={{ color: C.textMuted }}>
          {t("subtitle")}
        </p>
      </div>

      <div ref={bodyRef} className="flex flex-col gap-5 px-5 py-4 overflow-y-auto">
        {/* ── 1 · Auftrag ─────────────────────────────────────────────── */}
        <Fieldset legend={t("sectionBrief")}>
          <div className="grid gap-3 sm:grid-cols-[1fr_minmax(180px,0.8fr)]">
            <Field label={t("name")} error={fieldErrors.name}>
              <TextInput
                value={form.name}
                onChange={(v) => patch({ name: v })}
                placeholder={t("namePlaceholder")}
                invalid={!!fieldErrors.name}
              />
            </Field>
            <Field label={t("board")} error={fieldErrors.board}>
              <SelectInput
                value={form.boardId}
                onChange={(v) => patch({ boardId: v, projectId: "" })}
                invalid={!!fieldErrors.board}
              >
                <option value="">{t("chooseBoard")}</option>
                {boards.map((b: Board) => (
                  <option key={b.id} value={b.id}>
                    {b.name}
                  </option>
                ))}
              </SelectInput>
            </Field>
          </div>
          <Field label={t("goal")} hint={t("goalHint")} error={fieldErrors.goal}>
            <textarea
              value={form.goal}
              onChange={(e) => patch({ goal: e.target.value })}
              rows={3}
              placeholder={t("goalPlaceholder")}
              className="w-full resize-none rounded-md px-3 py-2 text-sm outline-none transition-colors"
              style={inputStyle(!!fieldErrors.goal)}
              onFocus={focusOn}
              onBlur={focusOff(!!fieldErrors.goal)}
            />
          </Field>
        </Fieldset>

        {/* ── 2 · Arbeitsquelle ───────────────────────────────────────── */}
        <Fieldset legend={t("sectionSource")}>
          <div role="radiogroup" aria-label={t("sectionSource")} className="flex flex-col gap-1.5">
            {SOURCE_ORDER.map((src) => {
              const meta = SOURCE_META[src];
              const Icon = meta.icon;
              const selected = form.backlogSource === src;
              return (
                <label
                  key={src}
                  className="flex flex-col rounded-md px-3 py-2.5 cursor-pointer transition-colors"
                  style={{
                    background: selected ? C.accentSubtle : C.bgSurface,
                    border: `1px solid ${selected ? C.borderAccent : C.border}`,
                  }}
                >
                  <span className="flex items-start gap-2.5">
                    <input
                      type="radio"
                      name="loop-backlog-source"
                      checked={selected}
                      onChange={() => patch({ backlogSource: src })}
                      className="sr-only"
                    />
                    <span
                      aria-hidden
                      className="mt-[3px] flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full transition-colors"
                      style={{ border: `1.5px solid ${selected ? C.accent : C.borderActive}` }}
                    >
                      {selected && (
                        <span className="h-1.5 w-1.5 rounded-full" style={{ background: C.accent }} />
                      )}
                    </span>
                    <span className="flex min-w-0 flex-col gap-0.5">
                      <span className="flex items-center gap-1.5 text-sm font-medium" style={{ color: C.textPrimary }}>
                        <Icon size={13} style={{ color: selected ? C.accent : C.textMuted }} aria-hidden />
                        {t(meta.labelKey)}
                      </span>
                      <span className="text-[11px] leading-snug" style={{ color: C.textMuted }}>
                        {t(meta.descKey)}
                      </span>
                    </span>
                  </span>

                  {/* Source-specific input, nested in the selected row */}
                  {selected && src === "markdown" && (
                    <span className="mt-2.5 block pl-6">
                      <textarea
                        value={form.backlogMd}
                        onChange={(e) => patch({ backlogMd: e.target.value })}
                        rows={5}
                        placeholder={t("backlogMarkdownPlaceholder")}
                        aria-label={t("backlogMarkdown")}
                        className="w-full resize-none rounded-md px-3 py-2 text-sm font-mono outline-none transition-colors"
                        style={inputStyle(!!fieldErrors.source)}
                        onFocus={focusOn}
                        onBlur={focusOff(!!fieldErrors.source)}
                      />
                    </span>
                  )}
                  {selected && src === "project" && (
                    <span className="mt-2.5 block pl-6">
                      <SelectInput
                        value={form.projectId}
                        onChange={(v) => patch({ projectId: v })}
                        disabled={!form.boardId}
                        invalid={!!fieldErrors.source}
                        ariaLabel={t("project")}
                      >
                        <option value="">{t("chooseProject")}</option>
                        {projects.map((proj) => (
                          <option key={proj.id} value={proj.id}>
                            {proj.name}
                          </option>
                        ))}
                      </SelectInput>
                    </span>
                  )}
                  {selected && src === "tag" && (
                    <span className="mt-2.5 block pl-6">
                      <TextInput
                        value={form.backlogTag}
                        onChange={(v) => patch({ backlogTag: v })}
                        placeholder={t("tagPlaceholder")}
                        invalid={!!fieldErrors.source}
                        ariaLabel={t("tag")}
                        mono
                      />
                    </span>
                  )}
                </label>
              );
            })}
          </div>
          {fieldErrors.source && <FieldError text={fieldErrors.source} />}
        </Fieldset>

        {/* ── 3 · Leitplanken ─────────────────────────────────────────── */}
        <Fieldset legend={t("sectionRails")} hint={t("railsHint")}>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t("maxRounds")}>
              <NumberInput
                value={String(form.maxRounds)}
                onChange={(v) => patch({ maxRounds: Math.max(1, Number(v) || 1) })}
                min={1}
                suffix={t("unitRounds")}
              />
            </Field>
            <Field label={t("maxDuration")}>
              <NumberInput
                value={form.maxDurationMinutes}
                onChange={(v) => patch({ maxDurationMinutes: v })}
                min={1}
                placeholder={t("noLimit")}
                suffix={t("unitMinutes")}
              />
            </Field>
            <Field label={t("budgetUsd")}>
              <NumberInput
                value={form.budgetUsd}
                onChange={(v) => patch({ budgetUsd: v })}
                min={0}
                step="0.5"
                placeholder={t("noLimit")}
                suffix="$"
              />
            </Field>
            <Field label={t("budgetTokens")}>
              <NumberInput
                value={form.budgetTokens}
                onChange={(v) => patch({ budgetTokens: v })}
                min={0}
                placeholder={t("noLimit")}
                suffix={t("unitTokens")}
              />
            </Field>
            <Field label={t("pauseAfterFailedRounds")} hint={t("breakerHint")}>
              <NumberInput
                value={String(form.pauseOnFailedRounds)}
                onChange={(v) => patch({ pauseOnFailedRounds: Math.max(1, Number(v) || 1) })}
                min={1}
                suffix={t("unitFailedRounds")}
              />
            </Field>
            <Field label={t("humanGateEvery")} hint={t("humanGateHint")}>
              <NumberInput
                value={String(form.humanEveryNRounds)}
                onChange={(v) => patch({ humanEveryNRounds: Math.max(0, Number(v) || 0) })}
                min={0}
                suffix={t("unitRounds")}
              />
            </Field>
          </div>

          <div className="flex flex-col gap-1 pt-1">
            {form.backlogSource !== "open_ended" && (
              <ToggleRow
                label={t("stopOnEmptyBacklog")}
                checked={form.stopOnBacklogEmpty}
                onChange={(v) => patch({ stopOnBacklogEmpty: v })}
              />
            )}
            <ToggleRow
              label={t("telegramReports")}
              checked={form.telegramReports}
              onChange={(v) => patch({ telegramReports: v })}
            />
          </div>
        </Fieldset>

        {serverError && (
          <div
            className="flex items-start gap-2 rounded-md border px-3 py-2 text-xs"
            role="alert"
            style={{
              borderColor: `${C.error}66`,
              background: `${C.error}14`,
              color: STATUS_TEXT.error,
            }}
          >
            <AlertTriangle size={14} className="mt-0.5 shrink-0" />
            <span>{serverError}</span>
          </div>
        )}
      </div>

      {/* ── Contract summary + actions ──────────────────────────────────── */}
      <div className="shrink-0" style={{ borderTop: `1px solid ${C.borderSubtle}` }}>
        <p
          className="px-5 pt-2.5 font-mono text-[11px] leading-relaxed"
          style={{ color: C.textMuted }}
          aria-live="polite"
        >
          <span className="label-sys mr-2" style={{ color: C.textMuted }}>
            {t("summaryLabel")}
          </span>
          {summaryParts.join(" · ")}
        </p>
        <div
          className="flex items-center justify-end gap-2 px-5 pt-2.5 pb-3"
          style={{ paddingBottom: "calc(env(safe-area-inset-bottom) + 0.75rem)" }}
        >
          <button
            type="button"
            onClick={onClose}
            disabled={createMutation.isPending}
            className="rounded-md px-3 py-1.5 text-sm cursor-pointer transition"
            style={{ border: `1px solid ${C.borderActive}`, color: C.textSecondary }}
          >
            {t("cancel")}
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={createMutation.isPending}
            className="flex items-center gap-1.5 rounded-md px-3.5 py-1.5 text-sm font-semibold cursor-pointer transition disabled:opacity-60"
            style={{ background: C.accent, color: C.onAccent }}
          >
            {createMutation.isPending && <Loader2 size={14} className="animate-spin" />}
            {t("createLoop")}
          </button>
        </div>
      </div>
    </ResponsiveModal>
  );
}

// ── Form primitives (local — token-only styling per the vocabulary rule) ────

function inputStyle(invalid: boolean): React.CSSProperties {
  return {
    background: C.bgDeep,
    border: `1px solid ${invalid ? `${C.error}88` : C.border}`,
    color: C.textPrimary,
  };
}

function focusOn(e: React.FocusEvent<HTMLElement>) {
  e.currentTarget.style.borderColor = `${C.accent}66`;
}

function focusOff(invalid: boolean) {
  return (e: React.FocusEvent<HTMLElement>) => {
    e.currentTarget.style.borderColor = invalid ? `${C.error}88` : C.border;
  };
}

function Fieldset({
  legend,
  hint,
  children,
}: {
  legend: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <fieldset className="flex flex-col gap-2.5 border-0 p-0 m-0 min-w-0">
      <legend className="label-sys p-0 mb-0.5 float-left w-full">{legend}</legend>
      {hint && (
        <p className="text-[11px] -mt-1.5" style={{ color: C.textMuted }}>
          {hint}
        </p>
      )}
      {children}
    </fieldset>
  );
}

function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: string;
  error?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1 min-w-0" {...(error ? { "data-field-error": true } : {})}>
      <span className="text-[11px] font-medium" style={{ color: C.textSecondary }}>
        {label}
      </span>
      {children}
      {hint && !error && (
        <span className="text-[10.5px] leading-snug" style={{ color: C.textMuted }}>
          {hint}
        </span>
      )}
      {error && <FieldError text={error} />}
    </label>
  );
}

function FieldError({ text }: { text: string }) {
  return (
    <span data-field-error className="flex items-center gap-1 text-[11px]" style={{ color: STATUS_TEXT.error }}>
      <AlertTriangle size={11} className="shrink-0" aria-hidden />
      {text}
    </span>
  );
}

function TextInput({
  value,
  onChange,
  placeholder,
  invalid = false,
  ariaLabel,
  mono = false,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  invalid?: boolean;
  ariaLabel?: string;
  mono?: boolean;
}) {
  return (
    <input
      type="text"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      aria-label={ariaLabel}
      className={`w-full rounded-md px-3 py-2 text-sm outline-none transition-colors ${mono ? "font-mono" : ""}`}
      style={inputStyle(invalid)}
      onFocus={focusOn}
      onBlur={focusOff(invalid)}
    />
  );
}

function SelectInput({
  value,
  onChange,
  children,
  disabled = false,
  invalid = false,
  ariaLabel,
}: {
  value: string;
  onChange: (v: string) => void;
  children: React.ReactNode;
  disabled?: boolean;
  invalid?: boolean;
  ariaLabel?: string;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      disabled={disabled}
      aria-label={ariaLabel}
      className="w-full rounded-md px-3 py-2 text-sm outline-none transition-colors cursor-pointer disabled:cursor-not-allowed disabled:opacity-60"
      style={inputStyle(invalid)}
      onFocus={focusOn}
      onBlur={focusOff(invalid)}
    >
      {children}
    </select>
  );
}

function NumberInput({
  value,
  onChange,
  min,
  step,
  placeholder,
  suffix,
}: {
  value: string;
  onChange: (v: string) => void;
  min?: number;
  step?: string;
  placeholder?: string;
  suffix?: string;
}) {
  return (
    <span
      className="flex w-full items-center rounded-md transition-colors"
      style={{ background: C.bgDeep, border: `1px solid ${C.border}` }}
    >
      <input
        type="number"
        value={value}
        min={min}
        step={step}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="w-full min-w-0 bg-transparent px-3 py-2 text-sm outline-none [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
        style={{ color: C.textPrimary }}
        onFocus={(e) => {
          (e.currentTarget.parentElement as HTMLElement).style.borderColor = `${C.accent}66`;
        }}
        onBlur={(e) => {
          (e.currentTarget.parentElement as HTMLElement).style.borderColor = C.border;
        }}
      />
      {suffix && (
        <span className="shrink-0 pr-3 font-mono text-[10px] uppercase tracking-wider" style={{ color: C.textMuted }}>
          {suffix}
        </span>
      )}
    </span>
  );
}

function ToggleRow({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className="flex w-full items-center justify-between gap-3 rounded-md px-1 py-1.5 text-left text-sm cursor-pointer"
      style={{ color: C.textSecondary }}
    >
      <span>{label}</span>
      <span
        aria-hidden
        className="relative shrink-0 rounded-full transition-colors"
        style={{
          width: 36,
          height: 20,
          backgroundColor: checked ? C.accent : C.bgElevated,
          border: `1px solid ${checked ? C.accent : C.border}`,
        }}
      >
        <span
          className="absolute top-1/2 -translate-y-1/2 rounded-full transition-all"
          style={{
            left: checked ? 18 : 2,
            width: 14,
            height: 14,
            backgroundColor: checked ? C.onAccent : C.textMuted,
          }}
        />
      </span>
    </button>
  );
}
