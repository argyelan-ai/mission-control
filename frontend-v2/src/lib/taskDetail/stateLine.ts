import type { Agent, Task } from "@/lib/types";
import type { HeadState } from "@/lib/heads";
import { runDurationSeconds } from "@/lib/heads";
import type { StateCard } from "./stateCard";
import type { ChatTaskStub } from "@/lib/chatTypes";
import { formatDuration, secondsBetween } from "./format";
import { STATUS_LABEL_KEY } from "./statusLabels";

/**
 * The one-line state sentence under the task title (DESIGN.md K10/K12):
 *
 *   ● Blocked · alpha asked 42 min ago
 *   ● In progress · alpha for 42 min
 *   ● Done · alpha took 2 h 17 min
 *   ● Failed · ended 18 min ago
 *   ● Passed · finished 9 min ago          (head run)
 *   ● Head failed · task awaits review     (head and card disagree)
 *
 * One word for the state (the status label from i18n, or the head state),
 * at most one "·", exactly one time — the one that belongs to the state.
 * The age of the card itself lives in the properties. Pure, so every state
 * is testable; the component only renders it.
 */

export type StateTone = "error" | "info" | "online" | "warning" | "accent" | "muted";

export type StateLine = {
  tone: StateTone;
  /** Message key of the state word: `tasks.<key>` or `heads.<key>`. */
  word: { ns: "tasks" | "heads"; key: string };
  /** `tasks.detail.line.<key>` with its values, or null for no detail. */
  detail: { key: string; values: Record<string, string> } | null;
};

export const STATUS_TONE: Record<string, StateTone> = {
  inbox: "muted",
  in_progress: "info",
  review: "warning",
  user_test: "accent",
  waiting: "info",
  blocked: "error",
  failed: "error",
  aborted: "warning",
  done: "online",
};

const HEAD_TONE: Record<HeadState, StateTone> = {
  starting: "info",
  running: "info",
  // Waiting on the operator = the brightest tone, not a hue (DESIGN.md).
  needs_you: "accent",
  passed: "online",
  failed: "error",
  stopped: "muted",
};

/** Card statuses that tell the same story as the head state. */
const HEAD_MATCHING_STATUS: Record<HeadState, ReadonlySet<string>> = {
  starting: new Set(["in_progress"]),
  running: new Set(["in_progress"]),
  needs_you: new Set(["blocked", "waiting", "user_test"]),
  passed: new Set(["done"]),
  failed: new Set(["failed", "aborted"]),
  stopped: new Set(["aborted", "failed"]),
};

/**
 * A time span in ONE unit, for "for …" / "… ago" / "seit …" / "vor …":
 * "42 min", "3 h", "5 days" / "5 Tagen" (dative after seit/vor).
 */
