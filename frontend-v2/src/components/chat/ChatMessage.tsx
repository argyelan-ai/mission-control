"use client";

import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { C } from "@/lib/colors";
import type { MessageEvent } from "@/lib/chatTypes";

// ── Markdown renderer — mirrors LegacyMemoryPage's `MarkdownContent` exactly
// (same component config incl. code-block styling), so knowledge docs and
// chat transcripts render identically.
//
// `compact` is the user-bubble register. Dispatch briefs are full documents
// with h1/h2 sections, and rendering them at document scale inside a chat
// bubble turned the operator's own message into the loudest thing on screen —
// louder than the agent's answer, which is what the reader came for. Compact
// flattens every heading to one weighted body step and drops the display
// rhythm; the content is unchanged, only its volume. ────────────────────────
function MarkdownContent({ content, compact = false }: { content: string; compact?: boolean }) {
  const headingClass = compact ? "text-[14px] font-semibold mb-1 mt-2.5" : null;
  return (
    <ReactMarkdown
      components={{
        h1: ({ children }) => <h1 className={headingClass ?? "text-lg font-bold mb-3 mt-4"} style={{ color: "var(--color-text-primary)" }}>{children}</h1>,
        h2: ({ children }) => <h2 className={headingClass ?? "text-base font-semibold mb-2 mt-4"} style={{ color: "var(--color-text-primary)" }}>{children}</h2>,
        h3: ({ children }) => <h3 className={headingClass ?? "text-sm font-semibold mb-1.5 mt-3"} style={{ color: "var(--color-text-primary)" }}>{children}</h3>,
        // Leading is set here rather than inherited: `leading-relaxed` (1.625)
        // used to win over the container's value, so the reading measure and
        // the line spacing disagreed.
        p: ({ children }) => (
          <p
            className={compact ? "mb-2 leading-[1.6]" : "mb-3 leading-[1.7]"}
            style={{ color: compact ? "var(--color-text-secondary)" : "var(--color-text-body)" }}
          >
            {children}
          </p>
        ),
        ul: ({ children }) => (
          <ul
            className={compact ? "mb-2 pl-4 space-y-1" : "mb-3 pl-4 space-y-1.5"}
            style={{ color: compact ? "var(--color-text-secondary)" : "var(--color-text-body)" }}
          >
            {children}
          </ul>
        ),
        ol: ({ children }) => (
          <ol
            className={compact ? "mb-2 pl-4 space-y-1 list-decimal" : "mb-3 pl-4 space-y-1.5 list-decimal"}
            style={{ color: compact ? "var(--color-text-secondary)" : "var(--color-text-body)" }}
          >
            {children}
          </ol>
        ),
        li: ({ children }) => (
          <li className={compact ? "text-[14px] leading-[1.6] list-disc" : "text-[14px] leading-[1.7] list-disc"}>
            {children}
          </li>
        ),
        code: ({ children, className }) => {
          const isBlock = className?.includes("language-");
          return isBlock ? (
            <code className="block px-4 py-3 rounded-lg text-xs font-mono mb-3 overflow-x-auto"
              style={{ background: "var(--color-bg-elevated)", color: C.accent, border: "1px solid var(--color-border)" }}>
              {children}
            </code>
          ) : (
            // `overflow-wrap: anywhere` is load-bearing, not cosmetic: inline
            // code in a transcript is usually an unbreakable token (a path, a
            // flag, a container name). At 390px those ran past the right edge
            // and — since the page itself must never scroll sideways — got
            // clipped, so the end of the identifier was simply unreadable.
            <code className="px-1.5 py-0.5 rounded text-xs font-mono"
              style={{ background: C.accentSubtle, color: C.accent, overflowWrap: "anywhere" }}>
              {children}
            </code>
          );
        },
        blockquote: ({ children }) => (
          <blockquote className="pl-4 mb-3 text-sm italic" style={{ border: `1px solid ${C.borderAccent}`, borderRadius: 4, background: C.accentSubtle, paddingLeft: "0.75rem", color: "var(--color-text-secondary)" }}>
            {children}
          </blockquote>
        ),
        strong: ({ children }) => <strong className="font-semibold" style={{ color: "var(--color-text-primary)" }}>{children}</strong>,
        hr: () => <hr className={compact ? "my-2.5" : "my-4"} style={{ borderColor: "var(--color-border)" }} />,
        a: ({ href, children }) => (
          // Same reason as inline code: a bare URL is one long token.
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="underline"
            style={{ color: C.accent, overflowWrap: "anywhere" }}
          >
            {children}
          </a>
        ),
      }}
    >
      {content}
    </ReactMarkdown>
  );
}

