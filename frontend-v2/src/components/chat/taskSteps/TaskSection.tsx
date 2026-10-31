"use client";

/**
 * A task's work inside the agent chat (spec A, variant A): segments of a
 * section, and the collapsed card of a finished task.
 *
 * A segment's steps stand on the chat's own ground, like the rest of the
 * timeline (operator 31.10.2026: the dark band behind a whole task read as an
 * island). Only the task's head is marked: hairlines above and below on the
 * chat ground, its clipboard icon pulsing while the task runs (static when
 * not, `motion-reduce` safe), and above a task's first head one muted line
 * "New task · own session" — the head and that line are one element, no
 * second divider. The head is ONE sticky 44 px line — clipboard icon, the title (truncated), and
 * a link by state ("View" / "Review" / "Answer") — the whole line opens the
 * task (K11/K12). The first segment adds the state line under it, which
 * scrolls away. A later segment's head follows the task's state there
 * (`entry.head`): "<title> · continues" only while the task works; the
 * title alone otherwise (finished, waiting for the operator, not started) —
 * also over past work rows — its link names the action, and the state line
 * stays with the first segment only (K3). Only the operator's own message
 * starts a new band; the chat's other lines stand below the open one
 * (`mergeTimeline`). Two regions
 * (K6/S10): `chat-task-head` (its one status colour is the state dot) and
 * `chat-task-steps` (its one status colour is the ocker dot of a failed step).
 */
import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { ChevronRight, ClipboardList } from "lucide-react";
import { C } from "@/lib/colors";
import { benchResultHrefOfTask } from "@/lib/benchLinks";
import type { ChatTaskStub } from "@/lib/chatTypes";
import type { DetailLevel } from "../chatOptions";
import { useScrollAnchor } from "../useScrollAnchor";
import type { TaskStepsState } from "./stepsState";
import { buildTaskUnits, rowMs, type TaskUnit } from "./buildTaskUnits";
import type { MergedEntry } from "./mergeTimeline";
import { ChatTaskStateLine } from "./ChatTaskStateLine";
import { SLOT, TaskUnitView, taskHref } from "./TaskUnits";

/** Units mounted first in a segment; the rest one frame later (§6.3). */
export const SEGMENT_FIRST_UNITS = 30;

const LINK_KEY: Record<string, "view" | "review" | "answer"> = {
  review: "review",
  waiting: "answer",
  blocked: "answer",
  user_test: "answer",
};

export function linkAction(status: string): "view" | "review" | "answer" {
  return LINK_KEY[status] ?? "view";
}

/** Where a card's head leads: a bench run to its benchmark result (the
 *  task page only holds the raw file), every other task to the task. */
export function headLink(stub: ChatTaskStub): { href: string; action: "view" | "review" | "answer" | "bench" } {
  const bench = benchResultHrefOfTask({ auto_reason: stub.autoReason });
  return bench ? { href: bench, action: "bench" } : { href: taskHref(stub.taskId), action: linkAction(stub.status) };
}

/** The head's mark: hairlines on the chat ground (opaque, it is sticky). */
const HEAD = "border-y";
const headStyle = { borderColor: C.border, background: C.bgSurface };
const MARK = "\u0000";

/** The one-line sticky head of a segment (S8). */
export function TaskSectionHeader({
  stub,
  continuation = false,
  sticky = true,
}: {
  stub: ChatTaskStub;
  continuation?: boolean;
  sticky?: boolean;
}) {
  const live = stub.live;
  const t = useTranslations("sessions.taskSteps");
  // "{title} · continues": the title is drawn on its own (truncated), the
  // words around it stay whole — in whatever order the language puts them.
  const parts = continuation ? t("continues", { title: MARK }).split(MARK) : null;
  const link = headLink(stub);
  return (
    <Link
      href={link.href}
      data-testid="chat-task-head"
      data-region="chat-task-head"
      data-live={live ? "" : undefined}
      className={`${sticky ? "sticky top-0 z-[1]" : ""} ${HEAD} min-h-11 px-4 flex items-center gap-2 no-underline`}
      style={headStyle}
    >
      <span className={SLOT} aria-hidden="true">
        {/* The calm live sign: the icon breathes while the task runs. */}
        <ClipboardList
          size={16}
          className={live ? "animate-pulse motion-reduce:animate-none" : ""}
          style={{ color: live ? C.textSecondary : C.textMuted }}
        />
      </span>
      <span className="min-w-0 flex-1 flex items-baseline text-sm">
        {parts?.[0] && (
          <span className="shrink-0 whitespace-pre" style={{ color: C.textMuted }}>
            {parts[0]}
          </span>
        )}
        <span className="min-w-0 truncate font-semibold" style={{ color: C.textPrimary }}>
          {stub.title}
        </span>
        {parts?.[1] && (
          <span className="shrink-0 whitespace-pre" style={{ color: C.textMuted }}>
            {parts[1]}
          </span>
        )}
      </span>
      <span className="shrink-0 flex items-center gap-1 text-xs" style={{ color: C.textSecondary }}>
        {t(`action.${link.action}`)}
        <ChevronRight size={14} aria-hidden="true" />
      </span>
    </Link>
  );
}

