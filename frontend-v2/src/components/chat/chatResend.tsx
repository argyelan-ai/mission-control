"use client";

/**
 * chatResend — the "Send again" contract between ChatView and the error card.
 *
 * Chat over ACP (bauplan S1-Rest §9): when a restart cuts a reply off, or the
 * model does not answer, the chat daemon writes a `chat_error` card that
 * carries the operator's own text of that turn (`error.retryText`). The card
 * offers ONE tap that sends exactly that text again. Never by itself — the
 * operator decides.
 *
 * Two rules live here, so the card stays a plain view:
 *  - `resend(text)` is ChatView's own `handleSend` (echo + deliver). There is
 *    no second send path that could drift from the composer's.
 *  - `canResend(uuid)` is true for ONE card at most: the newest error card
 *    that carries a text, and only while nothing was sent after it (no
 *    operator line, no echo on its way). After a page reload an old card
 *    must not send a second time.
 *
 * Outside ChatView (a head's transcript, a test without a provider) the
 * default answers "no", so no button appears where nobody can send.
 */
import { createContext, useContext, useMemo, useState } from "react";

import type { TimelineChatEvent } from "@/lib/chatTypes";

export interface ChatResend {
  resend: (text: string) => void;
  canResend: (uuid: string) => boolean;
}

export const NO_RESEND: ChatResend = {
  resend: () => {},
  canResend: () => false,
};

export const ChatResendContext = createContext<ChatResend>(NO_RESEND);

export function useChatResend(): ChatResend {
  return useContext(ChatResendContext);
}

/** The operator's text a card can send again — `null` when there is none.
 *  The backend leaves the field out rather than shortening it (F3), so any
 *  string here is the whole message. */
export function retryTextOf(ev: TimelineChatEvent): string | null {
  if (ev.kind !== "message" || ev.role !== "teammate" || ev.source?.kind !== "error") return null;
  const text = ev.error?.retryText;
  return typeof text === "string" && text.trim() ? text : null;
}

/** `events[a]` happened after `events[b]`. By transcript time when both rows
 *  carry one and they differ; otherwise by position. Position alone is not
 *  enough: a reconnect re-seed appends the rows the live stream missed at
 *  the END of the list, so an older card can sit below a newer operator line
 *  (useChatStream `seedSequence`). Both times come from the same daemon. */
function happenedAfter(events: TimelineChatEvent[], a: number, b: number): boolean {
  const ta = Date.parse(events[a].ts);
  const tb = Date.parse(events[b].ts);
  if (Number.isFinite(ta) && Number.isFinite(tb) && ta !== tb) return ta > tb;
  return a > b;
}

/** The uuid of the one card the TRANSCRIPT allows to offer "Send again", or
 *  `null`: the newest error card with a text, as long as no operator line
 *  came after it (the conversation went on — the card is history). */
export function resendCandidateUuid(events: TimelineChatEvent[]): string | null {
  let card = -1;
  for (let i = 0; i < events.length; i++) {
    if (retryTextOf(events[i]) === null) continue;
    if (card < 0 || happenedAfter(events, i, card)) card = i;
  }
  if (card < 0) return null;
  for (let i = 0; i < events.length; i++) {
    const ev = events[i];
    if (ev.kind === "message" && ev.role === "user" && happenedAfter(events, i, card)) return null;
  }
  return events[card].uuid;
}

/**
 * The uuid of the one card that may offer "Send again" right now, or `null`.
 *
 * On top of the transcript rule: an echo (a local send not yet in the
 * transcript) blocks the offer only if it went out AFTER the card showed up
 * on this page — that is a send on its way, the card's own tap included.
 * Echoes that already existed when the card arrived were sent before it and
 * say nothing about it. That matters after a restart with a queued follow-up:
 * its job died with the old daemon (`queue_lost`), and its echo goes queued →
 * pending → unconfirmed and is never retired; counted, it would hide the
 * button until a reload. Echoes are page-local, so after a reload there are
 * none and only the transcript decides.
 */
export function useResendableErrorUuid(
  events: TimelineChatEvent[],
  echoes: readonly { id: string }[],
  enabled: boolean,
): string | null {
  const candidate = useMemo(() => resendCandidateUuid(events), [events]);
  // Which echoes existed when the current candidate first appeared. Kept per
  // card (React's "store information from previous renders" pattern), and
  // independent of `enabled`, so a brief "ended" blip cannot re-take the
  // snapshot with the card's own send in it.
  const [seen, setSeen] = useState<{ uuid: string | null; before: ReadonlySet<string> }>(() => ({
    uuid: null,
    before: new Set(),
  }));
  let before = seen.before;
  if (seen.uuid !== candidate) {
    before = new Set(echoes.map((e) => e.id));
    setSeen({ uuid: candidate, before });
  }
  if (!enabled || candidate === null) return null;
  return echoes.some((e) => !before.has(e.id)) ? null : candidate;
}
