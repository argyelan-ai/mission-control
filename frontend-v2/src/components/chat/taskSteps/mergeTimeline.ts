/**
 * One timeline for the agent chat (spec A §6.2): chat events and the steps
 * of the agent's tasks, each at its time.
 *
 * Rows first, units second (M2): every task row goes to its own time among
 * the chat events; THEN a run of chat events becomes chat items
 * (`buildTimelineItems`, so a chat tool group never spans a task row) and a
 * run of one task's rows becomes a segment (`buildTaskUnits`, so a chapter
 * never spans a chat entry — tools written after the operator's message
 * land below it). Nothing of a task grows above a newer chat entry: the live
 * row, the "waiting" lines and the end line belong to the task's last
 * segment only while that segment is the newest entry; otherwise they form a
 * trailing continuation segment at the bottom. The end line keeps its time:
 * a task that ended before the next chat entry ends its last segment there.
 *
 * A later segment's head follows the task's state at that point (`head`):
 * work rows or a running task → "<title> · continues"; a task that waits for
 * the operator → title + state line; anything else that is not running (the
 * end line of a finished task, a vanished or not yet started task) → the
 * title alone — never "continues" for a task that does not run.
 *
 * Shapes are decided by the caller once per task per mount (M4): a card
 * stays a card (rows after its freeze time go below as segments), a section
 * stays a section.
 */
import type { ChatTaskStub, TaskLiveRow, TaskStepRow, TimelineChatEvent } from "@/lib/chatTypes";
import { buildTimelineItems, type TimelineItem } from "../buildTimelineItems";
import type { DetailLevel } from "../chatOptions";
import { buildTaskUnits, isRowVisible, rowMs, type TaskUnit, type UnitCtx } from "./buildTaskUnits";

export interface MergeTask {
  stub: ChatTaskStub;
  /** Fixed per mount by the caller (M4). */
  shape: "card" | "segments";
  /** Card only: rows newer than this are new work, shown below. */
  frozenAtMs: number | null;
  rows: readonly TaskStepRow[];
  liveRow: TaskLiveRow | null;
  omitted: number;
  hasSession: boolean;
  /** The steps query failed and nothing is loaded: a "could not be loaded"
   *  tail with a retry replaces the live / end tail (review SHOULD-2 on #937). */
  loadFailed?: boolean;
  /** The stub vanished in this mount after being live here. */
  detached: boolean;
  /** Show the end line (finished while seen in this mount). Default: a
   *  section that is no longer live. */
  endLine?: boolean;
}

/**
 * How a segment's head reads (spec A §2.5):
 * - `full`: title + state line (a task's first segment; a later one of a task
 *   that waits for the operator, so the band says why nothing runs);
 * - `continues`: "<title> · continues" — the task works on in this segment;
 * - `title`: the title alone — the segment holds no work and the task does
 *   not run (its end line or "no longer active here" says the state).
 */
export type SegmentHead = "full" | "continues" | "title";

export type MergedEntry =
  | { kind: "chat"; key: string; ms: number | null; item: TimelineItem }
  | { kind: "card"; key: string; ms: number | null; taskId: string }
  | {
      kind: "segment";
      key: string;
      taskId: string;
      /** The task's first segment (full header + state line). */
      first: boolean;
      head: SegmentHead;
      units: TaskUnit[];
    };

export interface MergeResult {
  entries: MergedEntry[];
  /** Newest time of any entry (chat item, card, task unit). */
  newestTs: number | null;
  /** Chat items + cards + task units newer than `ms` (S7 counter). */
  countNewer(ms: number): number;
}

export interface MergeInput {
  chatEvents: readonly TimelineChatEvent[];
  tasks: readonly MergeTask[];
  detailLevel: DetailLevel;
  /** More inbox tasks waiting (stubs `queued`). */
  queued: number;
  /** Chapter ids opened automatically (§6.3). */
  autoIds?: ReadonlySet<string>;
  /** The chat's status line already says "waiting for the model" (K3). */
  hidePending?: boolean;
  prev?: MergeResult | null;
}

const TAIL_KINDS = new Set<TaskUnit["u"]>(["live", "pending", "detached", "queued", "end", "notLoaded", "loadFailed"]);