// ── User-bubble clamp ───────────────────────────────────────────────────────
// A dispatch brief is thousands of characters. Left unclamped it becomes a wall
// the reader has to scroll past every single time to reach what the agent
// actually did. Ten lines is enough to recognise which brief this is; the rest
// is one tap away.
const USER_CLAMP_LINES = 10;
const USER_LINE_HEIGHT_PX = 23; // 14px × 1.6, rounded — matches the compact `p`
export const USER_CLAMP_MAX_PX = USER_CLAMP_LINES * USER_LINE_HEIGHT_PX;

/** Renders the user's markdown, clamped until the reader asks for the rest.
 *  The expander only appears when there is genuinely something hidden. */
function ClampedUserContent({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = contentRef.current;
    if (!el) return;
    const measure = () => {
      // Same trap as the composer's auto-grow: the mobile stack keeps the
      // off-screen pane mounted with `display: none`, where every metric reads
      // 0 and would report "nothing is hidden" for a 3000-character brief.
      if (el.scrollHeight === 0) return;
      setOverflows(el.scrollHeight > USER_CLAMP_MAX_PX + USER_LINE_HEIGHT_PX / 2);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [text]);

  const clamped = overflows && !expanded;

  return (
    <>
      <div
        ref={contentRef}
        data-testid="user-message-content"
        data-clamped={clamped}
        className="[&>*:last-child]:mb-0"
        style={clamped ? { maxHeight: USER_CLAMP_MAX_PX, overflow: "hidden" } : undefined}
      >
        <MarkdownContent content={text} compact />
      </div>
      {overflows && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="mt-1.5 pt-1.5 w-full text-left text-[12px] font-medium cursor-pointer transition-colors"
          style={{ color: C.textMuted, borderTop: `1px solid ${C.borderSubtle}` }}
        >
          {expanded ? "Weniger anzeigen" : "Mehr anzeigen"}
        </button>
      )}
    </>
  );
}

/**
 * One transcript message.
 *
 * The two roles get deliberately different shapes, the way the Claude app
 * does it: what the agent said is the page — full width, reading measure,
 * generous leading, no container. What the operator said is an aside — a
 * right-aligned bubble that reads as "this came from outside", capped at 85%
 * so the asymmetry itself carries the speaker. That removes the need for a
 * visible "Du" label; it stays as screen-reader text, since alignment is not
 * information a screen reader can hear.
 */
export function ChatMessage({ ev, showModel = false }: { ev: MessageEvent; showModel?: boolean }) {
  const isUser = ev.role === "user";

  if (isUser) {
    return (
      <div className="w-full px-4 md:px-5 py-2 flex justify-end">
        <div
          className="max-w-[85%] min-w-0 px-3.5 py-2.5 text-[14px] leading-[1.6]"
          style={{
            background: C.bgSurface,
            border: `1px solid ${C.border}`,
            borderRadius: "var(--radius-xl)",
          }}
        >
          <span className="sr-only">Du</span>
          <ClampedUserContent text={ev.text} />
        </div>
      </div>
    );
  }

  return (
    <div className="w-full px-4 md:px-5 py-3 md:py-4">
      {/* Only shown when the model CHANGED (see ChatView's modelBadgeUuids):
          stamping every assistant turn with the same name is noise, but a
          switch mid-session is exactly what this fleet's operator needs to
          see. */}
      {showModel && ev.model && (
        <div className="label-sys mb-2" style={{ color: C.textMuted }}>
          {ev.model}
        </div>
      )}
      {/* Reading measure, not island width: at full desktop width a line ran
          ~95 characters, well past where the eye loses its place. Capped in
          `ch` so it stays a measure and not a magic pixel number; below md the
          viewport is narrower than the cap anyway, so this is desktop-only in
          effect. */}
      <div className="text-[14px] leading-[1.7] max-w-[76ch] min-w-0 [&>*:last-child]:mb-0">
        <MarkdownContent content={ev.text} />
      </div>
    </div>
  );
}
