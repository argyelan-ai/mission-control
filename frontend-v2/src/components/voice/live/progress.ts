/**
 * Live voice — "listen along" (voice PR 3, bauplan B4). Pure parts only.
 *
 * While a call is open, the chat's own live stream (the same SSE the chat
 * renders: `state`, `tool`, `todo`, `message`) is watched here and turned
 * into a few MILESTONES: the agent started on what the voice passed on, the
 * todo step changed, a tool started/finished, an approval or a question is
 * pending, an error, the turn ended. Between milestones at most one "still
 * working" line per 15 s — and only if something changed, or once a minute
 * as a heartbeat.
 *
 * This module only decides WHEN and from which fields. The words the voice
 * reads come from MC (`POST /api/v1/voice/live/progress`, backend
 * `voice_progress.py`), which redacts them in one place. From here only the
 * tool's name and title, the todo step, the approval question, an error
 * code and the agent's own final reply leave the browser — never a tool's
 * output.
 *
 * Also here: `speakDecision` (may the voice talk about it now? never over
 * the operator) and `claimsSuccess` (the no-success guard: the voice's own
 * words claim "done" before the agent reported a final).
 */

import type { MessageEvent, StateEvent, TimelineChatEvent, TodoEvent, ToolEvent } from "@/lib/chatTypes";

export interface ProgressSnapshot {
  state: StateEvent | null;
  events: TimelineChatEvent[];
  todos: TodoEvent | null;
}

export type ProgressKind =
  | "snapshot"
  | "started"
  | "working"
  | "step"
  | "tool_started"
  | "tool_finished"
  | "approval"
  | "question"
  | "final"
  | "error"
  | "still_working"
  | "premature_success";

export interface ProgressStep {
  index: number;
  total: number;
  content: string;
  all_done?: boolean;
}

export interface ProgressTool {
  name: string;
  title: string;
  failed?: boolean;
}

/** The fields sent to MC for one line (backend `VoiceProgressBody`). */
export interface ProgressRequest {
  kind: ProgressKind;
  step?: ProgressStep;
  tool?: ProgressTool;
  text?: string;
  question?: string;
  error_code?: string;
  elapsed_s?: number;
}

/** always = say it (wait until the floor is free) · soft = say it only if
 *  the floor is free right now and nothing was said a moment ago ·
 *  periodic = only after a quiet stretch · never = context only. */
export type SpeakLevel = "always" | "soft" | "periodic" | "never";

export interface Milestone {
  req: ProgressRequest;
  speak: SpeakLevel;
}

/** The turn counts as over when `idle` held this long (a TUI pane can flicker). */
export const FINAL_SETTLE_MS = 2_500;
/** omp writes its reply only at turn end: wait this long for it, then report
 *  the end without text rather than never. */
export const FINAL_WAIT_MS = 8_000;
/** At most one periodic line per window, and only when something changed. */
export const PERIODIC_MS = 15_000;
/** ... or once per heartbeat when nothing changed (Grok table: ~60 s). */
export const HEARTBEAT_MS = 60_000;
/** Tool milestones closer together than this are coalesced (newest wins). */
export const TOOL_MIN_GAP_MS = 4_000;
/** A soft milestone is not spoken this soon after the last spoken update. */
export const SOFT_GAP_MS = 20_000;
/** Periodic lines are spoken only after the operator was quiet this long. */
export const QUIET_MS = 20_000;
/** The final reply is shortened in MC; this only bounds the request. */
export const FINAL_TEXT_MAX = 4_000;
/** A closing reply line shorter than this ("Gespeichert.", "Fertig.") is an
 *  acknowledgement, not the answer: the final reads the line before it too. */
export const SHORT_REPLY_CHARS = 40;
const FIELD_MAX = 200;
/** Only the newest events are scanned; older ones are history. */
const TAIL = 60;

type Status = StateEvent["status"];
const BUSY: ReadonlySet<Status> = new Set<Status>(["working", "permission_prompt", "waiting_input"]);

