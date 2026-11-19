/**
 * The visible units of ONE segment of a task section (spec A §2.4, §6.2
 * step 3) — pure, so every rule is testable without rendering.
 *
 * A thinking phase is a chapter: its first sentence, then the tools that
 * follow it. Anything else the agent writes (a text, a comment, a system
 * line) closes the chapter, so units stay in time order and a later chat
 * entry can split the segment exactly where it happened (M2). Tools before
 * the first thought of a segment are plain tool rows — no invented title.
 *
 * Runs per segment, with a context folded across the task's segments
 * (`lastMarkMs`, the last time shown) — a time mark appears before the first
 * unit ≥ 15 min after the last shown time of that task (D5, K18).
 */
import type { TaskStepRow } from "@/lib/chatTypes";
import type { DetailLevel } from "../chatOptions";
import { parseTs } from "@/lib/taskDetail/format";

export const TIME_MARK_GAP_MS = 15 * 60_000;

export type ToolStep = Extract<TaskStepRow, { k: "tool" }>;
type Row<K extends TaskStepRow["k"]> = Extract<TaskStepRow, { k: K }>;

export type TaskUnit =
  | {
      u: "chapter";
      key: string;
      id: string;
      ts: string | null;
      lead: string;
      more: boolean;
      /** Consecutive chapters with the same first sentence, folded (`2×`). */
      count: number;
      /** Summed thinking time, or null when the harness recorded none. */
      ms: number | null;
      tools: ToolStep[];
      /** Expanded unless the reader closed it (Ausführlich, or the newest
       *  chapter of a live task while the view follows). */
      open: boolean;
    }
  | { u: "tool"; key: string; id: string; ts: string | null; row: ToolStep }
  | { u: "text"; key: string; id: string; ts: string | null; row: Row<"text"> }
  | { u: "comment"; key: string; id: string; ts: string | null; row: Row<"comment"> }
  | {
      u: "system";
      key: string;
      id: string;
      ts: string | null;
      what: string;
      decision: string | null;
      request: boolean;
    }
  | { u: "delivery"; key: string; id: string; ts: string | null; lead: string }
  | { u: "attempt"; key: string; id: string; ts: string | null; n: number }
  | { u: "mark"; key: string; ts: string | null }
  | { u: "notLoaded"; key: string; count: number }
  // Tail units (added by mergeTimeline, never by buildTaskUnits):
  | {
      u: "live";
      key: string;
      liveRow: import("@/lib/chatTypes").TaskLiveRow | null;
      lastTs: string | null;
      /** The only content of its band (a new band under the operator's
       *  message): with nothing streaming yet it says "Thinking…" instead of
       *  leaving the band empty (round-3 t1 NICE-1). */
      alone?: boolean;
    }
  | { u: "pending"; key: string }
  /** The steps query failed before any row came in (says so, offers a retry). */
  | { u: "loadFailed"; key: string }
  | { u: "detached"; key: string }
  | { u: "queued"; key: string; count: number }
  | { u: "end"; key: string };

export interface UnitCtx {
  /** The last time shown for this task (a mark, an attempt, or its start). */
  lastMarkMs: number | null;
}

export interface UnitOpts {
  detailLevel: DetailLevel;
  /** Chapter ids opened automatically (newest live chapter while following). */
  autoIds?: ReadonlySet<string>;
  /** Units of the previous build, by key — equal units keep their object. */
  prev?: ReadonlyMap<string, TaskUnit>;
}

export function rowMs(ts: string | null | undefined): number | null {
  const d = parseTs(ts ?? null);
  return d ? d.getTime() : null;
}

/** Which rows a detail level shows at all (D13, §2.3). */
export function isRowVisible(row: TaskStepRow, level: DetailLevel): boolean {
  if (level !== "compact") return true;
  return row.k === "text" || row.k === "comment" || row.k === "attempt";
}

function sameArray<T>(a: readonly T[], b: readonly T[]): boolean {
  return a.length === b.length && a.every((x, i) => x === b[i]);
}

/** Two units say the same thing (rows are shared objects, see stepsState). */
export function sameUnit(a: TaskUnit, b: TaskUnit): boolean {
  if (a === b) return true;
  if (a.u !== b.u || a.key !== b.key) return false;
  const ra = a as Record<string, unknown>;
  const rb = b as Record<string, unknown>;
  for (const k of Object.keys(ra)) {
    const va = ra[k];
    const vb = rb[k];
    if (Array.isArray(va) && Array.isArray(vb)) {
      if (!sameArray(va, vb)) return false;
    } else if (va !== vb) {
      return false;
    }
  }
  return Object.keys(rb).length === Object.keys(ra).length;
}

export function reuse<T extends TaskUnit>(unit: T, prev?: ReadonlyMap<string, TaskUnit>): T {
  const old = prev?.get(unit.key);
  return old && sameUnit(old, unit) ? (old as T) : unit;
}

