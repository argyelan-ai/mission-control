"use client";

/**
 * MarkdownContent — der eine Markdown-Renderer der Chat-Oberflächen.
 *
 * Lag bis zum Gruppenchat (ADR-075) als lokale Funktion in ChatMessage.tsx.
 * Der Gruppenraum rendert dieselben Inhalte (Agenten-Beiträge, Lead-Synthese,
 * Ergebnis-Dokument) und soll dabei nicht anders aussehen als der 1:1-Chat —
 * eine zweite Kopie wäre genau die Drift, die man später nicht mehr findet.
 * Verhalten unverändert; ChatMessage importiert jetzt von hier.
 */
import ReactMarkdown from "react-markdown";
import { C } from "@/lib/colors";

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
export function MarkdownContent({ content, compact = false }: { content: string; compact?: boolean }) {
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