/**
 * Where the chat's session and a task's own session meet (31.10.2026: the
 * operator could not tell that a task runs in a fresh session, nor that the
 * chat goes on after it). No colour, no surface (K6/K8).
 *
 * `SessionStart`: one muted line on the title's edge, right above a task's
 * first head — part of the head, not a second divider.
 */
function SessionStart() {
  const t = useTranslations("sessions.taskSteps");
  return (
    <p data-testid="chat-session-divider" data-kind="start" className="m-0 px-4 pt-2 pb-1 flex gap-2 text-xs" style={{ color: C.textMuted }}>
      <span className={SLOT} aria-hidden="true" />
      <span className="min-w-0 truncate">{t("sessionStart")}</span>
    </p>
  );
}

/** "Back in chat": a hairline with one muted phrase where chat lines follow
 *  a task's steps — now that the steps stand on the chat ground, this is
 *  where the task's part ends. */
export function SessionDivider({ kind }: { kind: "back" }) {
  const t = useTranslations("sessions.taskSteps");
  const text = t("sessionBack");
  return (
    <div
      role="separator"
      aria-label={text}
      data-testid="chat-session-divider"
      data-kind={kind}
      className="px-4 pt-3 pb-1 flex items-center gap-3 text-xs"
    >
      <span aria-hidden="true" className="h-px flex-1" style={{ background: C.borderSubtle }} />
      <span aria-hidden="true" className="shrink-0" style={{ color: C.textMuted }}>{text}</span>
      <span aria-hidden="true" className="h-px flex-1" style={{ background: C.borderSubtle }} />
    </div>
  );
}

/** Tail first: the last units now, the rest one frame later (§6.3, S6). */
function useTailFirst(count: number): number {
  const [all, setAll] = useState(count <= SEGMENT_FIRST_UNITS);
  useEffect(() => {
    if (all) return;
    if (typeof requestAnimationFrame === "undefined") {
      setAll(true);
      return;
    }
    const h = requestAnimationFrame(() => setAll(true));
    return () => cancelAnimationFrame(h);
  }, [all]);
  return all ? 0 : Math.max(0, count - SEGMENT_FIRST_UNITS);
}

export const TaskSegment = memo(function TaskSegment({
  entry,
  stub,
  detailLevel,
  queuedLabel,
}: {
  entry: Extract<MergedEntry, { kind: "segment" }>;
  stub: ChatTaskStub;
  detailLevel: DetailLevel;
  queuedLabel: (count: number) => string;
}) {
  const t = useTranslations("sessions.taskSteps");
  const skip = useTailFirst(entry.units.length);
  const units = skip > 0 ? entry.units.slice(skip) : entry.units;
  return (
    <section
      data-testid="chat-task-segment"
      data-task-id={stub.taskId}
      data-first={entry.first}
      data-head={entry.head}
      aria-label={t("region", { title: stub.title })}
      className="my-2"
    >
      {entry.first && <SessionStart />}
      <TaskSectionHeader stub={stub} continuation={entry.head === "continues"} />
      {entry.head === "full" && (
        <div data-region="chat-task-head">
          <ChatTaskStateLine stub={stub} />
        </div>
      )}
      <div data-region="chat-task-steps" className="pb-1">
        {units.map((u) => (
          <TaskUnitView key={u.key} unit={u} stub={stub} taskLive={stub.live} detailLevel={detailLevel} queuedLabel={queuedLabel} />
        ))}
      </div>
    </section>
  );
});

/**
 * A finished task seen on (re)load: one collapsed card at its start (§2.2).
 * Title, state sentence, outcome line, link, chevron; the chevron loads the
 * steps lazily INSIDE the card, and the tapped head stays where it was (M3).
 */
