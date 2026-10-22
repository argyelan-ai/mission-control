"use client";

/**
 * The rows of a task section (spec A §2.4): chapter, tool, text, comment,
 * system line, delivery, attempt separator, time mark, and the tail rows
 * (live, waiting for the first step, detached, queued, end line).
 *
 * Two edges only (K9, §2.6): the main edge (16 px, the icon slot starts
 * here) and the text edge (main + 16 px slot + 8 px gap). Two sizes (14 for
 * what the agent said, 12 for meta) and two weights (K5). Every row is
 * `memo` and gets its unit object unchanged when nothing changed in it, so a
 * poll with one new row re-renders that row only (§6.4).
 */
import { createContext, memo, useContext, useMemo, useState } from "react";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { ChevronRight, MessageSquare, ShieldCheck } from "lucide-react";
import { C, stepFailedStyle } from "@/lib/colors";
import type { ChatTaskStub, TaskStepDetail, TaskStepLiveDetail, ToolEvent } from "@/lib/chatTypes";
import { formatDuration } from "@/lib/taskDetail/format";
import { formatSpan } from "@/lib/taskDetail/stateLine";
import { useNow } from "@/hooks/useNow";
import { useTaskStepDetail } from "@/hooks/useTaskStepDetail";
import type { DetailLevel } from "../chatOptions";
import { ClampedContent } from "../ClampedContent";
import { LiveThinkingRow } from "../LiveThinkingRow";
import { MarkdownContent } from "../MarkdownContent";
import { ToolRow, type ToolRunState } from "../ToolRow";
import { useAnchoredAction } from "../useScrollAnchor";
import { chapterSummary, rowMs, type TaskUnit, type ToolStep } from "./buildTaskUnits";
import { ChatTaskStateLine } from "./ChatTaskStateLine";

/** Main edge (16 px) — the same as ToolRow / LiveThinkingRow use. */
export const EDGE = "px-4";
/** The 16 px icon slot; with gap-2 the text starts on the text edge. */
export const SLOT = "w-4 shrink-0 flex justify-center";
/** No new step for this long (and no thought running) = quiet "no new steps". */
export const STALLED_AFTER_MS = 10 * 60_000;

/** "Load again" for a task's steps (the chat's steps query, by task id). */
export const TaskStepsRetryContext = createContext<(taskId: string) => void>(() => {});

export function taskHref(taskId: string): string {
  return `/tasks?task=${encodeURIComponent(taskId)}`;
}

export function clock(ts: string | null | undefined, locale: string): string {
  const ms = rowMs(ts);
  if (ms == null) return "";
  return new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit" }).format(new Date(ms));
}

export function toolEventFromRow(row: ToolStep): ToolEvent {
  return {
    kind: "tool",
    uuid: row.id,
    ts: row.ts ?? "",
    name: row.name ?? "",
    title: row.title,
    detail: {},
    toolUseId: null,
    result: row.st === "running" ? null : "",
    status: row.st === "error" ? "error" : "done",
    stats: row.stats ?? null,
    sidechain: false,
  };
}

export function toolEventFromDetail(d: TaskStepDetail, row: ToolStep): ToolEvent {
  return {
    ...toolEventFromRow(row),
    detail: (d.detail as Record<string, unknown> | null) ?? {},
    result: d.result ?? (row.st === "running" ? null : ""),
    diffs: d.diffs ?? null,
    locations: d.locations ?? null,
    stats: d.stats ?? row.stats ?? null,
  };
}

interface UnitProps {
  taskId: string;
  /** The task is live here: a tool without result runs; otherwise it has none (N9). */
  taskLive: boolean;
  detailLevel: DetailLevel;
}

/** A tool step: the chat's ToolRow, its body fetched on open. */
export const TaskToolRow = memo(function TaskToolRow({ row, taskId, taskLive, detailLevel }: UnitProps & { row: ToolStep }) {
  const [want, setWant] = useState(false);
  const detail = useTaskStepDetail(taskId, row.id, want, row.st);
  const ev = useMemo(() => toolEventFromRow(row), [row]);
  const lazy = useMemo(
    () => ({
      event: detail.data ? toolEventFromDetail(detail.data, row) : null,
      loading: detail.loading || (want && !detail.data && !detail.error),
      load: () => setWant(true),
      error: detail.error,
      retry: detail.retry,
    }),
    [detail.data, detail.loading, detail.error, detail.retry, want, row],
  );
  const runState: ToolRunState | null =
    row.st !== "running" ? null : taskLive ? { kind: "running", since: row.ts } : { kind: "noResult" };
  return (
    <ToolRow
      ev={ev}
      detailLevel={detailLevel}
      lazyDetail={lazy}
      liveOutput={false}
      runningTone="neutral"
      statsTone="muted"
      runState={runState}
      anchored
      wideIcon
    />
  );
});

