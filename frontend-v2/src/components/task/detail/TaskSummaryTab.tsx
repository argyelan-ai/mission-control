"use client";

/**
 * Summary tab — the default view of the task detail (except while running).
 * Built from the run record JSON (GET /api/v1/tasks/{id}/run-record), NOT from
 * the German .md: the labels come from i18n (EN/DE), the content (brief,
 * notes, step texts, decision texts) is shown as-is in its working language.
 *
 *   BRIEF      6 lines · Show full brief
 *   TIMES      created · dispatched · done
 *   PLAN       checklist ✓ x/y (or the curated plan notes)
 *   STEPS      subtasks by status, first 10 as links · Show all
 *              (no subtasks → status changes and events)
 *   EVIDENCE   PR · counts per kind
 *   DECISIONS  open (type, age) · approved · rejected
 *   FRICTION   disturbance types ×count (never the info-level reminders)
 *
 * PROPERTIES (the task's editable facts, built by TaskDetailBody) follow the
 * brief on the phone and stand as a right column from 720 px container width.
 * Below the boxes: the existing detail sections (relations, references, git).
 */

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import { useLocale, useTranslations } from "next-intl";
import { ChevronDown, CheckSquare, Square, AlertCircle } from "lucide-react";
import { C, LANE } from "@/lib/colors";
import { formatAge } from "@/lib/taskDetail/format";
import { eventLabel } from "@/lib/taskDetail/eventLabel";
import { statusLabelKey } from "@/lib/taskDetail/statusLabels";
import type { RunRecord, Task, TaskChecklistItem, TaskSummary } from "@/lib/types";
import { TaskDescription } from "../TaskDescription";
import { PrChip } from "./PrChip";

const STEP_LINKS = 10;
// Order of the status counts in STEPS — finished first, then the open ends.
const CHILD_STATUS_ORDER = ["done", "in_progress", "review", "user_test", "waiting", "blocked", "failed", "aborted", "inbox"] as const;

function Row({ label, children, testId }: { label: string; children: React.ReactNode; testId?: string }) {
  return (
    <div
      className="grid grid-cols-1 @min-[420px]:grid-cols-[96px_1fr] gap-x-3 gap-y-1 py-2.5"
      style={{ borderBottom: `1px solid ${C.borderSubtle}` }}
      data-testid={testId}
    >
      <div className="label-sys label-sys--dim pt-0.5">{label}</div>
      <div className="min-w-0 text-xs leading-relaxed" style={{ color: C.textPrimary }}>
        {children}
      </div>
    </div>
  );
}

function Toggle({ open, onClick, children }: { open: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-expanded={open}
      className="inline-flex items-center gap-1 mt-1 text-[11px] cursor-pointer hover:underline"
      style={{ color: C.textSecondary }}
    >
      {children}
      <ChevronDown size={11} style={{ transform: open ? "rotate(180deg)" : "none", transition: "transform 0.15s" }} />
    </button>
  );
}

function Dot({ status }: { status: string }) {
  return <span className="inline-block w-1.5 h-1.5 rounded-full shrink-0" style={{ background: LANE[status] ?? C.textMuted }} />;
}

