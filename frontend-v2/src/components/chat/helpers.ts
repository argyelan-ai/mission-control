/**
 * Helpers (subagents) in the chat — who was started, who is still working,
 * how long it took. Pure logic behind `HelperLine` ("4 helpers · 3 working").
 *
 * Why this is its own module: the first chat round could not say "working".
 * A delegated job in today's Claude Code starts in the BACKGROUND, so its tool
 * result arrives a second after launch and only says "launched". The card read
 * "has a result" as "finished" — every helper showed as done the moment it
 * started. The backend now hands over the launch state (`ToolEvent.helpers`),
 * and the END arrives on its own later:
 *
 *   - a `notification` (Claude `<task-notification>`, omp `async-result`) —
 *     by tool id, or by the helper's own id (`taskId`);
 *   - for a Claude team member: its `idle_notification` teammate message.
 *
 * Rule: an end is only ever taken from an OBSERVATION. Nothing here turns "no
 * news for a while" into "done".
 */
import type { ChatEvent, HelperSpawn, MessageEvent, NotificationEvent, ToolEvent } from "@/lib/chatTypes";
import { secondsBetween } from "@/lib/taskDetail/format";

export type HelperState = HelperSpawn["state"];

/** A tool call that started helpers. Claude Code: `Agent` (older `Task`).
 *  omp: `task` — but only WITH the backend's helper data, since a lowercase
 *  "task" tool could mean something else in another harness. */
export function isHelperSpawn(ev: ChatEvent): ev is ToolEvent {
  if (ev.kind !== "tool") return false;
  if (ev.name === "Agent" || ev.name === "Task") return true;
  return ev.name === "task" && Array.isArray(ev.helpers) && ev.helpers.length > 0;
}

export interface HelperView {
  key: string;
  /** The call that started this helper (one call can start several in omp). */
  spawn: ToolEvent;
  id: string | null;
  /** The helper's own name if the harness gave one (team member, omp job). */
  name: string | null;
  agent: string | null;
  state: HelperState;
  startedAt: string;
  /** Measured run time, or report time minus start. `null` while working. */
  durationMs: number | null;
}

interface Report {
  state: HelperState;
  ts: string;
  durationMs: number | null;
}

export interface HelperReports {
  byTool: Map<string, Report>;
  byTask: Map<string, Report>;
  byTeammate: Map<string, Report>;
}

const DONE = new Set(["completed", "done", "success"]);
const FAILED = new Set(["failed", "error", "killed", "aborted", "cancelled", "stopped"]);

function reportOf(ev: NotificationEvent): Report | null {
  const status = (ev.status ?? "").trim().toLowerCase();
  const state: HelperState | null = DONE.has(status) ? "done" : FAILED.has(status) ? "failed" : null;
  if (!state) return null;
  const ms = ev.durationMs;
  return { state, ts: ev.ts, durationMs: typeof ms === "number" && ms >= 0 ? ms : null };
}

/** A team member that went idle (Claude Code teams): its job is done until
 *  someone wakes it. The message text is JSON the CLI writes itself. */
function idleOf(ev: MessageEvent): string | null {
  if (ev.role !== "teammate" || !ev.teammate) return null;
  const text = ev.text.trim();
  if (!text.startsWith("{") || !text.includes("idle_notification")) return null;
  try {
    const parsed = JSON.parse(text) as { type?: unknown };
    return parsed.type === "idle_notification" ? ev.teammate : null;
  } catch {
    return null;
  }
}

/** Every observed end, once per stream. The LAST report wins — a job can
 *  report more than once, and the later state is the true one. */
export function helperReports(events: readonly ChatEvent[]): HelperReports {
  const out: HelperReports = { byTool: new Map(), byTask: new Map(), byTeammate: new Map() };
  for (const ev of events) {
    if (ev.kind === "notification") {
      const r = reportOf(ev);
      if (!r) continue;
      if (ev.toolUseId) out.byTool.set(ev.toolUseId, r);
      if (ev.taskId) out.byTask.set(ev.taskId, r);
    } else if (ev.kind === "message") {
      const who = idleOf(ev);
      if (who) out.byTeammate.set(who, { state: "done", ts: ev.ts, durationMs: null });
    }
  }
  return out;
}

/** Without backend data (an older backend): the old reading of the result. */
function legacyState(ev: ToolEvent): HelperState {
  if (ev.status === "error") return "failed";
  return ev.result === null ? "working" : "done";
}

function spanMs(start: string, end: string): number | null {
  const s = secondsBetween(start, end);
  return s == null || s < 0 ? null : s * 1000;
}

export function collectHelpers(spawns: readonly ToolEvent[], reports: HelperReports): HelperView[] {
  const out: HelperView[] = [];
  for (const ev of spawns) {
    const listed: (HelperSpawn | null)[] = ev.helpers?.length ? ev.helpers : [null];
    // A report on the CALL id is only unambiguous for a call with one helper.
    const single = listed.length === 1;
    listed.forEach((spawned, index) => {
      const report =
        (spawned?.id ? reports.byTask.get(spawned.id) : undefined) ??
        (single && ev.toolUseId ? reports.byTool.get(ev.toolUseId) : undefined) ??
        (spawned?.name ? reports.byTeammate.get(spawned.name) : undefined);
      const base = spawned?.state ?? legacyState(ev);
      const state = report?.state ?? base;
      const durationMs =
        report?.durationMs ??
        (report ? spanMs(ev.ts, report.ts) : null) ??
        (state !== "working" ? spawned?.durationMs ?? null : null);
      out.push({
        key: `${ev.toolUseId ?? ev.uuid}#${index}`,
        spawn: ev,
        id: spawned?.id ?? null,
        // omp names its jobs by id ("ScoutPreflight"); Claude's agentId is a
        // hash and never a name.
        name: spawned?.name ?? (ev.name === "task" ? spawned?.id ?? null : null),
        agent: spawned?.agent ?? null,
        state,
        startedAt: ev.ts,
        durationMs: state === "working" ? null : durationMs,
      });
    });
  }
  return out;
}

export interface HelperSummary {
  total: number;
  working: number;
  done: number;
  failed: number;
}

export function summarizeHelpers(helpers: readonly HelperView[]): HelperSummary {
  const s: HelperSummary = { total: helpers.length, working: 0, done: 0, failed: 0 };
  for (const h of helpers) s[h.state] += 1;
  return s;
}

/** Seconds a helper ran (finished) or has been running (working, up to
 *  `now`). `null` when a finished helper's end is unknown. */
export function helperElapsedSeconds(h: HelperView, now: Date = new Date()): number | null {
  if (h.state === "working") {
    const s = secondsBetween(h.startedAt, null, now);
    return s == null || s < 0 ? null : s;
  }
  return h.durationMs == null ? null : Math.round(h.durationMs / 1000);
}

/** The whole group: first start to the last end (or now while one works). */
export function groupElapsedSeconds(helpers: readonly HelperView[], now: Date = new Date()): number | null {
  if (helpers.length === 0) return null;
  const start = helpers.reduce((a, h) => (h.startedAt < a ? h.startedAt : a), helpers[0].startedAt);
  const startMs = Date.parse(start);
  if (Number.isNaN(startMs)) return null;
  let endMs = startMs;
  for (const h of helpers) {
    if (h.state === "working") return Math.max(0, Math.round((now.getTime() - startMs) / 1000));
    if (h.durationMs == null) return null;
    endMs = Math.max(endMs, Date.parse(h.startedAt) + h.durationMs);
  }
  return Math.round((endMs - startMs) / 1000);
}
