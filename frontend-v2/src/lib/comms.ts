/**
 * Talking across harnesses — the phone view's data and pure helpers
 * (ADR-100 §4, stage 2d).
 *
 * Every word about presence ("alive", "listening") comes from the backend's
 * one presence source (`comms_directory`, §6 rule 2) through
 * `GET /api/v1/comms/directory`. Nothing here computes either word — it only
 * counts what the backend said.
 */
import type { ToolEvent } from "@/lib/chatTypes";

export type Alive = "yes" | "no" | "unknown";

/** One row of `GET /api/v1/comms/directory` (same rows as `mc who`). */
export interface DirectoryRow {
  address: string;
  kind: "agent" | "session";
  label: string;
  harness: string | null;
  machine: string | null;
  alive: Alive;
  /** null = unknown (a session that has not reported yet). */
  listening: boolean | null;
  busy: boolean;
  accept: "accept" | "hold" | "refuse";
  task: string | null;
  last_seen: string | null;
}

export interface Directory {
  sessions: DirectoryRow[];
  warming_up: boolean;
}

export interface Participant {
  ref: string;
  kind: string;
  address: string;
  label: string;
  harness: string | null;
}

export interface CommsMessage {
  id: string;
  seq: number;
  from: string | null;
  /** The sender's own text — the backend strips the receiver's frame. */
  text: string;
  created_at: string | null;
}

export type BrakeState = "running" | "paused" | "stopped";

export interface Conversation {
  thread_id: string;
  participants: Participant[];
  message_count: number;
  last_message: CommsMessage | null;
  last_message_at: string | null;
  state: BrakeState;
  brake_approval_id: string | null;
}

/** "Reachable" = alive AND listening — both words from the backend. */
export function isReachable(row: DirectoryRow): boolean {
  return row.alive === "yes" && row.listening === true;
}

export function reachableCount(dir: Directory | null | undefined): number {
  return (dir?.sessions ?? []).filter(isReachable).length;
}

/** "lead ↔ worker" — always in the backend's order, so it never flips. */
export function pairTitle(conv: Pick<Conversation, "participants">): string {
  return conv.participants.map((p) => p.address).join(" ↔ ");
}

// ── The send card in the chat ───────────────────────────────────────────────

export interface SendCall {
  to: string;
  /** null when the text came from stdin (`mc send x -`). */
  body: string | null;
}

/** What the tool's result said about the delivery (mc CLI / mc_send MCP). */
export type SendOutcome =
  | { kind: "pending" }
  | { kind: "failed"; reason: string }
  | { kind: "sent"; delivery: string | null; paused: boolean };

/** Split a shell command into words, honouring '…' and "…" quotes. Enough
 *  for the one command we recognise; anything stranger is left as a tool row. */
function shellWords(cmd: string): string[] | null {
  const out: string[] = [];
  let cur = "";
  let quote: '"' | "'" | null = null;
  let has = false;
  for (let i = 0; i < cmd.length; i++) {
    const ch = cmd[i];
    if (quote) {
      if (ch === quote) quote = null;
      else if (ch === "\\" && quote === '"' && i + 1 < cmd.length) cur += cmd[++i];
      else cur += ch;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      has = true;
    } else if (/\s/.test(ch)) {
      if (has) out.push(cur);
      cur = "";
      has = false;
    } else if (ch === "\\" && i + 1 < cmd.length) {
      cur += cmd[++i];
      has = true;
    } else {
      cur += ch;
      has = true;
    }
  }
  if (quote) return null;
  if (has) out.push(cur);
  return out;
}

const MC_SEND_TOOL = /(^|__)mc_send$/;
const COMMAND_TOOLS = new Set(["Bash", "bash"]);

/**
 * Is this tool call "send a message to a session"? Two spellings exist:
 * the MCP tool `mc_send` (any server prefix, input `{to, body}`) and the
 * command line `mc send <to> <text|->` run through a shell tool. A command
 * that does more than that (pipes, `&&`) is not claimed — the card would
 * hide the rest of it.
 */
export function parseSendCall(ev: ToolEvent): SendCall | null {
  const detail = (ev.detail ?? {}) as Record<string, unknown>;
  if (MC_SEND_TOOL.test(ev.name)) {
    const to = typeof detail.to === "string" ? detail.to.trim() : "";
    if (!to) return null;
    return { to, body: typeof detail.body === "string" ? detail.body : null };
  }
  if (!COMMAND_TOOLS.has(ev.name)) return null;
  const command = typeof detail.command === "string" ? detail.command.trim() : "";
  if (!/^(\S*\/)?mc\s+send\s/.test(command) || /[|;&<>`$]/.test(command.replace(/(['"])[\s\S]*?\1/g, ""))) {
    return null;
  }
  const words = shellWords(command);
  if (!words || words.length !== 4) return null;
  const [, , to, text] = words;
  if (!to || to.startsWith("-")) return null;
  return { to, body: text === "-" ? null : text };
}

/** Read the tool's own result line: "sent to X · next_rest[ · …paused…]". */
export function sendOutcome(ev: ToolEvent): SendOutcome {
  if (ev.result == null) return { kind: "pending" };
  const text = String(ev.result).trim();
  if (ev.status === "error" || /^(fehler|error)\b/i.test(text)) {
    return { kind: "failed", reason: text.replace(/^(fehler|error):?\s*/i, "") };
  }
  const m = /^(?:sent to|gesendet an)\s+\S+\s+·\s+([a-z_]+)/i.exec(text);
  return { kind: "sent", delivery: m ? m[1] : null, paused: /paused|pausiert/i.test(text) };
}

/** Delivery codes the send route returns (backend `comms_send.send`). */
export const DELIVERY_KEYS: Record<string, string> = {
  next_rest: "deliveryNextRest",
  queued_busy: "deliveryBusy",
  queued_not_reachable: "deliveryNotReachable",
  queued_unknown: "deliveryUnknown",
  held: "deliveryHeld",
};
