/**
 * What a task's state in the chat is NOW, from the freshest source
 * (round-3 user test t1 NICE-2/3).
 *
 * Two polls tell the chat about a task: the stubs (`/chat/tasks`, every
 * 10 s while a task is live) and the steps (`/chat/tasks/{id}/steps`, every
 * 2 s). Both carry the task's status. The band and the status line read the
 * stub — so for 4–9 s a task the agent already worked on said "not started ·
 * delivered, waiting for the model", and at the end the status line said
 * "Working on a task" for 8–15 s while the band already linked "Review".
 *
 * Rules (pure, so every case is testable):
 * 1. The newer answer wins: the steps' status and liveness replace the
 *    stub's only when the steps were fetched after the stubs.
 * 2. A delivered task (`inbox`, live) whose agent already wrote work rows
 *    after the latest delivery is in progress — the status just has not
 *    flipped yet (the same reading as `segmentHead`, review NICE-1 on #948).
 */
import type { ChatAliveness, ChatTaskStub, StateEvent, TaskStepRow } from "@/lib/chatTypes";
import type { TaskStepsState } from "./stepsState";

/** Rows the agent wrote itself — not the delivery or the attempt marks. */
const WORK_KINDS = new Set<TaskStepRow["k"]>(["think", "tool", "text", "comment"]);

/** The first work row after the latest delivery / attempt mark, or null. */
function firstWorkSinceDelivery(rows: readonly TaskStepRow[]): TaskStepRow | null {
  let from = 0;
  for (let i = rows.length - 1; i >= 0; i--) {
    if (rows[i].k === "delivery" || rows[i].k === "attempt") {
      from = i + 1;
      break;
    }
  }
  for (let i = from; i < rows.length; i++) if (WORK_KINDS.has(rows[i].k)) return rows[i];
  return null;
}

/**
 * The stub as the chat should show it. Returns the SAME object when nothing
 * changes (memo-friendly).
 *
 * @param stubsAt  when the stubs answer came in (ms), 0 = unknown
 * @param stepsAt  when this task's steps answer came in (ms), 0 = none
 */
export function observedStub(
  stub: ChatTaskStub,
  steps: TaskStepsState | null,
  stubsAt: number,
  stepsAt: number,
): ChatTaskStub {
  let status = stub.status;
  let live = stub.live;
  let startedAt = stub.startedAt;
  if (steps && stepsAt > stubsAt && (steps.status !== status || steps.live !== live)) {
    status = steps.status;
    live = steps.live;
  }
  if (status === "inbox" && live && steps) {
    const work = steps.liveRow ? null : firstWorkSinceDelivery(steps.rows);
    if (steps.liveRow || work) {
      status = "in_progress";
      startedAt = startedAt ?? work?.ts ?? steps.liveRow?.ts ?? null;
    }
  }
  if (status === stub.status && live === stub.live && startedAt === stub.startedAt) return stub;
  return { ...stub, status, live, startedAt };
}

/**
 * The chat's state for its status line. `task_working` says a task turn
 * runs; when the chat shows task sections, its stubs are known and none of
 * them is live any more, that turn has ended — the probe just has not
 * noticed yet (it lagged 8–15 s). Then the line says what is true: ready.
 *
 * A review run is the exception (round-4 review r1 S2): its card stays in
 * `review`, so it is never live, yet the run is real — `reviewRun` (a stub
 * the agent's review run works) keeps the probe's word.
 */
export function statusLineState(
  state: StateEvent | null,
  {
    tasksShown,
    tasksKnown,
    liveTask,
    reviewRun = false,
  }: { tasksShown: boolean; tasksKnown: boolean; liveTask: boolean; reviewRun?: boolean },
): StateEvent | null {
  if (!state || state.status !== "task_working" || !tasksShown || !tasksKnown || liveTask || reviewRun) {
    return state;
  }
  return { ...state, status: "idle" };
}

/**
 * The header badge's aliveness, from the same state the status line shows
 * (round-4 user test t1). `aliveness` alone is the chat transcript's age
 * (written < 60 s ago = active): after a task turn it stayed "live" for
 * 12.9 s under a line that already said "Ready" (NICE-1), and while a task
 * ran in its own session the chat's quiet transcript gave the grey dot
 * (NICE-4). So: a line that reads ready is idle, a running turn is active,
 * an ended session stays ended.
 */
export function headerAliveness(aliveness: ChatAliveness, state: StateEvent | null): ChatAliveness {
  if (aliveness === "ended") return aliveness;
  if (state?.status === "idle") return aliveness === "active" ? "idle" : aliveness;
  if (state?.status === "working" || state?.status === "task_working") return "active";
  return aliveness;
}
