"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { AlertTriangle, Clock, Users } from "lucide-react";
import { C, STATUS_TEXT } from "@/lib/colors";
import { MarkdownContent } from "@/components/chat/MarkdownContent";
import { splitAttachments } from "./attachments";
import { ChatAttachmentTile } from "./ChatAttachmentTile";
import type { MessageEvent } from "@/lib/chatTypes";
import type { EchoStatus } from "@/hooks/useChatStream";
import type { Harness, HostHarness } from "@/lib/types";

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
  // Der Aufklapper stand bis 22.08.2026 fest auf Deutsch — in der
  // englischen Oberflaeche also mitten im Satz die falsche Sprache.
  const tChat = useTranslations("chat");
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
          {expanded ? tChat("showLess") : tChat("showMore")}
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
/**
 * What a send mid-turn actually does, per CLI. Claude Code QUEUES it until the
 * running turn ends. omp treats Enter as a STEERING message: it lands after
 * the running tool call and cuts the rest of that batch short — waiting for
 * the turn would be the wrong promise there.
 */
function queuedNote(harness: Harness | HostHarness | null | undefined): string {
  return harness === "omp"
    ? "Steuernachricht — greift nach dem laufenden Werkzeug"
    : "Eingereiht — wird nach dem laufenden Zug gesendet";
}

export function ChatMessage({
  ev,
  showModel = false,
  echoStatus,
  onWithdraw,
  onEdit,
  harness,
}: {
  ev: MessageEvent;
  showModel?: boolean;
  /** Set only for a locally-echoed send that the transcript hasn't confirmed
   *  yet (see useChatStream's optimistic echo). Absent = a real transcript
   *  message, which needs no qualifier. */
  echoStatus?: EchoStatus;
  /** Only meaningful while `echoStatus === "queued"`: the CLI still holds the
   *  message, so it can be taken back (withdraw) or taken back into the
   *  composer (edit). Absent = no buttons. */
  onWithdraw?: () => void;
  onEdit?: () => void;
  /** The CLI behind the agent. A mid-turn send means something different per
   *  harness (see `queuedNote`), so the waiting line names the right thing. */
  harness?: Harness | HostHarness | null;
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
        ? queuedNote(harness)
        : echoStatus === "starting"
          ? "Agent startet — wird zugestellt…"
          : null;
    // Only Claude Code lets us take a held message back (Up pops the queue
    // into the input line, Ctrl+U clears it). omp consumes a steer after the
    // running tool call; there is nothing to pull back, so no buttons.
    const canWithdraw = echoStatus === "queued" && harness !== "omp";
    if (echoStatus === "queued") {
      // Not a turn yet, so not a bubble: while the CLI holds the message it
      // sits as a small inset line under the running answer — the way Codex
      // and Claude Code stack queued input — and grows into a real bubble the
      // moment the transcript confirms it (operator wish 03.09.2026).
      return (
        <div className="w-full px-4 md:px-5 py-1 flex justify-end">
          <div
            data-testid="echo-bubble"
            data-echo-status={echoStatus}
            data-echo-compact="true"
            className="max-w-[70%] min-w-0 pl-3 pr-3 py-1.5 text-[13px] leading-[1.5] animate-fade-in"
            style={{
              color: C.textSecondary,
              borderRight: `2px solid ${C.borderSubtle}`,
              borderRadius: "var(--radius-sm)",
              background: `${C.bgElevated}80`,
            }}
          >
            <span className="sr-only">Du</span>
            {parsed.attachments.length > 0 && (
              <div className="flex flex-col gap-1.5 mb-1">
                {parsed.attachments.map((a) => (
                  <ChatAttachmentTile key={a.path} att={a} />
                ))}
              </div>
            )}
            {parsed.text && (
              <div className="whitespace-pre-wrap break-words line-clamp-2">{parsed.text}</div>
            )}
            <div
              className="mt-1 flex items-center gap-1.5 text-[11.5px]"
              style={{ color: C.textMuted }}
            >
              <Clock size={11} aria-hidden="true" />
              <span>{waitingNote}</span>
              {canWithdraw && (onWithdraw || onEdit) && (
                <span className="ml-auto pl-3 flex items-center gap-2.5">
                  {onEdit && (
                    <button type="button" onClick={onEdit} className="hover:underline" style={{ color: C.textSecondary }}>
                      Bearbeiten
                    </button>
                  )}
                  {onWithdraw && (
                    <button type="button" onClick={onWithdraw} className="hover:underline" style={{ color: C.textSecondary }}>
                      Zurückziehen
                    </button>
                  )}
                </span>
              )}
            </div>
          </div>
        </div>
      );
    }
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
            // Volle Deckkraft, auch waehrend "pending": Der Server hat die
            // Zustellung mit 204 quittiert, die Nachricht IST unterwegs. Bis
            // das Transkript sie zurueckspiegelt vergehen gemessen 1-3
            // Sekunden (die CLI schreibt den User-Turn erst rund eine Sekunde
            // spaeter, plus Poll) — genau so lange stand die eigene Zeile
            // blass da und der Chat fuehlte sich traege an (Operator-Befund
            // 01.09.2026). Das Dimmen sagte etwas Falsches ueber einen
            // Vorgang, der laengst geglueckt war. Die ehrliche Warnung bleibt:
            // ohne Bestaetigung wird die Blase nach zehn Sekunden markiert.
            opacity: 1,
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