export function formatSpan(seconds: number | null | undefined, locale: string = "en"): string | null {
  if (seconds == null || Number.isNaN(seconds)) return null;
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return "<1 min";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} h`;
  const d = Math.floor(h / 24);
  if (locale === "de") return d === 1 ? "1 Tag" : `${d} Tagen`;
  return d === 1 ? "1 day" : `${d} days`;
}

const spanSince = (ts: string | null | undefined, locale: string, now: Date) =>
  formatSpan(secondsBetween(ts, null, now), locale);

function withName(name: string | undefined, key: string, values: Record<string, string>) {
  return name ? { key: `${key}Named`, values: { ...values, name } } : { key, values };
}

export function deriveStateLine({
  task,
  card,
  agents,
  locale = "en",
  now = new Date(),
}: {
  task: Task;
  card: StateCard | null;
  agents: Agent[];
  locale?: string;
  now?: Date;
}): StateLine {
  const agentName = agents.find((a) => a.id === task.assigned_agent_id)?.name;
  // The agent that delivered the card (round-2 Q5): the hand-off to review
  // clears or moves the assignment, the status history keeps the builder.
  const delivererName = task.delivered_by_agent_id
    ? agents.find((a) => a.id === task.delivered_by_agent_id)?.name
    : undefined;
  const statusWord = { ns: "tasks" as const, key: STATUS_LABEL_KEY[task.status] };
  const statusTone = STATUS_TONE[task.status] ?? "muted";

  if (card?.kind === "head") {
    const run = card.run;
    // Head and card can tell different stories (a failed head leaves the card
    // in review). Then the line names both instead of one time:
    // "Head failed · task awaits review".
    if (!HEAD_MATCHING_STATUS[run.state].has(task.status)) {
      return {
        tone: HEAD_TONE[run.state],
        word: { ns: "tasks", key: `detail.headState.${run.state}` },
        detail: { key: `task.${task.status}`, values: {} },
      };
    }
    const word = { ns: "heads" as const, key: `state.${run.state}` };
    const ended = run.exited_at ?? run.started_at ?? run.created_at;
    if (run.state === "starting") return { tone: "info", word, detail: null };
    if (run.state === "running") {
      if (card.silentWarn) {
        const span = formatSpan(run.silent_s, locale);
        return { tone: "warning", word, detail: span ? { key: "silentFor", values: { span } } : null };
      }
      const span = formatSpan(runDurationSeconds(run, now.getTime()), locale);
      return { tone: "info", word, detail: span ? { key: "for", values: { span } } : null };
    }
    const span = spanSince(ended, locale, now);
    if (!span) return { tone: HEAD_TONE[run.state], word, detail: null };
    const key = run.state === "needs_you" ? "askedAgo" : run.state === "passed" ? "finishedAgo" : "endedAgo";
    return { tone: HEAD_TONE[run.state], word, detail: { key, values: { span } } };
  }

  if (card?.kind === "needs_you") {
    const asker = card.askerAgentId ? agents.find((a) => a.id === card.askerAgentId)?.name : undefined;
    const span = spanSince(card.since, locale, now);
    return {
      tone: statusTone,
      word: statusWord,
      detail: span ? withName(asker, "askedAgo", { span }) : asker ? { key: "with", values: { name: asker } } : null,
    };
  }

  if (card?.kind === "running") {
    const span = spanSince(card.startedAt, locale, now);
    return {
      tone: statusTone,
      word: statusWord,
      detail: span ? withName(agentName, "for", { span }) : agentName ? { key: "with", values: { name: agentName } } : null,
    };
  }

  if (card?.kind === "result") {
    const duration = formatDuration(card.durationSeconds, locale);
    return {
      tone: statusTone,
      word: statusWord,
      // Who did the work: the deliverer when known (a reviewer may hold the
      // card now, or nobody after the operator's review).
      detail: duration
        ? withName(delivererName ?? agentName, "took", { duration })
        : agentName
          ? { key: "with", values: { name: agentName } }
          : null,
    };
  }

  if (card?.kind === "failed") {
    const span = spanSince(task.completed_at ?? task.updated_at, locale, now);
    return { tone: statusTone, word: statusWord, detail: span ? { key: "endedAgo", values: { span } } : null };
  }

  if (card?.kind === "waiting_model") {
    // The model does not answer: the same warning tone as the chat's
    // "Waiting for the model" (K10). Delivered and answering: still calm.
    return {
      tone: card.ready ? statusTone : "warning",
      word: statusWord,
      detail: { key: card.ready ? "waitingFirstAnswer" : "waitingModel", values: {} },
    };
  }

  // No card: inbox, review, anything else.
  if (task.status === "inbox") return { tone: statusTone, word: statusWord, detail: { key: "notStarted", values: {} } };
  if (agentName) return { tone: statusTone, word: statusWord, detail: { key: "with", values: { name: agentName } } };
  // Nobody holds it (the operator's review): name who delivered it — "with"
  // would claim the agent still has it.
  return {
    tone: statusTone,
    word: statusWord,
    detail: delivererName ? { key: "deliveredBy", values: { name: delivererName } } : null,
  };
}

/**
 * The state line of a task section in the agent chat (spec A §2.5, S4):
 * "● Task done · took 1 h 4 min". The same keys and formatters as
 * `deriveStateLine` — the state word is the task's own sentence word
 * (`tasks.detail.line.task.<status>`), the detail `for` / `took` /
 * `endedAgo` / `waitingFirstAnswer` / `notStarted`. No agent name: the chat
 * IS that agent; a task that moved on to another agent says `with`.
 */
export function deriveChatTaskLine(stub: ChatTaskStub, locale: string = "en", now: Date = new Date()): StateLine {
  const tone = STATUS_TONE[stub.status] ?? "muted";
  const word = { ns: "tasks" as const, key: `detail.line.task.${stub.status}` };
  // The CURRENT run (round-7 re-check t1 NICE-3): after a rework the line
  // counted from the first start — "took 9 min" for 88 s + 48 s of work, the
  // wait for the review included. A second run says which run it is.
  const start = stub.runStartedAt ?? stub.startedAt ?? stub.startKey;
  const run = (stub.runs ?? 0) > 1 ? String(stub.runs) : null;
  if (stub.assignee) return { tone, word, detail: { key: "with", values: { name: stub.assignee } } };
  switch (stub.status) {
    case "in_progress": {
      const span = spanSince(start, locale, now);
      if (!span) return { tone, word, detail: null };
      return { tone, word, detail: run ? { key: "forRun", values: { span, n: run } } : { key: "for", values: { span } } };
    }
    case "done":
    case "review": {
      const duration = stub.endedAt ? formatDuration(secondsBetween(start, stub.endedAt, now), locale) : null;
      if (!duration) return { tone, word, detail: null };
      return { tone, word, detail: run ? { key: "tookRun", values: { duration, n: run } } : { key: "took", values: { duration } } };
    }
    case "failed":
    case "aborted": {
      const span = spanSince(stub.endedAt, locale, now);
      return { tone, word, detail: span ? { key: "endedAgo", values: { span } } : null };
    }
    case "inbox":
      return { tone, word, detail: { key: stub.deliveredAt ? "waitingFirstAnswer" : "notStarted", values: {} } };
    default:
      return { tone, word, detail: null };
  }
}

/** First letter up: a state word that also stands mid-sentence ("task in
 *  progress") opens a line in the chat. */
export function capitalize(text: string, locale: string = "en"): string {
  return text ? text.charAt(0).toLocaleUpperCase(locale) + text.slice(1) : text;
}