/**
 * A row's capped text with "Show more": the full text is fetched on tap and
 * shown clamped (ClampedContent, its own "show more" for very long texts).
 * `lead = null`: the lead is shown elsewhere (a chapter's first line).
 */
function ExpandableText({
  taskId,
  rowId,
  lead,
  markdown = false,
}: {
  taskId: string;
  rowId: string;
  lead: string | null;
  markdown?: boolean;
}) {
  const t = useTranslations("chat");
  const tSteps = useTranslations("sessions.taskSteps");
  const [want, setWant] = useState(false);
  const { ref, run } = useAnchoredAction();
  const detail = useTaskStepDetail(taskId, rowId, want);
  const full = detail.data?.text ?? null;
  const body = (text: string) =>
    markdown ? <MarkdownContent content={text} compact /> : <p dir="auto" className="m-0 whitespace-pre-wrap">{text}</p>;
  if (want && full != null) {
    return (
      <ClampedContent text={full} testId="task-step-full" className="break-words">
        {body(full)}
      </ClampedContent>
    );
  }
  return (
    <>
      {lead != null && body(lead)}
      {want ? (
        detail.error ? (
          <StepLoadFailed onRetry={detail.retry} />
        ) : (
          <p className="m-0 py-2 text-xs" style={{ color: C.textMuted }}>
            {tSteps("loading")}
          </p>
        )
      ) : (
        <button
          ref={ref as React.RefObject<HTMLButtonElement>}
          type="button"
          onClick={() => run(() => setWant(true))}
          className="min-h-11 text-left text-xs font-medium cursor-pointer bg-transparent border-0 p-0"
          style={{ color: C.textMuted }}
        >
          {t("showMore")}
        </button>
      )}
    </>
  );
}

export const TaskChapterRow = memo(function TaskChapterRow({
  unit,
  taskId,
  taskLive,
  detailLevel,
}: UnitProps & { unit: Extract<TaskUnit, { u: "chapter" }> }) {
  const t = useTranslations("sessions.taskSteps");
  const tTool = useTranslations("sessions.toolCard");
  const locale = useLocale();
  // The reader's own choice wins; until then the automatic default (§2.3).
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const open = userOpen ?? unit.open;
  const { ref, run } = useAnchoredAction();
  const summary = chapterSummary(unit.tools);
  const duration = unit.ms != null && unit.ms >= 60_000 ? formatDuration(unit.ms / 1000, locale) : null;
  const running = summary.running && taskLive;
  return (
    <div data-testid="task-unit" data-unit="chapter" data-open={open}>
      <button
        ref={ref as React.RefObject<HTMLButtonElement>}
        type="button"
        aria-expanded={open}
        onClick={() => run(() => setUserOpen(!open))}
        className={`w-full min-h-11 ${EDGE} py-2 flex items-start gap-2 text-left bg-transparent border-0 cursor-pointer`}
      >
        <span className={SLOT} aria-hidden="true" />
        <span className="min-w-0 flex-1 flex flex-col gap-1">
          <span className="flex items-baseline gap-2 min-w-0">
            {/* Clamped only while closed: open, the whole lead shows — a lead
                the server sent with `more: false` has nothing behind "Show
                more" (review SHOULD-1 on #937). */}
            <span
              dir="auto"
              className={`min-w-0 flex-1 text-sm ${open ? "" : "line-clamp-2"} break-words`}
              style={{ color: C.textSecondary }}
            >
              {unit.lead}
              {unit.count > 1 && <span style={{ color: C.textMuted }}> · {t("repeated", { count: unit.count })}</span>}
            </span>
            {duration && (
              <span className="shrink-0 text-xs tabular-nums" style={{ color: C.textMuted }}>
                {duration}
              </span>
            )}
          </span>
          {/* Line 2 says which tools ran — only while closed: open, the
              tools stand below as their own rows (K3: not twice). */}
          {unit.tools.length > 0 && !open && (
            <span className="flex items-center gap-2 min-w-0 text-xs" style={{ color: C.textMuted }}>
              {summary.titles.map((title, i) => (
                <span key={i} className="min-w-0 truncate font-mono">
                  {title}
                </span>
              ))}
              {summary.more > 0 && <span className="shrink-0 tabular-nums">{t("moreTools", { count: summary.more })}</span>}
              {summary.failed && (
                <span
                  data-testid="chapter-failed-dot"
                  role="img"
                  aria-label={tTool("failed")}
                  className="shrink-0 inline-block w-1.5 h-1.5 rounded-full"
                  style={{ background: stepFailedStyle().dot }}
                />
              )}
              {running && <span className="shrink-0">{tTool("running")}</span>}
            </span>
          )}
        </span>
      </button>
      {open && (
        <div data-testid="chapter-body" className="pb-2">
          {unit.more && (
            <div className={`${EDGE} flex gap-2`}>
              <span className={SLOT} aria-hidden="true" />
              <div className="min-w-0 flex-1">
                <ExpandableText taskId={taskId} rowId={unit.id} lead={null} />
              </div>
            </div>
          )}
          {unit.tools.map((row) => (
            <TaskToolRow key={row.id} row={row} taskId={taskId} taskLive={taskLive} detailLevel={detailLevel} />
          ))}
        </div>
      )}
    </div>
  );
});