const clip = (s: string | null | undefined, n = FIELD_MAX) => (s ?? "").slice(0, n);

function currentStep(todos: TodoEvent | null): { key: string; step: ProgressStep | null } {
  const items = todos?.items ?? [];
  if (items.length === 0) return { key: "", step: null };
  const idx = items.findIndex((i) => i.status === "in_progress");
  if (idx >= 0) {
    const content = clip(items[idx].content);
    return { key: `${idx}:${content}`, step: { index: idx + 1, total: items.length, content } };
  }
  if (items.every((i) => i.status === "completed")) {
    return { key: `done:${items.length}`, step: { index: items.length, total: items.length, content: "", all_done: true } };
  }
  return { key: "", step: null };
}

function toolKey(ev: ToolEvent): string {
  return ev.toolUseId ?? ev.uuid;
}

function toolFields(ev: ToolEvent, finished: boolean): ProgressTool {
  const out: ProgressTool = { name: clip(ev.name, 80), title: clip(ev.title) };
  if (finished && ev.status === "error") out.failed = true;
  return out;
}

export class ProgressTracker {
  private baselined = false;
  private status: Status = "unknown";
  private snap: ProgressSnapshot | null = null;
  private seenTools = new Map<string, boolean>();
  private seenMsgs = new Set<string>();
  private stepKey = "";
  /** A request was passed on and the agent has not started a turn since. */
  private awaitingStart = false;
  /** A request was passed on and no final came since. */
  private awaitingResult = false;
  private turnActive = false;
  private turnStartedAt: number | null = null;
  /** Assistant replies first seen during this turn / since the last send. */
  private replies: string[] = [];
  private pendingFinalSince: number | null = null;
  private lastContextAt = -Infinity;
  private lastPeriodicKey = "";
  private lastToolSentAt = -Infinity;
  private heldTool: Milestone | null = null;

  /** `send_to_agent` delivered a request. */
  forwarded(_midTurn: boolean, _now: number): void {
    this.awaitingStart = true;
    this.awaitingResult = true;
  }

  /** The no-success guard is armed while a result is owed. */
  guardArmed(): boolean {
    return this.awaitingResult || this.turnActive || BUSY.has(this.status);
  }

  private runningTool(): ToolEvent | null {
    const events = this.snap?.events ?? [];
    for (let i = events.length - 1; i >= Math.max(0, events.length - TAIL); i--) {
      const ev = events[i];
      if (ev.kind === "tool" && !ev.sidechain && ev.result == null) return ev;
    }
    return null;
  }

  private periodicKey(): string {
    const t = this.runningTool();
    return `${this.stepKey}|${t ? toolKey(t) : ""}`;
  }

  private emitted(out: Milestone[], now: number): Milestone[] {
    if (out.length > 0) {
      this.lastContextAt = now;
      this.lastPeriodicKey = this.periodicKey();
    }
    return out;
  }

  private startTurn(now: number) {
    this.turnActive = true;
    this.turnStartedAt = now;
  }

  private endTurn() {
    this.turnActive = false;
    this.turnStartedAt = null;
    this.awaitingResult = false;
    this.awaitingStart = false;
    this.pendingFinalSince = null;
    this.replies = [];
  }

