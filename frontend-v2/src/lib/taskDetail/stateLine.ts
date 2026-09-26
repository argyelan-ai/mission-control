import type { Agent, Task } from "@/lib/types";
import type { HeadState } from "@/lib/heads";
import { runDurationSeconds } from "@/lib/heads";
import type { StateCard } from "./stateCard";
import { formatDuration, secondsBetween } from "./format";
import { STATUS_LABEL_KEY } from "./statusLabels";

/**
 * The one-line state sentence under the task title (DESIGN.md K10/K12):
 *
 *   ● Blocked · Rex asked 42 min ago
 *   ● In progress · Hermes for 42 min
 *   ● Done · Hermes took 2 h 17 min
 *   ● Failed · ended 18 min ago
 *   ● Passed · finished 9 min ago          (head run)
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

const STATUS_TONE: Record<string, StateTone> = {
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
  const statusWord = { ns: "tasks" as const, key: STATUS_LABEL_KEY[task.status] };
  const statusTone = STATUS_TONE[task.status] ?? "muted";

  if (card?.kind === "head") {
    const run = card.run;
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
      detail: duration ? withName(agentName, "took", { duration }) : agentName ? { key: "with", values: { name: agentName } } : null,
    };
  }

  if (card?.kind === "failed") {
    const span = spanSince(task.completed_at ?? task.updated_at, locale, now);
    return { tone: statusTone, word: statusWord, detail: span ? { key: "endedAgo", values: { span } } : null };
  }

  // No card: inbox, review, anything else.
  if (task.status === "inbox") return { tone: statusTone, word: statusWord, detail: { key: "notStarted", values: {} } };
  return { tone: statusTone, word: statusWord, detail: agentName ? { key: "with", values: { name: agentName } } : null };
}
