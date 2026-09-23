import type { Approval, RunRecord, Task, TaskComment } from "@/lib/types";
import { HEAD_SILENT_WARN_S, type HeadRun } from "@/lib/heads";
import { parseTs, secondsBetween } from "./format";

/**
 * Which state card sits on top of the task detail, and what it says. Pure so
 * every state is testable with fixtures; the component only renders it.
 *
 *   blocked / waiting / user_test → NEEDS YOU (open approval embedded, else
 *                                   the latest blocker comment + Reply)
 *   in_progress                   → RUNNING (last step, runtime, heartbeat)
 *   done                          → RESULT (resolution, PR, evidence, duration)
 *   failed / aborted              → FAILED (error in plain words + Open log)
 *   inbox / review                → no card
 *
 * A head run (docs/specs/head-launcher.md §8.2) wins over the task status:
 * the card then shows the head's own state + ONE main action
 *   starting/running → Stop · needs_you → Answer · passed → Open PR ·
 *   failed/stopped → Restart with …
 */

export type HeadMainAction = "stop" | "answer" | "open_pr" | "restart";

export type StateCard =
  | {
      kind: "needs_you";
      approval: Approval | null;
      reason: string | null;
      since: string | null;
      askerAgentId: string | null;
    }
  | { kind: "running"; lastStep: string | null; startedAt: string | null; heartbeatAt: string | null }
  | {
      kind: "result";
      resolution: string | null;
      /** No resolution comment — `resolution` is the latest agent note. */
      resolutionIsFallback: boolean;
      prUrl: string | null;
      prNumber: number | null;
      evidenceCount: number | null;
      durationSeconds: number | null;
    }
  | { kind: "failed"; error: string | null }
  | {
      kind: "head";
      run: HeadRun;
      mainAction: HeadMainAction;
      /** Running but no output for > 15 min — warn tone, still running. */
      silentWarn: boolean;
    };

export function headMainAction(run: HeadRun): HeadMainAction {
  switch (run.state) {
    case "starting":
    case "running":
      return "stop";
    case "needs_you":
      return "answer";
    case "passed":
      return run.pr_url ? "open_pr" : "restart";
    default:
      return "restart";
  }
}

const NEEDS_YOU = new Set(["blocked", "waiting", "user_test"]);
const RESULT_FALLBACK_TYPES = new Set(["message", "progress", "review", "handoff", "checkpoint"]);

function newestFirst<T extends { created_at: string }>(list: T[]): T[] {
  return [...list].sort(
    (a, b) => (parseTs(b.created_at)?.getTime() ?? 0) - (parseTs(a.created_at)?.getTime() ?? 0),
  );
}

function firstLine(text: string | null | undefined): string | null {
  const line = (text ?? "").split("\n").map((l) => l.trim()).find(Boolean);
  return line ?? null;
}

export function deriveStateCard({
  task,
  approvals,
  comments,
  runRecord,
  headRun = null,
}: {
  task: Task;
  approvals: Approval[];
  comments: TaskComment[];
  runRecord: RunRecord | null;
  /** Newest head run of this task, if any. */
  headRun?: HeadRun | null;
}): StateCard | null {
  if (headRun) {
    return {
      kind: "head",
      run: headRun,
      mainAction: headMainAction(headRun),
      silentWarn: headRun.state === "running" && (headRun.silent_s ?? 0) > HEAD_SILENT_WARN_S,
    };
  }

  const sorted = newestFirst(comments);

  if (NEEDS_YOU.has(task.status)) {
    const open = newestFirst(approvals.filter((a) => a.task_id === task.id && a.status === "pending"))[0] ?? null;
    if (open) {
      return {
        kind: "needs_you",
        approval: open,
        reason: open.description || null,
        since: task.blocked_at ?? open.created_at,
        askerAgentId: open.agent_id ?? task.assigned_agent_id,
      };
    }
    const blocker = sorted.find((c) => c.comment_type === "blocker");
    return {
      kind: "needs_you",
      approval: null,
      reason: blocker?.content ?? null,
      since: task.blocked_at ?? blocker?.created_at ?? task.updated_at,
      askerAgentId: blocker?.author_agent_id ?? task.assigned_agent_id,
    };
  }

  if (task.status === "in_progress") {
    const step = sorted.find((c) => c.author_type !== "user" && c.comment_type !== "reflection");
    const lastSchritt = runRecord?.schritte?.[runRecord.schritte.length - 1]?.text ?? null;
    return {
      kind: "running",
      lastStep: firstLine(step?.content) ?? lastSchritt,
      startedAt: task.ack_at ?? task.dispatched_at ?? task.started_at,
      heartbeatAt: task.last_activity_at,
    };
  }

  if (task.status === "done") {
    const resolution = sorted.find((c) => c.comment_type === "resolution");
    // Most older cards have no resolution comment — the latest agent note
    // (not a reflection) is the next best answer to "what came out".
    const fallback = resolution
      ? undefined
      : sorted.find((c) => c.author_type === "agent" && RESULT_FALLBACK_TYPES.has(c.comment_type ?? "") && c.content?.trim());
    return {
      kind: "result",
      resolution: (resolution ?? fallback)?.content?.trim() || null,
      resolutionIsFallback: !resolution && !!fallback,
      prUrl: task.pr_url ?? null,
      prNumber: task.pr_number ?? null,
      evidenceCount: runRecord ? runRecord.beweise.anzahl : null,
      durationSeconds:
        runRecord?.zeiten.dauer_sekunden ?? secondsBetween(task.created_at, task.completed_at),
    };
  }

  if (task.status === "failed" || task.status === "aborted") {
    // Order: the agent's blocker → the last warning/error event (schritte
    // only carry warning+ events) → any system note. A harmless system note
    // written after the failure must not pose as the error.
    const blocker = sorted.find((c) => c.comment_type === "blocker" && c.content?.trim());
    const lastEvent = [...(runRecord?.schritte ?? [])].reverse().find((s) => s.quelle === "ereignis" && s.text);
    const systemNote = sorted.find((c) => c.author_type === "system" && c.content?.trim());
    return {
      kind: "failed",
      error: blocker?.content?.trim() || lastEvent?.text || systemNote?.content?.trim() || null,
    };
  }

  return null;
}