export const TaskTextRow = memo(function TaskTextRow({
  unit,
  taskId,
}: { unit: Extract<TaskUnit, { u: "text" }>; taskId: string }) {
  return (
    <div data-testid="task-unit" data-unit="text" className={`${EDGE} py-2 min-w-0`}>
      <div dir="auto" className="text-sm break-words" style={{ color: C.textPrimary }}>
        {unit.row.more ? (
          <ExpandableText taskId={taskId} rowId={unit.id} lead={unit.row.text} markdown />
        ) : (
          <MarkdownContent content={unit.row.text} compact />
        )}
      </div>
    </div>
  );
});

export const TaskCommentRow = memo(function TaskCommentRow({
  unit,
  taskId,
}: { unit: Extract<TaskUnit, { u: "comment" }>; taskId: string }) {
  const t = useTranslations("sessions.taskSteps");
  return (
    <div data-testid="task-unit" data-unit="comment" className={`${EDGE} py-2 flex items-start gap-2`}>
      <span className={`${SLOT} pt-1`}>
        <MessageSquare size={13} style={{ color: C.textMuted }} role="img" aria-label={t("comment")} />
      </span>
      <div dir="auto" className="min-w-0 flex-1 text-sm break-words" style={{ color: C.textSecondary }}>
        {unit.row.more ? (
          <ExpandableText taskId={taskId} rowId={unit.id} lead={unit.row.text} />
        ) : (
          <span className="whitespace-pre-wrap">{unit.row.text}</span>
        )}
      </div>
    </div>
  );
});

export const TaskSystemRow = memo(function TaskSystemRow({ unit }: { unit: Extract<TaskUnit, { u: "system" }> }) {
  const t = useTranslations("sessions.taskSteps");
  const known = ["allow_always", "allow_once", "reject_once", "reject_always"];
  let label = unit.what;
  if (unit.decision) {
    const decision = known.includes(unit.decision) ? t(`permissionDecision.${unit.decision}`) : unit.decision;
    label = t("permission", { what: unit.what, decision });
  } else if (unit.request) {
    label = t("permissionRequest", { what: unit.what });
  }
  return (
    <div data-testid="task-unit" data-unit="system" className={`${EDGE} min-h-11 flex items-center gap-2 text-xs`}>
      <span className={SLOT}>
        <ShieldCheck size={13} style={{ color: C.textMuted }} aria-hidden="true" />
      </span>
      <span dir="auto" className="min-w-0 truncate" style={{ color: C.textMuted }}>
        {label}
      </span>
    </div>
  );
});