/** Live states in which the task waits for the operator, not for its agent. */
const WAITS_FOR_OPERATOR = new Set(["waiting", "blocked", "user_test"]);

/** The head of a task's segment, from what it holds and the task's state now. */
export function segmentHead(task: MergeTask, units: readonly TaskUnit[], first: boolean): SegmentHead {
  if (first) return "full";
  if (units.some((u) => !TAIL_KINDS.has(u.u))) return "continues";
  if (task.detached || !task.stub.live) return "title";
  if (task.stub.status === "in_progress") return "continues";
  return WAITS_FOR_OPERATOR.has(task.stub.status) ? "full" : "title";
}

/** An entry's time: a chat item's / card's own, a segment's first timed unit. */
function entryMs(e: MergedEntry): number | null {
  if (e.kind !== "segment") return e.ms;
  for (const u of e.units) {
    const ms = "ts" in u ? rowMs(u.ts) : null;
    if (ms != null) return ms;
  }
  return null;
}

type Entry =
  | { type: "chat"; ms: number; rank: 0; seq: number; ev: TimelineChatEvent }
  | { type: "card"; ms: number; rank: 1; seq: number; task: MergeTask }
  | { type: "head"; ms: number; rank: 1; seq: number; task: MergeTask }
  | { type: "row"; ms: number; rank: 2; seq: number; task: MergeTask; row: TaskStepRow };

/** Times in order; a missing/invalid one inherits the previous entry's. */
function withTimes<T>(items: readonly T[], tsOf: (x: T) => string | null | undefined, start: number): [T, number][] {
  let last = start;
  return items.map((x) => {
    const ms = rowMs(tsOf(x));
    if (ms != null) last = ms;
    return [x, last];
  });
}

function stubStartMs(stub: ChatTaskStub): number | null {
  return rowMs(stub.startKey) ?? rowMs(stub.startedAt) ?? rowMs(stub.deliveredAt);
}

function sameArray<T>(a: readonly T[], b: readonly T[]): boolean {
  return a.length === b.length && a.every((x, i) => x === b[i]);
}

/** Parallel tool calls of one turn share the turn's uuid; their toolUseId differs. */
function evKey(ev: TimelineChatEvent): string {
  return ev.kind === "tool" ? (ev.toolUseId ?? ev.uuid) : ev.uuid;
}

function itemEvents(item: TimelineItem): TimelineChatEvent[] {
  return item.kind === "single" || item.kind === "agent" ? [item.event] : item.events;
}