export function TaskSummaryTab({
  task,
  runRecord,
  isLoading,
  isError,
  onRetry,
  subtasks,
  checklist,
  briefExtra,
  onOpenTask,
  leading,
  properties,
  children,
}: {
  task: Task;
  runRecord: RunRecord | undefined;
  isLoading: boolean;
  isError: boolean;
  onRetry: () => void;
  subtasks: TaskSummary[];
  checklist: TaskChecklistItem[];
  /** Intake/briefing fields, shown with the full brief. */
  briefExtra?: React.ReactNode;
  onOpenTask?: (taskId: string) => void;
  /** Rendered above the run record boxes (head runs list). */
  leading?: React.ReactNode;
  /** The properties list — after the brief, or the right column when wide. */
  properties?: React.ReactNode;
  /** Existing detail sections, rendered below the run record boxes. */
  children?: React.ReactNode;
}) {
  const t = useTranslations("tasks");
  const locale = useLocale();
  const [briefOpen, setBriefOpen] = useState(false);
  const [planOpen, setPlanOpen] = useState(false);
  const [stepsOpen, setStepsOpen] = useState(false);
  const [allSteps, setAllSteps] = useState(false);

  const statusWord = (s: string) => {
    const key = statusLabelKey(s);
    return key ? t(key) : s;
  };
  const ago = (ts: string | null | undefined) => formatAge(ts, locale);

  let briefBox: React.ReactNode = null;
  let boxes: React.ReactNode;
  if (isLoading) {
    // Fixed height while loading — the tabs below must not jump.
    boxes = (
      <div className="min-h-[280px] flex items-start pt-3 text-xs" style={{ color: C.textMuted }} aria-busy="true">
        {t("detail.summaryLoading")}
      </div>
    );
  } else if (isError || !runRecord) {
    boxes = (
      <div className="min-h-[80px] py-3 text-xs flex items-center gap-3" style={{ color: C.textSecondary }} role="alert">
        {t("detail.summaryError")}
        <button
          type="button"
          onClick={onRetry}
          className="px-2.5 py-1 rounded-md text-[11px] cursor-pointer"
          style={{ border: `1px solid ${C.borderActive}`, color: C.textSecondary }}
        >
          {t("detail.retry")}
        </button>
      </div>
    );
  } else {
    const { zeiten, plan, schritte, beweise, entscheidungen, reibung } = runRecord;

    // ── BRIEF ──
    const brief = task.description?.trim() ?? "";
    const briefNode = brief ? (
      <>
        {briefOpen ? (
          <div className="-mx-4">
            <TaskDescription description={brief} />
          </div>
        ) : (
          // Collapsed: the same markdown renderer as the full brief, cut at
          // ~6 lines; headings use the calm prose scale (no page-size type).
          <div
            data-testid="summary-brief-preview"
            className="prose-description max-h-[9.9em] overflow-hidden [mask-image:linear-gradient(to_bottom,black_70%,transparent)]"
            style={{ color: C.textSecondary }}
          >
            <ReactMarkdown>{brief}</ReactMarkdown>
          </div>
        )}
        {briefOpen && briefExtra}
        <div>
          <Toggle open={briefOpen} onClick={() => setBriefOpen((o) => !o)}>
            {briefOpen ? t("detail.showLess") : t("detail.showFullBrief")}
          </Toggle>
        </div>
      </>
    ) : (
      <span style={{ color: C.textMuted }}>{t("detail.noBrief")}</span>
    );

    // ── TIMES ──
    const created = ago(zeiten.erstellt ?? task.created_at);
    const dispatched = ago(zeiten.dispatched ?? task.dispatched_at);
    const done = ago(zeiten.abgeschlossen ?? task.completed_at);
    const times = [
      created ? t("detail.timesCreated", { age: created }) : null,
      dispatched ? t("detail.timesDispatched", { age: dispatched }) : t("detail.timesNotDispatched"),
      done ? t("detail.timesDone", { age: done }) : t("detail.timesNotDone"),
    ].filter(Boolean);

    // ── PLAN ──
    const checklistDone = checklist.filter((i) => i.status === "done").length;
    let planNode: React.ReactNode;
    if (checklist.length > 0) {
      planNode = (
        <>
          <span className="font-mono">✓ {checklistDone}/{checklist.length}</span>
          <div>
            <Toggle open={planOpen} onClick={() => setPlanOpen((o) => !o)}>{t("checklist")}</Toggle>
          </div>
          {planOpen && (
            <ul className="mt-1.5 space-y-1">
              {checklist.map((item) => (
                <li key={item.id} className="flex items-center gap-2">
                  {item.status === "done" ? (
                    <CheckSquare size={12} style={{ color: C.online, flexShrink: 0 }} />
                  ) : item.status === "blocked" ? (
                    <AlertCircle size={12} style={{ color: C.error, flexShrink: 0 }} />
                  ) : (
                    <Square size={12} style={{ color: C.textMuted, flexShrink: 0 }} />
                  )}
                  <span style={{ color: item.status === "done" ? C.textMuted : C.textPrimary }}>{item.title}</span>
                </li>
              ))}
            </ul>
          )}
        </>
      );
    } else if (plan.length > 0) {
      planNode = (
        <>
          <span>{t("detail.planNotes", { count: plan.length })}</span>
          <div>
            <Toggle open={planOpen} onClick={() => setPlanOpen((o) => !o)}>{t("detail.showAll", { count: plan.length })}</Toggle>
          </div>
          {planOpen && (
            <ul className="mt-1.5 space-y-1">
              {plan.map((p, i) => (
                <li key={`${p.ts}-${i}`} style={{ color: C.textSecondary }}>
                  {p.autor ? <span style={{ color: C.textMuted }}>{p.autor}: </span> : null}
                  {p.inhalt}
                </li>
              ))}
            </ul>
          )}
        </>
      );
    } else {
      planNode = <span style={{ color: C.textMuted }}>—</span>;
    }

    // ── STEPS ──
    let stepsNode: React.ReactNode;
    if (subtasks.length > 0) {
      const counts = new Map<string, number>();
      for (const c of subtasks) counts.set(c.status, (counts.get(c.status) ?? 0) + 1);
      const parts = CHILD_STATUS_ORDER.filter((s) => counts.get(s)).map((s) =>
        t("detail.statusCount", { label: statusWord(s), count: counts.get(s)! }),
      );
      const shown = allSteps ? subtasks : subtasks.slice(0, STEP_LINKS);
      stepsNode = (
        <>
          <span>{[t("detail.subtasksCount", { count: subtasks.length }), ...parts].join(" · ")}</span>
          <div>
            <Toggle open={stepsOpen} onClick={() => setStepsOpen((o) => !o)}>
              {t("detail.showFirst", { count: Math.min(STEP_LINKS, subtasks.length) })}
            </Toggle>
          </div>
          {stepsOpen && (
            <ul className="mt-1.5 space-y-1" data-testid="summary-subtasks">
              {shown.map((c) => (
                <li key={c.id} className="flex items-center gap-2 min-w-0">
                  <Dot status={c.status} />
                  <a
                    href={`/tasks?task=${c.id}`}
                    onClick={(e) => {
                      if (!onOpenTask || e.metaKey || e.ctrlKey) return;
                      e.preventDefault();
                      onOpenTask(c.id);
                    }}
                    className="truncate hover:underline"
                    style={{ color: C.textPrimary }}
                    title={c.title}
                  >
                    {c.title}
                  </a>
                  <span className="shrink-0 text-[11px]" style={{ color: C.textMuted }}>
                    {statusWord(c.status)}
                  </span>
                </li>
              ))}
              {!allSteps && subtasks.length > STEP_LINKS && (
                <li>
                  <Toggle open={false} onClick={() => setAllSteps(true)}>
                    {t("detail.showAll", { count: subtasks.length })}
                  </Toggle>
                </li>
              )}
            </ul>
          )}
        </>
      );
    } else if (schritte.length > 0) {
      const recent = allSteps ? schritte : schritte.slice(-STEP_LINKS);
      stepsNode = (
        <>
          <span>{t("detail.stepsCount", { count: schritte.length })}</span>
          <div>
            <Toggle open={stepsOpen} onClick={() => setStepsOpen((o) => !o)}>
              {schritte.length > STEP_LINKS
                ? t("detail.showLast", { count: STEP_LINKS })
                : t("detail.showAll", { count: schritte.length })}
            </Toggle>
          </div>
          {stepsOpen && (
            <ul className="mt-1.5 space-y-1">
              {[...recent].reverse().map((s, i) => (
                <li key={`${s.ts}-${i}`} style={{ color: C.textSecondary }}>
                  <span className="font-mono text-[10px] mr-1.5" style={{ color: C.textMuted }}>{ago(s.ts)}</span>
                  {s.actor_label || s.changed_by ? <span style={{ color: C.textMuted }}>{s.actor_label || s.changed_by}: </span> : null}
                  {s.text}
                </li>
              ))}
              {!allSteps && schritte.length > STEP_LINKS && (
                <li>
                  <Toggle open={false} onClick={() => setAllSteps(true)}>
                    {t("detail.showAll", { count: schritte.length })}
                  </Toggle>
                </li>
              )}
            </ul>
          )}
        </>
      );
    } else {
      stepsNode = <span style={{ color: C.textMuted }}>—</span>;
    }

    // ── EVIDENCE ──
    const evidenceParts = Object.entries(beweise.nach_typ).map(([typ, n]) => {
      const key = `detail.evidenceType.${typ}`;
      return `${t.has(key) ? t(key) : typ}: ${n}`;
    });
    const evidenceNode =
      task.pr_url || evidenceParts.length > 0 ? (
        <span className="inline-flex items-center gap-2 flex-wrap">
          {task.pr_url && <PrChip url={task.pr_url} number={task.pr_number} />}
          {evidenceParts.length > 0 && <span>{evidenceParts.join(" · ")}</span>}
        </span>
      ) : (
        <span style={{ color: C.textMuted }}>{t("detail.none")}</span>
      );

    // ── DECISIONS ──
    const open = entscheidungen.filter((e) => e.status === "offen");
    const approved = entscheidungen.filter((e) => e.status === "approved").length;
    const rejected = entscheidungen.filter((e) => e.status === "rejected").length;
    const decisionParts = [
      open.length
        ? `${t("detail.decisionsOpen", { count: open.length })} (${open
            .map((e) => t("detail.decisionOpenItem", { type: eventLabel(t, e.typ), age: ago(e.ts) ?? "—" }))
            .join("; ")})`
        : null,
      approved ? t("detail.decisionsApproved", { count: approved }) : null,
      rejected ? t("detail.decisionsRejected", { count: rejected }) : null,
    ].filter(Boolean);

    // ── FRICTION ──
    const frictionParts = Object.entries(reibung)
      .sort((a, b) => b[1].anzahl - a[1].anzahl)
      .map(([typ, info]) => t("detail.repeated", { label: eventLabel(t, typ), count: info.anzahl }));

    briefBox = (
      <div data-testid="run-record-summary">
        <Row label={t("detail.boxBrief")} testId="summary-brief">{briefNode}</Row>
      </div>
    );
    boxes = (
      <div data-testid="run-record-summary-rest">
        <Row label={t("detail.boxTimes")} testId="summary-times">{times.join(" · ")}</Row>
        <Row label={t("detail.boxPlan")} testId="summary-plan">{planNode}</Row>
        <Row label={t("detail.boxSteps")} testId="summary-steps">{stepsNode}</Row>
        <Row label={t("detail.boxEvidence")} testId="summary-evidence">{evidenceNode}</Row>
        <Row label={t("detail.boxDecisions")} testId="summary-decisions">
          {decisionParts.length ? decisionParts.join(" · ") : <span style={{ color: C.textMuted }}>{t("detail.none")}</span>}
        </Row>
        <Row label={t("detail.boxFriction")} testId="summary-friction">
          {frictionParts.length ? frictionParts.join(" · ") : <span style={{ color: C.textMuted }}>{t("detail.none")}</span>}
        </Row>
      </div>
    );
  }

  // One DOM order for both layouts: narrow = brief, properties, rest (auto
  // flow); wide = brief + rest in the left column, properties on the right.
  // (Class names spelled out — Tailwind only sees literal strings.)
  return (
    <div className="@container">
      <div className="grid grid-cols-1 gap-y-6 @min-[720px]:grid-cols-[minmax(0,1fr)_16rem] @min-[720px]:gap-x-8 @min-[720px]:gap-y-0">
        <div className="min-w-0 @min-[720px]:col-start-1 @min-[720px]:row-start-1">
          {leading}
          {briefBox}
        </div>
        {properties && (
          <div className="min-w-0 @min-[720px]:col-start-2 @min-[720px]:row-start-1 @min-[720px]:row-span-2 @min-[720px]:self-start">
            {properties}
          </div>
        )}
        <div className="min-w-0 @min-[720px]:col-start-1 @min-[720px]:row-start-2">
          {boxes}
          {children && (
            <div className="mt-4 -mx-4">
              <div className="px-4 pb-1 label-sys label-sys--dim">{t("detail.details")}</div>
              {children}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