export const TaskDeliveryRow = memo(function TaskDeliveryRow({
  unit,
  taskId,
}: { unit: Extract<TaskUnit, { u: "delivery" }>; taskId: string }) {
  const t = useTranslations("sessions.taskSteps");
  const locale = useLocale();
  const [open, setOpen] = useState(false);
  const { ref, run } = useAnchoredAction();
  const detail = useTaskStepDetail(taskId, unit.id, open);
  return (
    <div data-testid="task-unit" data-unit="delivery">
      <button
        ref={ref as React.RefObject<HTMLButtonElement>}
        type="button"
        aria-expanded={open}
        onClick={() => run(() => setOpen((v) => !v))}
        className={`w-full min-h-11 ${EDGE} flex items-center gap-2 text-left text-xs bg-transparent border-0 cursor-pointer`}
        style={{ color: C.textMuted }}
      >
        <span className={SLOT} aria-hidden="true" />
        <span className="min-w-0 truncate">{t("delivered", { time: clock(unit.ts, locale) })}</span>
        <ChevronRight
          size={14}
          aria-hidden="true"
          className={`shrink-0 transition-transform motion-reduce:transition-none ${open ? "rotate-90" : ""}`}
        />
      </button>
      {open && (
        <div className={`${EDGE} pb-2 flex gap-2`}>
          <span className={SLOT} aria-hidden="true" />
          <div className="min-w-0 flex-1 text-sm" style={{ color: C.textSecondary }}>
            {detail.data?.text != null ? (
              <ClampedContent text={detail.data.text} testId="task-delivery-text" className="break-words">
                <p dir="auto" className="m-0 whitespace-pre-wrap">{detail.data.text}</p>
              </ClampedContent>
            ) : detail.error ? (
              <StepLoadFailed onRetry={detail.retry} />
            ) : (
              <p className="m-0 text-xs" style={{ color: C.textMuted }}>
                {t("loading")}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
});

/** One step's full text could not be fetched: say so, offer "Load again". */
function StepLoadFailed({ onRetry }: { onRetry: () => void }) {
  const t = useTranslations("sessions.taskSteps");
  return (
    <p data-testid="task-step-failed" className="m-0 flex items-center gap-2 text-xs" style={{ color: C.textMuted }}>
      <span>{t("stepLoadFailed")}</span>
      <button
        type="button"
        onClick={onRetry}
        className="min-h-11 bg-transparent border-0 p-0 cursor-pointer text-xs font-medium"
        style={{ color: C.textSecondary }}
      >
        {t("retry")}
      </button>
    </p>
  );
}

/** A section's steps could not be loaded (no rows yet): say so, offer "Load again". */
function TaskStepsLoadFailedRow({ taskId }: { taskId: string }) {
  const t = useTranslations("sessions.taskSteps");
  const retry = useContext(TaskStepsRetryContext);
  return (
    <div
      data-testid="task-unit"
      data-unit="loadFailed"
      className={`${EDGE} min-h-11 flex items-center gap-2 text-xs`}
      style={{ color: C.textMuted }}
    >
      <span className={SLOT} aria-hidden="true" />
      <span className="min-w-0 truncate">{t("loadFailed")}</span>
      <button
        type="button"
        onClick={() => retry(taskId)}
        className="shrink-0 min-h-11 bg-transparent border-0 p-0 cursor-pointer text-xs font-medium"
        style={{ color: C.textSecondary }}
      >
        {t("retry")}
      </button>
    </div>
  );
}

/** A quiet meta line on the text edge: time mark, attempt, not loaded, queued, pending. */
function MetaRow({ unit, children }: { unit: string; children: React.ReactNode }) {
  return (
    <div data-testid="task-unit" data-unit={unit} className={`${EDGE} py-2 flex items-center gap-2 text-xs`} style={{ color: C.textMuted }}>
      <span className={SLOT} aria-hidden="true" />
      <span className="min-w-0 truncate tabular-nums">{children}</span>
    </div>
  );
}

export const TaskMarkRow = memo(function TaskMarkRow({ unit }: { unit: Extract<TaskUnit, { u: "mark" | "attempt" }> }) {
  const t = useTranslations("sessions.taskSteps");
  const locale = useLocale();
  if (unit.u === "attempt") return <MetaRow unit="attempt">{t("attempt", { n: unit.n, time: clock(unit.ts, locale) })}</MetaRow>;
  return <MetaRow unit="mark">{clock(unit.ts, locale)}</MetaRow>;
});

/** The running thought / answer, or a quiet "no new steps" when it stalls. */
export const TaskLiveRow = memo(function TaskLiveRow({
  unit,
  taskId,
}: { unit: Extract<TaskUnit, { u: "live" }>; taskId: string }) {
  const t = useTranslations("sessions.taskSteps");
  const locale = useLocale();
  const now = useNow(60_000);
  const [open, setOpen] = useState(false);
  const live = unit.liveRow;
  const detail = useTaskStepDetail<TaskStepLiveDetail>(taskId, "live", open && live?.block === "thinking");
  if (live && live.block === "thinking") {
    const text = detail.data?.liveRow && detail.data.liveRow.block === "thinking" ? detail.data.liveRow.text ?? "" : "";
    return (
      <div data-testid="task-unit" data-unit="live">
        <LiveThinkingRow
          wide
          onOpenChange={setOpen}
          preview={{
            kind: "preview",
            uuid: null,
            ts: live.ts,
            text,
            source: "acp",
            block: "thinking",
            ...(typeof live.chars === "number" ? { chars: live.chars } : {}),
            ...(live.startedAt ? { startedAt: live.startedAt } : {}),
          }}
        />
      </div>
    );
  }
  const lastMs = rowMs(live?.ts ?? unit.lastTs);
  const quietMs = lastMs == null ? 0 : now - lastMs;
  if ((live?.stale ?? false) || quietMs >= STALLED_AFTER_MS) {
    return <MetaRow unit="stalled">{t("noNewSteps", { span: formatSpan(quietMs / 1000, locale) ?? "" })}</MetaRow>;
  }
  if (live && live.block === "text" && live.text.trim()) {
    return (
      <div data-testid="task-unit" data-unit="live" className={`${EDGE} py-2`}>
        <p dir="auto" className="m-0 text-sm line-clamp-3 break-words whitespace-pre-wrap" style={{ color: C.textSecondary }}>
          {live.text}
        </p>
      </div>
    );
  }
  return null;
});

export const TaskEndLine = memo(function TaskEndLine({ stub }: { stub: ChatTaskStub }) {
  const t = useTranslations("sessions.taskSteps");
  return (
    <div data-testid="task-unit" data-unit="end" className="pb-2">
      <ChatTaskStateLine stub={stub} />
      {(stub.outcome || stub.deliverables > 0) && (
        <div className={`${EDGE} flex gap-2`}>
          <span className={SLOT} aria-hidden="true" />
          <p dir="auto" className="m-0 min-w-0 flex-1 text-sm line-clamp-2 break-words" style={{ color: C.textSecondary }}>
            {stub.outcome}
            {stub.deliverables > 0 && (
              <span style={{ color: C.textMuted }}>
                {stub.outcome ? " · " : ""}
                {t("files", { count: stub.deliverables })}
              </span>
            )}
          </p>
        </div>
      )}
    </div>
  );
});

export function TaskUnitView({
  unit,
  stub,
  taskLive,
  detailLevel,
  queuedLabel,
}: {
  unit: TaskUnit;
  stub: ChatTaskStub;
  taskLive: boolean;
  detailLevel: DetailLevel;
  queuedLabel: (count: number) => string;
}) {
  const t = useTranslations("sessions.taskSteps");
  const taskId = stub.taskId;
  switch (unit.u) {
    case "chapter":
      return <TaskChapterRow unit={unit} taskId={taskId} taskLive={taskLive} detailLevel={detailLevel} />;
    case "tool":
      return <TaskToolRow row={unit.row} taskId={taskId} taskLive={taskLive} detailLevel={detailLevel} />;
    case "text":
      return <TaskTextRow unit={unit} taskId={taskId} />;
    case "comment":
      return <TaskCommentRow unit={unit} taskId={taskId} />;
    case "system":
      return <TaskSystemRow unit={unit} />;
    case "delivery":
      return <TaskDeliveryRow unit={unit} taskId={taskId} />;
    case "mark":
    case "attempt":
      return <TaskMarkRow unit={unit} />;
    case "notLoaded":
      return <MetaRow unit="notLoaded">{t("notLoaded", { count: unit.count })}</MetaRow>;
    case "live":
      return <TaskLiveRow unit={unit} taskId={taskId} />;
    case "pending":
      return <MetaRow unit="pending">{t("firstStepPending")}</MetaRow>;
    case "loadFailed":
      return <TaskStepsLoadFailedRow taskId={taskId} />;
    case "queued":
      return <MetaRow unit="queued">{queuedLabel(unit.count)}</MetaRow>;
    case "detached":
      return (
        <Link
          href={taskHref(taskId)}
          data-testid="task-unit"
          data-unit="detached"
          className={`${EDGE} min-h-11 flex items-center gap-2 text-xs`}
          style={{ color: C.textMuted }}
        >
          <span className={SLOT} aria-hidden="true" />
          <span className="min-w-0 truncate">{t("detached")}</span>
          <span className="shrink-0 flex items-center gap-1" style={{ color: C.textSecondary }}>
            {t("action.view")}
            <ChevronRight size={14} aria-hidden="true" />
          </span>
        </Link>
      );
    case "end":
      return <TaskEndLine stub={stub} />;
  }
}
