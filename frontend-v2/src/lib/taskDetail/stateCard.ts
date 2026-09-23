import type { Approval, RunRecord, Task, TaskComment } from "@/lib/types";
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
 */

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
      prUrl: string | null;
      prNumber: number | null;
      evidenceCount: number | null;
      durationSeconds: number | null;
    }
  | { kind: "failed"; error: string | null };

const NEEDS_YOU = new Set(["blocked", "waiting", "user_test"]);

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
}: {
  task: Task;
  approvals: Approval[];
  comments: TaskComment[];
  runRecord: RunRecord | null;
}): StateCard | null {
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
    return {
      kind: "result",
      resolution: resolution?.content?.trim() || null,
      prUrl: task.pr_url ?? null,
      prNumber: task.pr_number ?? null,
      evidenceCount: runRecord ? runRecord.beweise.anzahl : null,
      durationSeconds:
        runRecord?.zeiten.dauer_sekunden ?? secondsBetween(task.created_at, task.completed_at),
    };
  }

  if (task.status === "failed" || task.status === "aborted") {
    const err = sorted.find((c) => c.comment_type === "blocker" || c.author_type === "system");
    const lastEvent = [...(runRecord?.schritte ?? [])].reverse().find((s) => s.quelle === "ereignis");
    return { kind: "failed", error: err?.content?.trim() || lastEvent?.text || null };
  }

  return null;
}