export function buildTaskUnits(
  rows: readonly TaskStepRow[],
  ctx: UnitCtx,
  opts: UnitOpts,
): { units: TaskUnit[]; ctx: UnitCtx } {
  const { detailLevel, autoIds, prev } = opts;
  const compact = detailLevel === "compact";
  const units: TaskUnit[] = [];
  let lastMarkMs = ctx.lastMarkMs;
  // (cast: TS would narrow a `let … = null` to `null` across the closures below)
  let chapter = null as Extract<TaskUnit, { u: "chapter" }> | null;
  const consumed = new Set<string>();

  const closeChapter = () => {
    if (chapter) units.push(reuse(chapter, prev));
    chapter = null;
  };

  const markBefore = (ts: string | null) => {
    if (compact) return;
    const ms = rowMs(ts);
    if (ms == null) return;
    if (lastMarkMs == null) {
      lastMarkMs = ms;
      return;
    }
    if (ms - lastMarkMs >= TIME_MARK_GAP_MS) {
      units.push(reuse({ u: "mark", key: `mark:${ts}`, ts }, prev));
      lastMarkMs = ms;
    }
  };

  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    if (consumed.has(row.id) || !isRowVisible(row, detailLevel)) continue;

    if (row.k === "tool" && chapter) {
      const ch: Extract<TaskUnit, { u: "chapter" }> = chapter;
      chapter = { ...ch, tools: [...ch.tools, row] };
      continue;
    }

    if (row.k === "think") {
      const ch: Extract<TaskUnit, { u: "chapter" }> | null = chapter;
      if (ch && ch.lead === row.lead) {
        // Same first sentence again (a retry loop): one chapter, "2×".
        chapter = {
          ...ch,
          count: ch.count + 1,
          ms: ch.ms == null && row.ms == null ? null : (ch.ms ?? 0) + (row.ms ?? 0),
          more: ch.more || !!row.more,
          open: ch.open || detailLevel === "verbose" || !!autoIds?.has(row.id),
        };
        continue;
      }
      closeChapter();
      markBefore(row.ts);
      chapter = {
        u: "chapter",
        key: `unit:${row.id}`,
        id: row.id,
        ts: row.ts,
        lead: row.lead,
        more: !!row.more,
        count: 1,
        ms: typeof row.ms === "number" ? row.ms : null,
        tools: [],
        open: detailLevel === "verbose" || !!autoIds?.has(row.id),
      };
      continue;
    }

    closeChapter();

    if (row.k === "attempt") {
      units.push(reuse({ u: "attempt", key: `unit:${row.id}`, id: row.id, ts: row.ts, n: row.n }, prev));
      const ms = rowMs(row.ts);
      if (ms != null) lastMarkMs = ms;
      continue;
    }

    markBefore(row.ts);
    switch (row.k) {
      case "tool":
        units.push(reuse({ u: "tool", key: `unit:${row.id}`, id: row.id, ts: row.ts, row }, prev));
        break;
      case "text":
        units.push(reuse({ u: "text", key: `unit:${row.id}`, id: row.id, ts: row.ts, row }, prev));
        break;
      case "comment":
        units.push(reuse({ u: "comment", key: `unit:${row.id}`, id: row.id, ts: row.ts, row }, prev));
        break;
      case "delivery":
        units.push(reuse({ u: "delivery", key: `unit:${row.id}`, id: row.id, ts: row.ts, lead: row.lead }, prev));
        break;
      case "system": {
        let decision = row.decision ?? null;
        if (row.request && decision == null) {
          // The decision that answers this request (N8): one row, not two.
          for (let j = i + 1; j < rows.length; j++) {
            const r = rows[j];
            if (r.k === "system" && !r.request && r.decision && r.what === row.what && !consumed.has(r.id)) {
              decision = r.decision;
              consumed.add(r.id);
              break;
            }
          }
        }
        units.push(
          reuse(
            { u: "system", key: `unit:${row.id}`, id: row.id, ts: row.ts, what: row.what, decision, request: !!row.request },
            prev,
          ),
        );
        break;
      }
    }
  }
  closeChapter();
  return { units, ctx: { lastMarkMs } };
}

/**
 * Line 2 of a chapter (§2.4): the running tool first, then the first tool
 * titles — at most two — and "+N" for the rest; whether one failed or runs.
 */
export function chapterSummary(tools: readonly ToolStep[]): {
  titles: string[];
  more: number;
  failed: boolean;
  running: boolean;
} {
  const running = tools.find((t) => t.st === "running") ?? null;
  const ordered = running ? [running, ...tools.filter((t) => t !== running)] : [...tools];
  // One line each: a title can hold a newline (a script's first line).
  const titles = ordered.slice(0, 2).map((t) => t.title.replace(/\s+/g, " ").trim());
  return {
    titles,
    more: Math.max(0, tools.length - titles.length),
    failed: tools.some((t) => t.st === "error"),
    running: !!running,
  };
}
