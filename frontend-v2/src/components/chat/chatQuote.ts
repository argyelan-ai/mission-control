"use client";

/**
 * chatQuote — "quote this reply" between an assistant message and the composer.
 *
 * The message hands the text (the selection inside it, else the whole reply)
 * to `quote(text)`; ChatView passes it on to the Composer, which appends it to
 * the draft as a Markdown blockquote. Nothing is ever sent by this path.
 *
 * Outside ChatView (a head's transcript, a group card, a test without a
 * provider) the context is `null`, so no button appears where there is no
 * composer to quote into.
 */
import { createContext, useContext } from "react";

export const ChatQuoteContext = createContext<((text: string) => void) | null>(null);

export function useChatQuote(): ((text: string) => void) | null {
  return useContext(ChatQuoteContext);
}

/** `text` as a Markdown blockquote: every line gets `> ` (a blank line inside
 *  stays part of the quote as `>`). Blank lines at either end are dropped. */
export function toMarkdownQuote(text: string): string {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  while (lines.length > 0 && lines[0].trim() === "") lines.shift();
  while (lines.length > 0 && lines[lines.length - 1].trim() === "") lines.pop();
  return lines.map((line) => (line.trim() === "" ? ">" : `> ${line}`)).join("\n");
}

/** The draft with the quote appended: separated from existing text by one
 *  empty line, followed by an empty line so the reply starts below it. */
export function appendQuote(draft: string, text: string): string {
  const quote = toMarkdownQuote(text);
  if (!quote) return draft;
  const head = draft.replace(/\s+$/, "");
  return `${head ? `${head}\n\n` : ""}${quote}\n\n`;
}