  /** Feed the chat's current state. The first call is the baseline. */
  observe(snap: ProgressSnapshot, now: number): Milestone[] {
    this.snap = snap;
    const status: Status = snap.state?.status ?? "unknown";
    const tail = snap.events.slice(-TAIL);

    if (!this.baselined) {
      this.baselined = true;
      for (const ev of tail) {
        if (ev.kind === "tool") this.seenTools.set(toolKey(ev), ev.result != null);
        if (ev.kind === "message") this.seenMsgs.add(ev.uuid);
      }
      this.stepKey = currentStep(snap.todos).key;
      this.status = status;
      if (status === "working") this.startTurn(now);
      if (!BUSY.has(status)) return [];
      const req: ProgressRequest = { kind: "snapshot" };
      const step = currentStep(snap.todos).step;
      const running = this.runningTool();
      if (step) req.step = step;
      if (running) req.tool = toolFields(running, false);
      return this.emitted([{ req, speak: "never" }], now);
    }

    const out: Milestone[] = [];
    let toolMilestone: Milestone | null = null;

    for (const ev of tail) {
      if (ev.kind === "message") {
        if (this.seenMsgs.has(ev.uuid)) continue;
        this.seenMsgs.add(ev.uuid);
        const m = ev as MessageEvent;
        if (m.source?.kind === "error") {
          out.push({ req: { kind: "error", error_code: clip(m.error?.code, 40) || "unknown" }, speak: "always" });
        } else if (m.role === "assistant" && !m.sidechain && (this.turnActive || this.awaitingResult)) {
          this.replies.push(m.uuid);
          if (!BUSY.has(status) && this.pendingFinalSince === null) this.pendingFinalSince = now;
        }
      } else if (ev.kind === "tool" && !ev.sidechain) {
        const key = toolKey(ev);
        const finished = ev.result != null;
        const prev = this.seenTools.get(key);
        if (prev === undefined || (prev === false && finished)) {
          this.seenTools.set(key, finished);
          toolMilestone = {
            req: { kind: finished ? "tool_finished" : "tool_started", tool: toolFields(ev, finished) },
            speak: finished ? "never" : "soft",
          };
        }
      }
    }

    const { key: stepKey, step } = currentStep(snap.todos);
    if (stepKey !== this.stepKey) {
      this.stepKey = stepKey;
      if (step) out.push({ req: { kind: "step", step }, speak: "soft" });
    }

    if (status !== this.status) {
      this.status = status;
      if (status === "working") {
        this.pendingFinalSince = null;
        if (!this.turnActive) {
          this.startTurn(now);
          out.unshift({ req: { kind: this.awaitingStart ? "started" : "working" }, speak: this.awaitingStart ? "always" : "never" });
          this.awaitingStart = false;
        }
      } else if (status === "permission_prompt") {
        out.push({ req: { kind: "approval", question: clip(snap.state?.prompt?.question) }, speak: "always" });
      } else if (status === "waiting_input") {
        out.push({ req: { kind: "question", question: clip(snap.state?.prompt?.question) }, speak: "always" });
      } else if (this.turnActive || this.awaitingResult) {
        // idle / unknown: the turn may be over — settled by tick().
        if (this.turnActive || this.replies.length > 0) this.pendingFinalSince ??= now;
      }
    }

    if (toolMilestone) {
      if (now - this.lastToolSentAt >= TOOL_MIN_GAP_MS) {
        this.lastToolSentAt = now;
        this.heldTool = null;
        out.push(toolMilestone);
      } else {
        this.heldTool = toolMilestone;
      }
    }
    return this.emitted(out, now);
  }

  /** The turn's answer, read from its END. MC speaks only the first one or
   *  two sentences of this text (`voice_progress.summarize_reply`), so those
   *  must be the conclusion:
   *  - Claude Code writes one line per text block — "I'll look at the logs
   *    first." → tool → "The bug was X, fixed." The result is the last line;
   *    starting with the opening line would speak the plan, not the result.
   *  - A turn can also close with a short acknowledgement after its answer
   *    ("Saved.", and more often once the omp bridge writes one line per text
   *    segment, spec A PR-1). Speaking only that loses the answer.
   *  So: the last non-empty reply, plus earlier ones put in front only while
   *  the collected text is shorter than `SHORT_REPLY_CHARS`. Event order,
   *  joined with a blank line, capped from the start. */
  private lastReplyText(): string {
    const events = this.snap?.events ?? [];
    const wanted = new Set(this.replies);
    const texts: string[] = [];
    for (const ev of events) {
      if (ev.kind === "message" && wanted.has(ev.uuid) && ev.text.trim()) texts.push(ev.text);
    }
    const parts: string[] = [];
    let collected = 0;
    for (let i = texts.length - 1; i >= 0 && collected < SHORT_REPLY_CHARS; i--) {
      parts.unshift(texts[i]);
      collected += texts[i].trim().length;
    }
    return parts.join("\n\n").slice(0, FINAL_TEXT_MAX);
  }

