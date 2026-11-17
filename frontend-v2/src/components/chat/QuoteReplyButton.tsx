"use client";

import { useRef } from "react";
import { TextQuote } from "lucide-react";
import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import { useChatQuote } from "./chatQuote";

/** The selected text, when the selection lies wholly inside `scope`. */
function selectionInside(scope: Element | null): string | null {
  const sel = typeof window !== "undefined" ? window.getSelection() : null;
  if (!scope || !sel || sel.isCollapsed || sel.rangeCount === 0) return null;
  if (!sel.anchorNode || !sel.focusNode) return null;
  if (!scope.contains(sel.anchorNode) || !scope.contains(sel.focusNode)) return null;
  const text = sel.toString();
  return text.trim() ? text : null;
}

/**
 * "Quote in reply" under an assistant message: the selection inside the
 * message (`[data-quote-message]`), else the whole reply, goes into the
 * composer draft as a blockquote (chatQuote.ts). Always visible — a phone has
 * no hover. The selection is read on pointer-down: on touch the tap itself
 * can clear it before the click arrives.
 */
export function QuoteReplyButton({ text }: { text: string }) {
  const t = useTranslations("chat");
  const quote = useChatQuote();
  const pickedRef = useRef<string | null>(null);
  if (!quote) return null;

  const scopeOf = (el: HTMLElement) => el.closest("[data-quote-message]");

  return (
    <button
      type="button"
      data-testid="quote-reply"
      aria-label={t("quoteReply")}
      title={t("quoteReply")}
      onPointerDown={(e) => {
        pickedRef.current = selectionInside(scopeOf(e.currentTarget));
      }}
      onClick={(e) => {
        const picked = pickedRef.current ?? selectionInside(scopeOf(e.currentTarget));
        pickedRef.current = null;
        quote(picked ?? text);
      }}
      className="mt-1 -mb-2 inline-flex items-center min-h-11 min-w-11 rounded opacity-70 hover:opacity-100"
      style={{ color: C.textSecondary }}
    >
      <TextQuote size={14} aria-hidden="true" />
    </button>
  );
}
