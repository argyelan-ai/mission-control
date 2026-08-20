"use client";

import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { AlertTriangle, Clock, Users } from "lucide-react";
import { C, STATUS_TEXT } from "@/lib/colors";
import { splitAttachments } from "./attachments";
import { ChatAttachmentTile } from "./ChatAttachmentTile";
import type { MessageEvent } from "@/lib/chatTypes";
import type { EchoStatus } from "@/hooks/useChatStream";

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
/** Anhang-Zeilen aus dem Text holen: im Verlauf soll eine Kachel stehen, kein
 *  roher Pfad. Der Text bleibt die einzige Quelle — der Verlauf ist das
 *  Transkript der CLI, nicht unsere Datenbank (siehe attachments.ts). */
export function ChatMessage({
  ev,
  showModel = false,
  echoStatus,
}: {
  ev: MessageEvent;
  showModel?: boolean;
  /** Set only for a locally-echoed send that the transcript hasn't confirmed
   *  yet (see useChatStream's optimistic echo). Absent = a real transcript
   *  message, which needs no qualifier. */
  echoStatus?: EchoStatus;
}) {
  // Rueckmeldung eines Subagenten / einer anderen Sitzung. Claude Code legt
  // die als gewoehnlichen USER-Turn ab — ohne eigene Behandlung erschiene sie
  // als rechtsbuendige Blase, also als etwas, das der Operator selbst getippt
  // hat (Operator-Befund 19.08.2026: "ganz komische sachen"). Sie bekommt
  // darum eine eigene, ruhige Zeile: erkennbar fremd, ohne den Verlauf zu
  // dominieren.
  if (ev.role === "teammate") {
    return (
      <div className="w-full px-4 md:px-5 py-1.5">
        <div
          data-testid="teammate-row"
          className="flex items-start gap-2 px-3 py-2 rounded-lg text-[13px] leading-[1.5]"
          style={{ background: C.bgHover, border: `1px solid ${C.borderSubtle}` }}
        >
          <Users size={13} className="mt-0.5 shrink-0" style={{ color: C.textMuted }} aria-hidden="true" />
          <div className="min-w-0">
            {ev.teammate && (
              <span className="font-mono text-[11px] mr-2" style={{ color: C.textMuted }}>
                {ev.teammate}
              </span>
            )}
            {/* Der Text stand hier in einem blanken <span>, und globals.css
                hat keine globale Umbruch-Regel. In Chromium bei 390px
                nachgemessen: ein wirklich unbrechbares Wort (122 Zeichen ohne
                Satzzeichen) wird 947,9px breit und gibt der SEITE einen
                waagerechten Rollbalken; mehrzeilige Nutzlasten kollabieren zu
                einer Zeile. Das haeufigste JSON bricht Chromium an seinen
                Kommata zwar von selbst um — Rueckmeldungen sind aber
                beliebiger Text, und seit gebuendelte Bloecke einzeln
                ankommen, sind mehrzeilige Nutzlasten der Normalfall. */}
            <span
              data-testid="teammate-text"
              className="break-words whitespace-pre-wrap"
              style={{ color: C.textSecondary }}
            >
              {ev.text}
            </span>
          </div>
        </div>
      </div>
    );
  }

  // `splitAttachments` steht bewusst HIER und nicht oben: gelesen wird sein
  // Ergebnis nur in diesem Zweig. Oben lief es fuer jede Teamkollegen- und
  // Assistenten-Zeile mit — voller `split("\n")`, Regex je Zeile, `join` — und
  // wurde restlos weggeworfen. ChatMessage ist nicht memoisiert, das fiel also
  // bei jedem Stream-Tick fuer jede sichtbare Zeile an.
  if (ev.role === "user") {
    const parsed = splitAttachments(ev.text);
    const unconfirmed = echoStatus === "unconfirmed";
    // Queued and starting are WAITS, not problems: the CLI genuinely holds a
    // message sent mid-turn until the turn ends, and a booting agent will get it
    // shortly. They say what they are waiting for and stay out of the way.
    const waitingNote =
      echoStatus === "queued"
        ? "Eingereiht — wird nach dem laufenden Zug gesendet"
        : echoStatus === "starting"
          ? "Agent startet — wird zugestellt…"
          : null;
    return (
      <div className="w-full px-4 md:px-5 py-2 flex justify-end">
        <div
          data-testid={echoStatus ? "echo-bubble" : undefined}
          data-echo-status={echoStatus}
          className="max-w-[85%] min-w-0 px-3.5 py-2.5 text-[14px] leading-[1.6] transition-opacity"
          style={{
            background: C.bgElevated,
            border: `1px solid ${unconfirmed ? `${C.warning}55` : C.border}`,
            borderRadius: "var(--radius-xl)",
            // Pending is dimmed, not spinning: the message is there, it just
            // isn't acknowledged yet. Once confirmed the bubble is replaced by
            // the real transcript event and returns to full opacity.
            opacity: echoStatus === "pending" ? 0.55 : 1,
            // queued/starting keep full opacity: they carry an explanatory line
            // of their own, and dimming them too would read as "degraded".
          }}
        >
          <span className="sr-only">Du</span>
          {parsed.attachments.length > 0 && (
            <div className="flex flex-col gap-1.5 mb-1.5">
              {parsed.attachments.map((a) => (
                <ChatAttachmentTile key={a.path} att={a} />
              ))}
            </div>
          )}
          {parsed.text && <ClampedUserContent text={parsed.text} />}
          {unconfirmed && (
            // Truthful, not reassuring: after the timeout we genuinely do not
            // know whether the CLI received this, so it says so and names the
            // one place that can answer it.
            <div
              className="mt-1.5 pt-1.5 flex items-center gap-1.5 text-xs"
              style={{ color: STATUS_TEXT.warning, borderTop: `1px solid ${C.borderSubtle}` }}
            >
              <AlertTriangle size={12} aria-hidden="true" />
              <span>Nicht bestätigt — Terminal prüfen</span>
            </div>
          )}
          {waitingNote && (
            // Muted, with a clock rather than a warning glyph — nothing here
            // needs the operator to act.
            <div
              className="mt-1.5 pt-1.5 flex items-center gap-1.5 text-xs"
              style={{ color: C.textMuted, borderTop: `1px solid ${C.borderSubtle}` }}
            >
              <Clock size={12} aria-hidden="true" />
              <span>{waitingNote}</span>
            </div>
          )}
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
