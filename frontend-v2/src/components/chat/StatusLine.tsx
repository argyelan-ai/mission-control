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

function resolveDisplay(state: StateEvent | null, connected: boolean): StatusDisplay {
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

export function StatusLine({ state, connected }: StatusLineProps) {
  const display = resolveDisplay(state, connected);

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