  /** Call about once a second: settles the final, flushes a held tool
   *  milestone and produces the periodic line. */
  tick(now: number): Milestone[] {
    const out: Milestone[] = [];
    if (this.heldTool && now - this.lastToolSentAt >= TOOL_MIN_GAP_MS) {
      out.push(this.heldTool);
      this.heldTool = null;
      this.lastToolSentAt = now;
    }
    if (this.pendingFinalSince !== null && !BUSY.has(this.status)) {
      const waited = now - this.pendingFinalSince;
      if (waited >= FINAL_SETTLE_MS) {
        const text = this.lastReplyText();
        if (text || waited >= FINAL_WAIT_MS) {
          this.heldTool = null;
          out.push({ req: { kind: "final", text }, speak: "always" });
          this.endTurn();
        }
      }
    }
    if (this.status === "working" && this.turnActive && now - this.lastContextAt >= PERIODIC_MS) {
      const key = this.periodicKey();
      if (key !== this.lastPeriodicKey || now - this.lastContextAt >= HEARTBEAT_MS) {
        const req: ProgressRequest = { kind: "still_working" };
        const step = currentStep(this.snap?.todos ?? null).step;
        const running = this.runningTool();
        if (step) req.step = step;
        if (running) req.tool = toolFields(running, false);
        if (this.turnStartedAt !== null) req.elapsed_s = Math.round((now - this.turnStartedAt) / 1000);
        out.push({ req, speak: "periodic" });
      }
    }
    return this.emitted(out, now);
  }
}

// ── may the voice talk about it now? ─────────────────────────────────────

export interface VoiceFloor {
  /** The operator is talking (VAD speech_started … speech_stopped). */
  userSpeaking: boolean;
  /** The voice is generating or playing a response. */
  voiceBusy: boolean;
  lastUserSpeechAt: number;
  lastSpokenProgressAt: number;
}

/** now = ask for a response · later = once the floor is free · no = context only. */
export function speakDecision(level: SpeakLevel, floor: VoiceFloor, now: number): "now" | "later" | "no" {
  const busy = floor.userSpeaking || floor.voiceBusy;
  switch (level) {
    case "always":
      return busy ? "later" : "now";
    case "soft":
      return busy || now - floor.lastSpokenProgressAt < SOFT_GAP_MS ? "no" : "now";
    case "periodic":
      return busy || now - floor.lastUserSpeechAt < QUIET_MS || now - floor.lastSpokenProgressAt < HEARTBEAT_MS
        ? "no"
        : "now";
    default:
      return "no";
  }
}

// ── no-success guard (bauplan B4, layer b) ───────────────────────────────

const SUCCESS =
  /\b(erledigt|fertig|geschafft|abgeschlossen|erfolgreich|done|finished|complete|completed|succeeded|successful|successfully)\b/i;
// Negations and conditionals: "noch nicht fertig", "sobald er fertig ist",
// "not done yet", "when he's done" are honest, not claims.
const HEDGE =
  /\b(nicht|nichts|kein|keine|noch|sobald|wenn|falls|bis|not|no|never|yet|when|once|if|until|after|soon)\b|n't\b|n’t\b/i;

/** Does this spoken text claim the work is done? Checked clause by clause. */
export function claimsSuccess(text: string): boolean {
  return text
    .split(/[.!?;:,\n—–]+/)
    .some((clause) => SUCCESS.test(clause) && !HEDGE.test(clause));
}

/** A system item for the realtime conversation. On its own it makes the
 *  model say nothing; a `response.create` after it does. */
export function contextItemEvent(text: string): object {
  return {
    type: "conversation.item.create",
    item: { type: "message", role: "system", content: [{ type: "input_text", text }] },
  };
}
