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
import { useEffect, useState } from "react";

import { C, STATUS, STATUS_TEXT } from "@/lib/colors";
import type { ChatAliveness, StateEvent } from "@/lib/chatTypes";

interface StatusLineProps {
  state: StateEvent | null;
  connected: boolean;
  /** How alive the session is (see `resolveAliveness`). Only `ended` is a
   *  statement that nothing more can happen; `idle` is a running CLI waiting at
   *  its prompt, which reads as "Bereit" like any other quiet moment. The old
   *  boolean could not tell those apart and therefore announced a finished
   *  session at one that was merely quiet. */
  aliveness?: ChatAliveness;
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

/**
 * Wechselnde Verben fuer den Arbeits-Zustand (Operator-Wunsch 18.08.2026, nach
 * dem Vorbild der Claude-Code-CLI). Ein starres "Arbeitet…" ueber Minuten sieht
 * aus wie ein eingefrorenes UI; ein Wort, das sich alle paar Sekunden aendert,
 * zeigt Leben — ohne etwas zu behaupten, das wir nicht wissen. Deshalb sind alle
 * Begriffe bewusst inhaltsleer: sie beschreiben NICHT, was der Agent tut (das
 * wuesste nur er selbst), sondern nur DASS er laeuft. Der pulsierende Punkt
 * bleibt das eigentliche Signal.
 */
export const WORKING_WORDS = [
  "Arbeitet",
  "Denkt nach",
  "Gruebelt",
  "Bruetet",
  "Werkelt",
  "Tueftelt",
  "Rechnet",
  "Sinniert",
  "Knobelt",
  "Feilt",
  "Sortiert",
  "Kombiniert",
  "Verdichtet",
  "Spuert nach",
  "Waelzt Ideen",
  "Zieht Faeden",
] as const;

/** Wie lange ein Wort stehen bleibt. Kurz genug, dass es lebendig wirkt, lang
 *  genug, dass man es zu Ende lesen kann, bevor es wechselt. */
export const WORKING_WORD_INTERVAL_MS = 4000;

/** Liefert das aktuelle Arbeits-Verb und rotiert es, solange gearbeitet wird.
 *  Der Startpunkt wird bei jedem NEUEN Arbeitsabschnitt neu gewuerfelt, damit
 *  nicht jeder Zug mit demselben Wort beginnt; steht der Agent still, laeuft
 *  kein Timer (kein Rendern im Ruhezustand). */
function useWorkingWord(active: boolean): string {
  const [tick, setTick] = useState(0);
  const [seed, setSeed] = useState(0);

  // Gewuerfelt wird im Effekt, NICHT im Render (Review 20.08.2026). Vorher
  // standen `seed.current = Math.random()…` und `wasActive.current = active`
  // im Render-Koerper — beides verbietet React, und beides hatte eine echte
  // Folge: der Server-Render wuerfelte ein anderes Verb als der Client beim
  // Hydrieren (Hydration-Mismatch, im Test durch zwei ungleiche
  // renderToStaticMarkup-Ausgaben belegt), und unter Concurrent Rendering
  // liess ein verworfener Render `wasActive.current = true` stehen, womit der
  // naechste Zug NICHT neu wuerfelte — genau das, was die Zufallsauswahl
  // verhindern soll. Der Effekt haengt ohnehin an `active` und laeuft damit
  // exakt einmal pro Arbeitsabschnitt. `tick` faengt dabei wieder bei 0 an,
  // damit das erste Wort eines Zuges sein volles Intervall steht.
  useEffect(() => {
    if (!active) return;
    setSeed(Math.floor(Math.random() * WORKING_WORDS.length));
    setTick(0);
    const id = setInterval(() => setTick((t) => t + 1), WORKING_WORD_INTERVAL_MS);
    return () => clearInterval(id);
  }, [active]);

  return WORKING_WORDS[(seed + tick) % WORKING_WORDS.length];
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
  aliveness: ChatAliveness,
  sending: boolean,
  workingWord: string,
): StatusDisplay {
  // Outranks the pane probe on purpose: right after a send the probe still
  // reports the PREVIOUS state (idle), and showing "Bereit" one frame after the
  // operator hit send is exactly the unresponsive feeling this round is about.
  if (sending) {
    return SENDING_DISPLAY;
  }
  if (aliveness === "ended") {
    return ENDED_DISPLAY;
  }
  if (!connected || !state || state.status === "unknown") {
    return UNKNOWN_DISPLAY;
  }

  switch (state.status) {
    case "working":
      return { dotColor: STATUS.busy, textColor: STATUS_TEXT.info, label: `${workingWord}…`, pulse: true };
    case "waiting_input":
      return { dotColor: STATUS.busy, textColor: STATUS_TEXT.info, label: "Wartet auf dich", pulse: false };
    case "permission_prompt":
      return { dotColor: C.warning, textColor: STATUS_TEXT.warning, label: "Wartet auf Genehmigung", pulse: false };
    case "idle":
      return { dotColor: C.textDim, textColor: C.textMuted, label: "Bereit", pulse: false };
  }
}

export function StatusLine({
  state,
  connected,
  aliveness = "active",
  sending = false,
}: StatusLineProps) {
  // Der Hook muss VOR jedem fruehen Return laufen (Regeln der Hooks); er ist nur
  // aktiv, wenn wirklich gearbeitet wird, und laesst sonst keinen Timer laufen.
  const workingWord = useWorkingWord(connected && state?.status === "working" && !sending);
  const display = resolveDisplay(state, connected, aliveness, sending, workingWord);

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
