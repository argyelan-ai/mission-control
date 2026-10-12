/**
 * Recognises a chat message the live voice passed on (voice PR 2).
 *
 * The backend (`services/voice_live_tools.compose_message`) writes it as:
 *
 *   [per Stimme] <the voice's wording>
 *   Wortlaut: „<the operator's own words>“      (left out when identical)
 *   (Voice transcript: … )                       (note for the agent)
 *
 * The chat shows the wording with a small "by voice" label, keeps the
 * operator's own words visible, and hides the marker and the agent note.
 * The marker is protocol, not UI copy, so it is the same in both languages.
 */

export const VOICE_PREFIX = "[per Stimme]";
const VERBATIM = /^Wortlaut:\s*„([\s\S]*)“$/;
const AGENT_NOTE = /^\(Voice transcript:[\s\S]*\)$/;

export interface VoiceMessage {
  text: string;
  /** The operator's own words, when they differ from `text`. */
  verbatim: string | null;
}

/** `null` = not a voice message (plain typed text). Strict on purpose: the
 *  marker must open the message, so a message that merely mentions it is
 *  shown as typed. */
export function parseVoiceMessage(raw: string): VoiceMessage | null {
  const text = raw ?? "";
  if (!text.startsWith(`${VOICE_PREFIX} `)) return null;
  const lines = text.split("\n");
  const first = lines[0].slice(VOICE_PREFIX.length + 1).trim();
  let verbatim: string | null = null;
  const rest: string[] = [];
  for (const line of lines.slice(1)) {
    const v = VERBATIM.exec(line.trim());
    if (v && verbatim === null) {
      verbatim = v[1].trim();
      continue;
    }
    if (AGENT_NOTE.test(line.trim())) continue;
    rest.push(line);
  }
  const body = [first, ...rest].join("\n").trim();
  return { text: body, verbatim: verbatim || null };
}
