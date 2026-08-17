"use client";

/**
 * StatusLine — one truthful line above the chat composer, driven by the
 * tailer's `state` event (A6 pane-state probe) plus the stream's own
 * connection health.
 *
 * Truthful-status principle: if we don't actually know the agent's state —
 * status "unknown" from the probe, or the SSE stream itself disconnected —
 * this never guesses a plausible-looking status. It says so and points at
 * the terminal, the one place that can't lie.
 */
import { C, STATUS, STATUS_TEXT } from "@/lib/colors";
import type { StateEvent } from "@/lib/chatTypes";

interface StatusLineProps {
  state: StateEvent | null;
  connected: boolean;
  /** Whether the underlying CLI session is still running. A session that has
   *  ENDED is not an unknown state — it is a known, ordinary one, and painting
   *  it amber told the operator something was wrong when nothing was. */
  sessionLive?: boolean;
  /** A send has gone out and the transcript hasn't shown any sign of the turn
   *  yet. Local knowledge, and honest about being exactly that: it says the
   *  message left, not that the agent received or started it. */
  sending?: boolean;
}

interface StatusDisplay {
  dotColor: string;
  textColor: string;
  label: string;
  pulse: boolean;
}

const UNKNOWN_DISPLAY: StatusDisplay = {
  dotColor: C.warning,
  textColor: STATUS_TEXT.warning,
  label: "Status unklar — Terminal prüfen",
  pulse: false,
};

// A finished session is a normal end state, not a fault: neutral tones, no
// pulse, and it says what happens next instead of leaving the operator to
// wonder whether typing is even possible. Amber stays reserved for the case
// that genuinely needs attention — the session is live but we cannot read it.
const ENDED_DISPLAY: StatusDisplay = {
  dotColor: C.textDim,
  textColor: C.textMuted,
  label: "Session beendet — neue Nachricht startet die nächste Session",
  pulse: false,
};

// Local, and scoped to exactly what we know: the request left the browser.
// It deliberately does NOT claim the agent got it or started working — that
// only becomes true when a real state/tool/message frame arrives, which is
// what clears this.
const SENDING_DISPLAY: StatusDisplay = {
  dotColor: STATUS.busy,
  textColor: STATUS_TEXT.info,
  label: "Gesendet…",
  pulse: true,
};

function resolveDisplay(
  state: StateEvent | null,
  connected: boolean,
  sessionLive: boolean,
  sending: boolean,
): StatusDisplay {
  // Outranks the pane probe on purpose: right after a send the probe still
  // reports the PREVIOUS state (idle), and showing "Bereit" one frame after the
  // operator hit send is exactly the unresponsive feeling this round is about.
  if (sending) {
    return SENDING_DISPLAY;
  }
  if (!sessionLive) {
    return ENDED_DISPLAY;
  }
  if (!connected || !state || state.status === "unknown") {
    return UNKNOWN_DISPLAY;
  }

  switch (state.status) {
    case "working":
      return { dotColor: STATUS.busy, textColor: STATUS_TEXT.info, label: "Arbeitet…", pulse: true };
    case "waiting_input":
      return { dotColor: STATUS.busy, textColor: STATUS_TEXT.info, label: "Wartet auf dich", pulse: false };
    case "permission_prompt":
      return { dotColor: C.warning, textColor: STATUS_TEXT.warning, label: "Wartet auf Genehmigung", pulse: false };
    case "idle":
      return { dotColor: C.textDim, textColor: C.textMuted, label: "Bereit", pulse: false };
  }
}

export function StatusLine({ state, connected, sessionLive = true, sending = false }: StatusLineProps) {
  const display = resolveDisplay(state, connected, sessionLive, sending);

  return (
    // Left edge lines up with the message column (px-4 md:px-5), so the status
    // reads as the last line of the conversation rather than composer chrome.
    <div
      className="flex items-center gap-2 px-4 md:px-5 pb-1.5 text-[12px]"
      style={{ color: display.textColor }}
      aria-live="polite"
    >
      <span className="relative inline-flex h-1.5 w-1.5 shrink-0">
        <span
          className="absolute inset-0 rounded-full"
          style={{ backgroundColor: display.dotColor }}
        />
        {display.pulse && (
          <span
            className="absolute inset-0 animate-ping rounded-full"
            style={{ backgroundColor: display.dotColor, opacity: 0.6 }}
          />
        )}
      </span>
      <span>{display.label}</span>
    </div>
  );
}
