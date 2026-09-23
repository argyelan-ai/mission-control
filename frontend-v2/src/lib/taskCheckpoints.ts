/**
 * taskCheckpoints — turns the unified task timeline into the short list of
 * "checkpoints" the cockpit shows at the top: milestones (dispatched, acked,
 * blocked), status changes, and the agents' progress-type comments. Plain
 * chatter (message / feedback / reflection / system) stays in the
 * conversation. The retired task_checkpoints table is intentionally not a
 * source — agents report progress through comments today.
 */
import type { TaskTimelineEntry } from "./types";

export type CheckpointTone = "accent" | "warning" | "error" | "online" | "muted";

export interface Checkpoint {
  ts: string;
  kind: string;
  tone: CheckpointTone;
  text: string;
  actor: string | null;
  source: TaskTimelineEntry["source"];
}

const TONE_BY_KIND: Record<string, CheckpointTone> = {
  // milestones
  created: "muted",
  dispatched: "accent",
  acked: "accent",
  blocked: "error",
  // task events
  status_change: "accent",
  // comment types that mark progress
  progress: "accent",
  checkpoint: "accent",
  subtask_completed: "accent",
  phase_approved: "online",
  resolution: "online",
  handoff: "accent",
  blocker: "error",
  waiting_on_callback: "warning",
  escalate_to_operator: "warning",
};

const CHECKPOINT_KINDS = new Set(Object.keys(TONE_BY_KIND));

const LINE_MAX = 110;

/**
 * First meaningful line of a comment, stripped of markdown and the agents'
 * protocol prefixes ("**Update** —"), capped on a word boundary.
 */
export function checkpointLine(detail: string | null | undefined, fallbackTitle = ""): string {
  const lines = (detail ?? "")
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l && !/^#{1,6}\s/.test(l));
  let line = lines[0] ?? "";
  line = line
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^(update|progress|status|checkpoint)\s*[—–:-]\s*/i, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!line) line = fallbackTitle;
  if (line.length <= LINE_MAX) return line;
  const cut = line.slice(0, LINE_MAX);
  const sp = cut.lastIndexOf(" ");
  return (sp > 0 ? cut.slice(0, sp) : cut).trimEnd() + "…";
}

/** Newest first, capped. */
export function deriveCheckpoints(
  entries: TaskTimelineEntry[],
  { limit = 8 }: { limit?: number } = {},
): Checkpoint[] {
  return entries
    .filter((e) => CHECKPOINT_KINDS.has(e.kind))
    .sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0))
    .slice(0, limit)
    .map((e) => ({
      ts: e.ts,
      kind: e.kind,
      tone: TONE_BY_KIND[e.kind] ?? "muted",
      text: e.source === "comment" ? checkpointLine(e.detail, e.title) : e.title,
      actor: e.actor ?? null,
      source: e.source,
    }));
}