export const TaskCard = memo(function TaskCard({
  stub,
  open,
  onToggle,
  steps,
  loading,
  error,
  onRetry,
  frozenAtMs,
  detailLevel,
}: {
  stub: ChatTaskStub;
  open: boolean;
  onToggle: (taskId: string) => void;
  steps: TaskStepsState | null;
  loading: boolean;
  error: boolean;
  onRetry: (taskId: string) => void;
  /** Rows after this time are new work, shown below as segments (M4). */
  frozenAtMs: number | null;
  detailLevel: DetailLevel;
}) {
  const t = useTranslations("sessions.taskSteps");
  const anchor = useScrollAnchor();
  const headRef = useRef<HTMLDivElement>(null);
  const pending = useRef(false);
  useLayoutEffect(() => {
    if (!pending.current) return;
    pending.current = false;
    anchor.restore();
  }, [open, anchor]);

  const units = useMemo<TaskUnit[]>(() => {
    if (!open || !steps) return [];
    const frozen = frozenAtMs ?? Number.POSITIVE_INFINITY;
    const rows = steps.rows.filter((r) => {
      const ms = rowMs(r.ts);
      return ms == null || ms <= frozen;
    });
    const start = rowMs(stub.startKey) ?? rowMs(stub.startedAt);
    const built = buildTaskUnits(rows, { lastMarkMs: start }, { detailLevel });
    return steps.omitted > 0 ? [{ u: "notLoaded", key: "notLoaded", count: steps.omitted }, ...built.units] : built.units;
  }, [open, steps, frozenAtMs, stub.startKey, stub.startedAt, detailLevel]);

  return (
    <section
      data-testid="chat-task-card"
      data-region="chat-task-card"
      data-task-id={stub.taskId}
      aria-label={t("region", { title: stub.title })}
      className="my-2"
    >
      <div ref={headRef} className={`${open ? "sticky top-0 z-[1]" : ""} ${HEAD} flex items-stretch`} style={headStyle}>
        <button
          type="button"
          aria-expanded={open}
          aria-label={open ? t("hideSteps") : t("showSteps")}
          onClick={() => {
            anchor.capture(headRef.current);
            pending.current = true;
            onToggle(stub.taskId);
          }}
          className="shrink-0 min-h-11 pl-4 pr-2 flex items-center bg-transparent border-0 cursor-pointer"
        >
          <span className={SLOT}>
            <ChevronRight
              size={16}
              aria-hidden="true"
              className={`transition-transform motion-reduce:transition-none ${open ? "rotate-90" : ""}`}
              style={{ color: C.textMuted }}
            />
          </span>
        </button>
        <Link
          href={headLink(stub).href}
          data-region="chat-task-head"
          className="min-w-0 flex-1 min-h-11 pr-4 flex items-center gap-2 no-underline"
        >
          <span className="min-w-0 flex-1 truncate text-sm font-semibold" style={{ color: C.textPrimary }}>
            {stub.title}
          </span>
          <span className="shrink-0 flex items-center gap-1 text-xs" style={{ color: C.textSecondary }}>
            {t(`action.${headLink(stub).action}`)}
            <ChevronRight size={14} aria-hidden="true" />
          </span>
        </Link>
      </div>
      <ChatTaskStateLine stub={stub} />
      {(stub.outcome || stub.deliverables > 0) && (
        <div className="px-4 pb-2 flex gap-2">
          <span className={SLOT} aria-hidden="true" />
          <p className="m-0 min-w-0 flex-1 flex items-baseline text-sm" style={{ color: C.textSecondary }}>
            {stub.outcome && (
              <span dir="auto" className="min-w-0 truncate">
                {stub.outcome}
              </span>
            )}
            {stub.deliverables > 0 && (
              <span className="shrink-0 whitespace-pre" style={{ color: C.textMuted }}>
                {stub.outcome ? " · " : ""}
                {t("files", { count: stub.deliverables })}
              </span>
            )}
          </p>
        </div>
      )}
      {open && (
        <div data-region="chat-task-steps" data-testid="chat-task-card-steps" className="pb-1">
          {!steps && loading && (
            <div className="px-4 py-2 flex flex-col gap-2 animate-pulse motion-reduce:animate-none" aria-hidden="true">
              {["88%", "64%", "76%"].map((w) => (
                <div key={w} className="ml-6 h-3 rounded-sm" style={{ width: w, background: C.bgElevated }} />
              ))}
              <span className="sr-only">{t("loading")}</span>
            </div>
          )}
          {!steps && error && (
            <div className="px-4 min-h-11 flex items-center gap-2 text-xs" style={{ color: C.textMuted }}>
              <span className={SLOT} aria-hidden="true" />
              <span>{t("loadFailed")}</span>
              <button
                type="button"
                onClick={() => onRetry(stub.taskId)}
                className="min-h-11 bg-transparent border-0 p-0 cursor-pointer text-xs font-medium"
                style={{ color: C.textSecondary }}
              >
                {t("retry")}
              </button>
            </div>
          )}
          {units.map((u) => (
            <TaskUnitView key={u.key} unit={u} stub={stub} taskLive={false} detailLevel={detailLevel} queuedLabel={() => ""} />
          ))}
        </div>
      )}
    </section>
  );
});
