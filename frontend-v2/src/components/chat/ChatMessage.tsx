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
        p: ({ children }) => <p className="mb-3 leading-relaxed" style={{ color: "var(--color-text-body)" }}>{children}</p>,
        ul: ({ children }) => <ul className="mb-3 pl-4 space-y-1" style={{ color: "var(--color-text-body)" }}>{children}</ul>,
        ol: ({ children }) => <ol className="mb-3 pl-4 space-y-1 list-decimal" style={{ color: "var(--color-text-body)" }}>{children}</ol>,
        li: ({ children }) => <li className="text-sm leading-relaxed list-disc">{children}</li>,
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

export function ChatMessage({ ev }: { ev: MessageEvent }) {
  const isUser = ev.role === "user";

  return (
    <div
      className="w-full px-4 py-3"
      style={{ background: isUser ? C.bgSurface : "transparent" }}
    >
      <div className="flex items-baseline gap-2 mb-1.5">
        {isUser ? (
          <span
            className="text-[10px] font-mono font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded"
            style={{ background: C.accentSubtle, color: C.accent }}
          >
            Du
          </span>
        ) : (
          <span
            className="inline-block w-2 h-2 rounded-full shrink-0"
            style={{ background: C.accent }}
            aria-hidden="true"
          />
        )}
        {ev.model && (
          <span className="text-[10px] font-mono" style={{ color: C.textDim }}>
            {ev.model}
          </span>
        )}
      </div>
      <MarkdownContent content={ev.text} />
    </div>
  );
}
