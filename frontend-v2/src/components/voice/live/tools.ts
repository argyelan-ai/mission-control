/**
 * Live voice — the voice's two tools, browser side (voice PR 2). Pure parts
 * only: reading a function call out of the Realtime events, collecting the
 * operator's verbatim words, and turning MC's answer (or a failure) into the
 * `function_call_output` the model reads. The tools RUN in MC
 * (`POST /api/v1/voice/live/tool`); the browser only relays.
 */

export const VOICE_TOOL_NAMES = ["send_to_agent", "get_agent_status"] as const;
export type VoiceToolName = (typeof VOICE_TOOL_NAMES)[number];

export interface FunctionCall {
  callId: string;
  name: VoiceToolName;
  args: Record<string, unknown>;
}

interface FunctionCallEvent {
  type: string;
  call_id?: string;
  name?: string;
  arguments?: string;
  item?: { type?: string; call_id?: string; name?: string; arguments?: string } | null;
}

function parseArgs(raw: string | undefined): Record<string, unknown> {
  try {
    const v = JSON.parse(raw || "{}");
    return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function isToolName(name: string | undefined): name is VoiceToolName {
  return (VOICE_TOOL_NAMES as readonly string[]).includes(name ?? "");
}

/** A finished function call, from either event OpenAI sends for it
 *  (`response.output_item.done` carries the whole item; the arguments event
 *  carries the name too). The caller dedupes on `callId`. Unknown tool
 *  names are `null`: the model only has these two. */
export function functionCallFromEvent(ev: FunctionCallEvent): FunctionCall | null {
  if (ev.type === "response.output_item.done" && ev.item?.type === "function_call") {
    const { call_id, name, arguments: args } = ev.item;
    if (!call_id || !isToolName(name)) return null;
    return { callId: call_id, name, args: parseArgs(args) };
  }
  if (ev.type === "response.function_call_arguments.done" && ev.call_id && isToolName(ev.name)) {
    return { callId: ev.call_id, name: ev.name, args: parseArgs(ev.arguments) };
  }
  return null;
}

/** The client events that hand a tool's answer back to the model. */
export function functionOutputEvent(callId: string, output: unknown): object {
  return {
    type: "conversation.item.create",
    item: { type: "function_call_output", call_id: callId, output: JSON.stringify(output) },
  };
}

/**
 * The operator's utterances, in order, as the speech-to-text delivers them.
 * `send_to_agent` attaches every finished utterance since the last delivered
 * send — the operator's own words, not the voice's paraphrase (incident
 * 12.09.2026). An utterance is "pending" from the moment the audio buffer is
 * committed until its transcription completes (or fails); a send waits for
 * pending ones, because transcription often lands after the function call.
 */
export class UtteranceLog {
  private order: string[] = [];
  private texts = new Map<string, string>();
  private done = new Set<string>();
  private consumed = new Set<string>();

  private note(id: string) {
    if (!this.texts.has(id)) {
      this.order.push(id);
      this.texts.set(id, "");
    }
  }

  committed(id: string) {
    this.note(id);
  }

  delta(id: string, text: string) {
    this.note(id);
    if (!this.done.has(id)) this.texts.set(id, (this.texts.get(id) ?? "") + text);
  }

  final(id: string, text: string) {
    this.note(id);
    this.texts.set(id, text.trim());
    this.done.add(id);
  }

  /** Transcription failed: nothing to wait for, nothing to attach. */
  failed(id: string) {
    this.final(id, "");
  }

  pending(): boolean {
    return this.order.some((id) => !this.consumed.has(id) && !this.done.has(id));
  }

  /** The words to attach: finished, not yet delivered, non-empty. */
  take(): { ids: string[]; text: string } {
    const ids = this.order.filter((id) => !this.consumed.has(id) && this.done.has(id) && this.texts.get(id));
    return { ids, text: ids.map((id) => this.texts.get(id)).join(" ").trim() };
  }

  consume(ids: string[]) {
    ids.forEach((id) => this.consumed.add(id));
  }
}

/** How long a send waits for a transcription that is still running. */
export const TRANSCRIPT_WAIT_MS = 4_000;

/** What the model hears when MC could not run the tool at all (HTTP error
 *  or no network). `request()` throws `Error("API <status>: <body>")`. */
export function toolFailureOutput(name: VoiceToolName, err: unknown): Record<string, unknown> {
  const msg = err instanceof Error ? err.message : String(err ?? "");
  const m = /^API (\d+): ([\s\S]*)$/.exec(msg);
  let reason = m ? "error" : "network";
  if (m) {
    try {
      reason = JSON.parse(m[2])?.detail?.reason ?? (m[1] === "404" ? "voice_off" : "error");
    } catch {
      /* non-JSON body */
    }
  }
  if (name === "get_agent_status") {
    return { state: "unknown", reason, note: "Could not read the status. Say so; do not guess." };
  }
  const say: Record<string, string> = {
    call_unknown: "NOT delivered: this call lost its link to Mission Control. Ask the operator to hang up and call again.",
    wrong_agent: "NOT delivered: this call belongs to another chat.",
    rate_limited: "NOT delivered: too many messages in a short time. Wait a moment, then try once more.",
    text_too_long: "NOT delivered: too long for voice. Ask the operator to type it in the chat.",
    agent_archived: "NOT delivered: this agent is archived.",
  };
  return {
    delivered: false,
    reason,
    say: say[reason] ?? "NOT delivered: Mission Control did not answer. Tell the operator to type it in the chat.",
  };
}
