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
import { createContext, useContext } from "react";

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

/** The uuid of the one card that may offer "Send again", or `null`.
 *  Walks back from the newest event: an operator line on the way means the
 *  conversation went on after every card behind it. */
export function resendableErrorUuid(events: TimelineChatEvent[], openEchoes: number): string | null {
  if (openEchoes > 0) return null;
  for (let i = events.length - 1; i >= 0; i--) {
    const ev = events[i];
    if (ev.kind === "message" && ev.role === "user") return null;
    if (retryTextOf(ev) !== null) return ev.uuid;
  }
  return null;
}
