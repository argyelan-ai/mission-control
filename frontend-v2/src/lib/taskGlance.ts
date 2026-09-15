/**
 * taskGlance — pure helpers behind the task "cockpit" header: a plain-language
 * summary, a human status line, and a silence measure. No React, no fetch —
 * everything here is unit-tested in isolation (lib/__tests__/taskGlance.test.ts).
 */
import type { TaskStatus } from "./types";

const SUMMARY_MAX = 220;

/**
 * First real paragraph of a markdown description, stripped of headings, inline
 * code ticks and link syntax, capped at SUMMARY_MAX chars on a word boundary.
 * Used as the fallback "what is this about" line until a task carries its own
 * plain-language summary.
 */
export function plainSummary(description: string | null | undefined): string {
  if (!description) return "";
  const paragraphs = description
    .split(/\n\s*\n/)
    .map((p) =>
      p
        .split("\n")
        .filter((line) => !/^\s*#{1,6}\s/.test(line)) // drop heading lines
        .join(" ")
        .trim(),
    )
    .filter(Boolean);
  const first = paragraphs[0];
  if (!first) return "";
  const text = first
    .replace(/`([^`]*)`/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
  if (text.length <= SUMMARY_MAX) return text;
  const cut = text.slice(0, SUMMARY_MAX);
  const lastSpace = cut.lastIndexOf(" ");
  return (lastSpace > 0 ? cut.slice(0, lastSpace) : cut).trimEnd() + "…";
}

export type GlanceTone = "warning" | "info" | "error" | "online" | "muted";

/** i18n key (tasks.* namespace) + colour tone for the human status line. */
export function humanStatus(task: { status: TaskStatus }): { key: string; tone: GlanceTone } {
  switch (task.status) {
    case "waiting":
      return { key: "glanceWaitingForYou", tone: "warning" };
    case "in_progress":
      return { key: "glanceWorking", tone: "info" };
    case "review":
    case "user_test":
      return { key: "glanceNeedsAcceptance", tone: "warning" };
    case "blocked":
      return { key: "glanceStuck", tone: "error" };
    case "failed":
      return { key: "glanceFailed", tone: "error" };
    case "aborted":
      return { key: "glanceAborted", tone: "muted" };
    case "done":
      return { key: "glanceDone", tone: "online" };
    case "inbox":
    default:
      return { key: "glanceNotStarted", tone: "muted" };
  }
}

/** Whole hours since `lastActivityAt`; null when there is no timestamp. */
export function silenceHours(lastActivityAt: string | null | undefined, now: Date = new Date()): number | null {
  if (!lastActivityAt) return null;
  const then = new Date(lastActivityAt).getTime();
  if (Number.isNaN(then)) return null;
  return Math.max(0, Math.floor((now.getTime() - then) / 3_600_000));
}
