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
import remarkGfm from "remark-gfm";
import { createContext, useContext } from "react";
import { C } from "@/lib/colors";

// ── Markdown renderer — grew out of LegacyMemoryPage's `MarkdownContent`
// (same look for headings, tables, code). One deliberate divergence: the chat
// fence carries its own horizontal scroller on the <pre> (below), because the
// chat is the surface where a wide block can push the whole transcript
// sideways on a phone; the memory page renders inside desktop scrollers.
//
// `compact` is the user-bubble register. Dispatch briefs are full documents
// with h1/h2 sections, and rendering them at document scale inside a chat
// bubble turned the operator's own message into the loudest thing on screen —
// louder than the agent's answer, which is what the reader came for. Compact
// flattens every heading to one weighted body step and drops the display
// rhythm; the content is unchanged, only its volume. ────────────────────────

// Ein gefencer Block (<pre><code>) ist CODE, egal ob er einen Language-Tag
// traegt — "```" ohne Tag ist im Agenten-Alltag der Normalfall. Ueber den
// Context bekommt das <code> das mit und rendert ohne Inline-Pill-Styling;
// das <pre> selbst ist der Block-Kasten und der horizontale Scroller.
const InCodeBlock = createContext(false);

export function MarkdownContent({ content, compact = false }: { content: string; compact?: boolean }) {
  const headingClass = compact ? "text-[14px] font-semibold mb-1 mt-2.5" : null;
  return (
    <ReactMarkdown
      // Ohne GFM kennt react-markdown weder Tabellen noch ~~durchgestrichen~~
      // noch Aufgabenlisten — die Pipe-Zeilen einer Tabelle landeten als
      // Fliesstext im Chat (Operator-Befund 01.09.2026). Agenten schreiben
      // ihre Ergebnisse haeufig als Tabelle; das ist keine Randnotiz.
      remarkPlugins={[remarkGfm]}
      components={{
        // Ueberschriften tragen ihre Ebene in der FARBE, nicht nur in der
        // Groesse — so wie Claude Code es im Terminal tut. Drei gleich
        // eingefaerbte Stufen lesen sich in einer langen Antwort als eine
        // einzige Ebene, und die Gliederung geht verloren (Marks Wunsch
        // 01.09.2026). Die Akzentfarbe fuehrt, danach zwei Textstufen.
        h1: ({ children }) => <h1 className={headingClass ?? "text-lg font-bold mb-3 mt-4"} style={{ color: C.accent }}>{children}</h1>,
        h2: ({ children }) => <h2 className={headingClass ?? "text-base font-semibold mb-2 mt-4"} style={{ color: "var(--color-text-primary)" }}>{children}</h2>,
        h3: ({ children }) => <h3 className={headingClass ?? "text-sm font-semibold mb-1.5 mt-3"} style={{ color: "var(--color-text-secondary)" }}>{children}</h3>,
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
        pre: ({ children }) => (
          // Der Scroller sitzt hier, nicht auf dem <code>: ein gefencer Block
          // ohne Language-Tag erreicht den Inline-Zweig nie als "isBlock" —
          // und ein <pre> ohne eigenen Scroller zieht mit `white-space: pre`
          // das ganze Transkript seitwaerts, obwohl seine Zeilen nur
          // Leerzeichen enthalten (Operator-Befund 19.09.2026, iPhone:
          // scrollWidth 568 vs clientWidth 390). overflow-wrap hilft in einem
          // <pre> nicht — kein Wrap-Point, kein Umbruch.
          <InCodeBlock.Provider value={true}>
            <pre
              className="block px-4 py-3 rounded-dense text-xs font-mono mb-3 overflow-x-auto"
              style={{ background: "var(--color-bg-elevated)", color: C.accent, border: "1px solid var(--color-border)" }}
            >
              {children}
            </pre>
          </InCodeBlock.Provider>
        ),
        code: ({ children, className }) => {
          return useContext(InCodeBlock) ? (
            <code className="block text-xs font-mono" style={{ color: C.accent }}>
              {children}
            </code>
          ) : (
            // `overflow-wrap: anywhere` is load-bearing, not cosmetic: inline
            // code in a transcript is usually an unbreakable token (a path, a
            // flag, a container name). At 390px those ran past the right edge
            // and — since the page itself must never scroll sideways — got
            // clipped, so the end of the identifier was simply unreadable.
            <code className="px-1.5 py-0.5 rounded-sm text-xs font-mono"
              style={{ background: C.accentSubtle, color: C.accent, overflowWrap: "anywhere" }}>
              {children}
            </code>
          );
        },
        // Tabellen scrollen in ihrem eigenen Kasten: eine breite Tabelle darf
        // die Seite nicht seitwaerts schieben (auf dem Handy sonst garantiert).
        table: ({ children }) => (
          <div className="mb-3 overflow-x-auto" style={{ border: `1px solid ${C.border}`, borderRadius: "var(--radius-md, 6px)" }}>
            <table className="w-full text-[13px]" style={{ borderCollapse: "collapse" }}>{children}</table>
          </div>
        ),
        thead: ({ children }) => (
          <thead style={{ background: "var(--color-bg-elevated)" }}>{children}</thead>
        ),
        th: ({ children }) => (
          <th className="px-3 py-2 text-left font-semibold" style={{ color: "var(--color-text-primary)", borderBottom: `1px solid ${C.border}` }}>{children}</th>
        ),
        td: ({ children }) => (
          <td className="px-3 py-2 align-top" style={{ color: "var(--color-text-body)", borderTop: `1px solid ${C.borderSubtle}` }}>{children}</td>
        ),
        del: ({ children }) => (
          <del style={{ color: "var(--color-text-secondary)" }}>{children}</del>
        ),
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