export function mergeTimeline(input: MergeInput): MergeResult {
  const { chatEvents, tasks, detailLevel, queued, autoIds, hidePending, prev } = input;
  const all: Entry[] = [];
  let seq = 0;

  for (const [ev, ms] of withTimes(chatEvents, (e) => e.ts, Number.NEGATIVE_INFINITY)) {
    all.push({ type: "chat", ms, rank: 0, seq: seq++, ev });
  }

  for (const task of tasks) {
    const start = stubStartMs(task.stub);
    const visible = task.rows.filter((r) => isRowVisible(r, detailLevel));
    let rows = withTimes(visible, (r) => r.ts, start ?? Number.NEGATIVE_INFINITY);
    if (task.shape === "card") {
      all.push({ type: "card", ms: start ?? Number.NEGATIVE_INFINITY, rank: 1, seq: seq++, task });
      const frozen = task.frozenAtMs ?? Number.POSITIVE_INFINITY;
      rows = rows.filter(([, ms]) => ms > frozen);
    } else if (rows.length === 0) {
      const at = rowMs(task.stub.deliveredAt) ?? rowMs(task.stub.startedAt) ?? start ?? Number.NEGATIVE_INFINITY;
      all.push({ type: "head", ms: at, rank: 1, seq: seq++, task });
    }
    for (const [row, ms] of rows) all.push({ type: "row", ms, rank: 2, seq: seq++, task, row });
  }

  // Task entries in time order; then merged into the chat events, which keep
  // THEIR order (a reconnect re-seed may append an older chat line at the
  // end — the chat has always shown it there, and still does). A task entry
  // goes before the first chat event that is strictly newer (ties: chat first).
  const chatEntries = all.filter((e) => e.type === "chat");
  const taskEntries = all.filter((e) => e.type !== "chat").sort((a, b) => a.ms - b.ms || a.rank - b.rank || a.seq - b.seq);
  all.length = 0;
  let j = 0;
  for (const c of chatEntries) {
    while (j < taskEntries.length && taskEntries[j].ms < c.ms) all.push(taskEntries[j++]);
    all.push(c);
  }
  while (j < taskEntries.length) all.push(taskEntries[j++]);

  // Previous objects, for identity reuse (N5).
  const prevUnits = new Map<string, Map<string, TaskUnit>>();
  const prevSegments = new Map<string, Extract<MergedEntry, { kind: "segment" }>>();
  const prevChat = new Map<string, Extract<MergedEntry, { kind: "chat" }>>();
  const prevCards = new Map<string, Extract<MergedEntry, { kind: "card" }>>();
  for (const e of prev?.entries ?? []) {
    if (e.kind === "segment") {
      prevSegments.set(e.key, e);
      let m = prevUnits.get(e.taskId);
      if (!m) prevUnits.set(e.taskId, (m = new Map()));
      for (const u of e.units) m.set(u.key, u);
    } else if (e.kind === "chat") prevChat.set(e.key, e);
    else prevCards.set(e.key, e);
  }

  const out: MergedEntry[] = [];
  const unitTimes: number[] = [];
  const ctxOf = new Map<string, UnitCtx>();
  const segCount = new Map<string, number>();
  const lastSegIndex = new Map<string, number>();

  let chatRun: TimelineChatEvent[] = [];
  let seg: { task: MergeTask; rows: TaskStepRow[] } | null = null;

  const flushChat = () => {
    if (chatRun.length === 0) return;
    const run = chatRun;
    chatRun = [];
    for (const item of buildTimelineItems(run)) {
      const evs = itemEvents(item);
      const key = `chat:${item.kind}:${evs[0] ? evKey(evs[0]) : ""}`;
      const old = prevChat.get(key);
      const ms = rowMs(evs[0]?.ts);
      if (old && old.item.kind === item.kind && sameArray(itemEvents(old.item), evs)) out.push(old);
      else out.push({ kind: "chat", key, ms, item });
    }
  };

  const pushSegment = (task: MergeTask, units: TaskUnit[], keyHint: string) => {
    const id = task.stub.taskId;
    const n = segCount.get(id) ?? 0;
    const first = n === 0 && task.shape === "segments";
    const head = segmentHead(task, units, first);
    const key = `task:${id}:seg:${keyHint}`;
    const old = prevSegments.get(key);
    const entry =
      old && old.first === first && old.head === head && sameArray(old.units, units)
        ? old
        : ({ kind: "segment", key, taskId: id, first, head, units } as const);
    segCount.set(id, n + 1);
    lastSegIndex.set(id, out.length);
    out.push(entry);
  };

  const flushSeg = () => {
    if (!seg) return;
    const { task, rows } = seg;
    seg = null;
    const id = task.stub.taskId;
    const ctx = ctxOf.get(id) ?? { lastMarkMs: stubStartMs(task.stub) };
    const built = buildTaskUnits(rows, ctx, { detailLevel, autoIds, prev: prevUnits.get(id) });
    ctxOf.set(id, built.ctx);
    const units = built.units;
    for (const u of units) {
      const ms = "ts" in u ? rowMs(u.ts) : null;
      if (ms != null && u.u !== "mark") unitTimes.push(ms);
    }
    const firstSeg = (segCount.get(id) ?? 0) === 0 && task.shape === "segments";
    if (firstSeg && task.omitted > 0) {
      units.unshift({ u: "notLoaded", key: "notLoaded", count: task.omitted });
    }
    pushSegment(task, units, rows[0]?.id ?? "head");
  };

  for (const e of all) {
    if (e.type === "chat") {
      flushSeg();
      chatRun.push(e.ev);
      continue;
    }
    flushChat();
    if (e.type === "card") {
      flushSeg();
      const key = `task:${e.task.stub.taskId}:card`;
      const old = prevCards.get(key);
      const ms = Number.isFinite(e.ms) ? e.ms : null;
      out.push(old && old.ms === ms ? old : { kind: "card", key, ms, taskId: e.task.stub.taskId });
      if (ms != null) unitTimes.push(ms);
      continue;
    }
    if (seg && seg.task !== e.task) flushSeg();
    seg ??= { task: e.task, rows: [] };
    if (e.type === "row") seg.rows.push(e.row);
  }
  flushChat();
  flushSeg();

  // Tails (§6.2 step 5): live row / pending / detached / queued / end line.
  const newestLive = [...tasks].filter((t) => t.stub.live && !t.detached).pop() ?? null;
  const trailing: { task: MergeTask; units: TaskUnit[] }[] = [];
  for (const task of tasks) {
    const id = task.stub.taskId;
    const tail: TaskUnit[] = [];
    if (task.detached) {
      tail.push({ u: "detached", key: "tail:detached" });
    } else if (task.loadFailed && task.shape === "segments") {
      tail.push({ u: "loadFailed", key: "tail:loadFailed" });
      if (task.stub.live && task === newestLive && queued > 0) tail.push({ u: "queued", key: "tail:queued", count: queued });
    } else if (task.stub.live) {
      if (!task.hasSession && !task.liveRow) {
        if (!hidePending) tail.push({ u: "pending", key: "tail:pending" });
      } else {
        const last = task.rows.length > 0 ? task.rows[task.rows.length - 1].ts : null;
        tail.push({ u: "live", key: "tail:live", liveRow: task.liveRow, lastTs: task.liveRow?.ts ?? last });
      }
      if (task === newestLive && queued > 0) tail.push({ u: "queued", key: "tail:queued", count: queued });
    } else if (task.endLine ?? task.shape === "segments") {
      tail.push({ u: "end", key: "tail:end" });
    }
    if (tail.length === 0) continue;
    const at = lastSegIndex.get(id);
    // Only while the task's last segment is the newest entry of all (M2) —
    // or, for the end line alone, while the task ended before the entry that
    // follows its last segment (the end is not newer than that entry; ties:
    // the chat first, as in the merge).
    const endedMs = rowMs(task.stub.endedAt);
    const endsBeforeNext =
      at != null &&
      at < out.length - 1 &&
      tail.length === 1 &&
      tail[0].u === "end" &&
      endedMs != null &&
      (entryMs(out[at + 1]) ?? Number.NEGATIVE_INFINITY) > endedMs;
    if (at != null && (at === out.length - 1 || endsBeforeNext)) {
      const segEntry = out[at] as Extract<MergedEntry, { kind: "segment" }>;
      const units = [...segEntry.units, ...tail.map((u) => reuseTail(u, prevSegments.get(segEntry.key)))];
      const head = segmentHead(task, units, segEntry.first);
      const old = prevSegments.get(segEntry.key);
      out[at] =
        old && old.first === segEntry.first && old.head === head && sameArray(old.units, units)
          ? old
          : { ...segEntry, head, units };
    } else {
      trailing.push({ task, units: tail });
    }
  }
  for (const { task, units } of trailing) {
    const key = `task:${task.stub.taskId}:seg:tail`;
    const old = prevSegments.get(key);
    const reused = units.map((u) => reuseTail(u, old));
    const first = !lastSegIndex.has(task.stub.taskId) && task.shape === "segments";
    const head = segmentHead(task, reused, first);
    out.push(
      old && old.first === first && old.head === head && sameArray(old.units, reused)
        ? old
        : { kind: "segment", key, taskId: task.stub.taskId, first, head, units: reused },
    );
  }

  for (const e of out) if (e.kind === "chat" && e.ms != null) unitTimes.push(e.ms);
  const newestTs = unitTimes.length > 0 ? Math.max(...unitTimes) : null;
  return {
    entries: out,
    newestTs,
    countNewer: (ms: number) => unitTimes.filter((t) => t > ms).length,
  };
}

/** Tail units are compared by value against the same segment's last build. */
function reuseTail(unit: TaskUnit, old: Extract<MergedEntry, { kind: "segment" }> | undefined): TaskUnit {
  const prior = old?.units.find((u) => u.key === unit.key);
  if (!prior || !TAIL_KINDS.has(prior.u)) return unit;
  const a = prior as Record<string, unknown>;
  const b = unit as Record<string, unknown>;
  const same = Object.keys(b).every((k) => a[k] === b[k]) && Object.keys(a).length === Object.keys(b).length;
  return same ? prior : unit;
}
