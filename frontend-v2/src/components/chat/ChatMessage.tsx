"use client";

import ReactMarkdown from "react-markdown";
import { C } from "@/lib/colors";
import type { MessageEvent } from "@/lib/chatTypes";

// ── Markdown renderer — mirrors LegacyMemoryPage's `MarkdownContent` exactly
// (same component config incl. code-block styling), so knowledge docs and
// chat transcripts render identically. ─────────────────────────────────────
function MarkdownContent({ content }: { content: string }) {
  return (
    <ReactMarkdown
      components={{
        h1: ({ children }) => <h1 className="text-lg font-bold mb-3 mt-4" style={{ color: "var(--color-text-primary)" }}>{children}</h1>,
        h2: ({ children }) => <h2 className="text-base font-semibold mb-2 mt-4" style={{ color: "var(--color-text-primary)" }}>{children}</h2>,
        h3: ({ children }) => <h3 className="text-sm font-semibold mb-1.5 mt-3" style={{ color: "var(--color-text-primary)" }}>{children}</h3>,
        // Leading is set here rather than inherited: `leading-relaxed` (1.625)
        // used to win over the container's value, so the reading measure and
        // the line spacing disagreed.
        p: ({ children }) => <p className="mb-3 leading-[1.7]" style={{ color: "var(--color-text-body)" }}>{children}</p>,
        ul: ({ children }) => <ul className="mb-3 pl-4 space-y-1.5" style={{ color: "var(--color-text-body)" }}>{children}</ul>,
        ol: ({ children }) => <ol className="mb-3 pl-4 space-y-1.5 list-decimal" style={{ color: "var(--color-text-body)" }}>{children}</ol>,
        li: ({ children }) => <li className="text-[14px] leading-[1.7] list-disc">{children}</li>,
        code: ({ children, className }) => {
          const isBlock = className?.includes("language-");
          return isBlock ? (
            <code className="block px-4 py-3 rounded-lg text-xs font-mono mb-3 overflow-x-auto"
              style={{ background: "var(--color-bg-elevated)", color: C.accent, border: "1px solid var(--color-border)" }}>
              {children}
            </code>
          ) : (
            <code className="px-1.5 py-0.5 rounded text-xs font-mono"
              style={{ background: C.accentSubtle, color: C.accent }}>
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
        hr: () => <hr className="my-4" style={{ borderColor: "var(--color-border)" }} />,
        a: ({ href, children }) => (
          <a href={href} target="_blank" rel="noopener noreferrer" className="underline" style={{ color: C.accent }}>
            {children}
          </a>
        ),
      }}
    >
      {content}
    </ReactMarkdown>
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
          className="max-w-[85%] px-3.5 py-2.5 text-[14px] leading-[1.65] [&>*:last-child]:mb-0"
          style={{
            background: C.bgSurface,
            border: `1px solid ${C.border}`,
            borderRadius: "var(--radius-xl)",
          }}
        >
          <span className="sr-only">Du</span>
          <MarkdownContent content={ev.text} />
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
        <div className="text-[10px] font-mono mb-2" style={{ color: C.textMuted }}>
          {ev.model}
        </div>
      )}
      {/* Reading measure, not island width: at full desktop width a line ran
          ~95 characters, well past where the eye loses its place. Capped in
          `ch` so it stays a measure and not a magic pixel number; below md the
          viewport is narrower than the cap anyway, so this is desktop-only in
          effect. */}
      <div className="text-[14px] leading-[1.7] max-w-[76ch] [&>*:last-child]:mb-0">
        <MarkdownContent content={ev.text} />
      </div>
    </div>
  );
}
