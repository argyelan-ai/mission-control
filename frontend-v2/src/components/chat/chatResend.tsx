"use client";

/**
 * chatResend — the "Send again" contract between ChatView and the error card.
 *
 * TDD stub: the interface only. The rule comes in the next commit.
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

export function resendableErrorUuid(_events: TimelineChatEvent[], _openEchoes: number): string | null {
  return null;
}
