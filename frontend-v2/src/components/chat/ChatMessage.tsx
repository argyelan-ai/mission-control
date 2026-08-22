"use client";

import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Clock } from "lucide-react";
import { C, STATUS_TEXT } from "@/lib/colors";
import { MarkdownContent } from "@/components/chat/MarkdownContent";
import type { MessageEvent } from "@/lib/chatTypes";
import type { EchoStatus } from "@/hooks/useChatStream";

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
  const isUser = ev.role === "user";

  if (isUser) {
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
          <ClampedUserContent text={ev.text} />
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
