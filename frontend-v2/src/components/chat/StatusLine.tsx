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
import { useTranslations } from "next-intl";

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
  /** What the agent is doing RIGHT NOW, from a structured event (the tool
   *  call whose result has not arrived yet). Replaces the rotating verb with
   *  the tool's own title ("Read poll.sh", "$ git diff"). Only honoured while
   *  the probe says working — a stale activity never outranks the state. */
  activity?: { title: string } | null;
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
/** Die Liste steht im Katalog (`sessions.status.workingWords`, EN/DE), mit `|`
 *  getrennt — next-intl liefert keine Arrays. */
function useWorkingWords(): readonly string[] {
  const t = useTranslations("sessions.status");
  return t("workingWords").split("|");
}

/** Wie lange ein Wort stehen bleibt. Kurz genug, dass es lebendig wirkt, lang
 *  genug, dass man es zu Ende lesen kann, bevor es wechselt. */
export const WORKING_WORD_INTERVAL_MS = 4000;

/** Liefert das aktuelle Arbeits-Verb und rotiert es, solange gearbeitet wird.
 *  Der Startpunkt wird bei jedem NEUEN Arbeitsabschnitt neu gewuerfelt, damit
 *  nicht jeder Zug mit demselben Wort beginnt; steht der Agent still, laeuft
 *  kein Timer (kein Rendern im Ruhezustand). */
function useWorkingWord(active: boolean): string {
  const WORKING_WORDS = useWorkingWords();
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

type Labels = (key: string) => string;

const unknownDisplay = (t: Labels): StatusDisplay => ({
  dotColor: C.warning,
  textColor: STATUS_TEXT.warning,
  label: t("unknown"),
  pulse: false,
});

// A finished session is a normal end state, not a fault: neutral tones, no
// pulse, and it says what happens next instead of leaving the operator to
// wonder whether typing is even possible. Amber stays reserved for the case
// that genuinely needs attention — the session is live but we cannot read it.
const endedDisplay = (t: Labels): StatusDisplay => ({
  dotColor: C.textDim,
  textColor: C.textMuted,
  label: t("ended"),
  pulse: false,
});

// Local, and scoped to exactly what we know: the request left the browser.
// It deliberately does NOT claim the agent got it or started working — that
// only becomes true when a real state/tool/message frame arrives, which is
// what clears this.
const sendingDisplay = (t: Labels): StatusDisplay => ({
  dotColor: STATUS.busy,
  textColor: STATUS_TEXT.info,
  label: t("sent"),
  pulse: true,
});

function resolveDisplay(
  state: StateEvent | null,
  connected: boolean,
  aliveness: ChatAliveness,
  sending: boolean,
  workingWord: string,
  activity: { title: string } | null,
  t: Labels,
): StatusDisplay {
  // Outranks the pane probe on purpose: right after a send the probe still
  // reports the PREVIOUS state (idle), and showing "Bereit" one frame after the
  // operator hit send is exactly the unresponsive feeling this round is about.
  if (sending) {
    return sendingDisplay(t);
  }
  if (aliveness === "ended") {
    return endedDisplay(t);
  }
  if (!connected || !state || state.status === "unknown") {
    return unknownDisplay(t);
  }

  switch (state.status) {
    case "working":
      // Ein laufendes Werkzeug ist konkreter als jedes Verb: es stammt aus
      // dem Transkript, nicht aus geratenem Bildschirmtext (Befund 10.09.2026).
      if (activity) {
        return { dotColor: STATUS.busy, textColor: STATUS_TEXT.info, label: activity.title, pulse: true };
      }
      return { dotColor: STATUS.busy, textColor: STATUS_TEXT.info, label: `${workingWord}…`, pulse: true };
    case "waiting_input":
      return { dotColor: STATUS.busy, textColor: STATUS_TEXT.info, label: t("waitingForYou"), pulse: false };
    case "permission_prompt":
      return { dotColor: C.warning, textColor: STATUS_TEXT.warning, label: t("waitingApproval"), pulse: false };
    case "idle":
      return { dotColor: C.textDim, textColor: C.textMuted, label: t("ready"), pulse: false };
  }
}

/**
 * Das Arbeits-Wort buchstabenweise (Livefeed-Look, Operator-Wunsch
 * 04.09.2026): jeder Buchstabe ist ein eigenes <span>, damit ein Lichtlauf
 * durch die Buchstaben ziehen und ein neues Wort buchstabenweise
 * hereinkommen kann (CSS `.status-letter`, globals.css). Die Ellipse bleibt
 * ein ruhiges Zeichen am Ende. Der `key` am Wort sorgt dafuer, dass beim
 * Wechsel neue Buchstaben eingehaengt werden — ihre Eintritts-Animation ist
 * der „Swap". Nur der Arbeits-Zustand wird so gesetzt; jede andere Zeile
 * bleibt schlichter Text.
 */
function LetterWord({ word }: { word: string }) {
  return (
    <span key={word} className="status-word" aria-label={`${word}…`}>
      {Array.from(word).map((ch, i) => (
        <span
          key={i}
          data-letter=""
          aria-hidden="true"
          className="status-letter"
          style={{ animationDelay: `${i * 60}ms, ${i * 40}ms, ${i * 35}ms` }}
        >
          {ch}
        </span>
      ))}
      <span aria-hidden="true">…</span>
    </span>
  );
}

export function StatusLine({
  state,
  connected,
  aliveness = "active",
  sending = false,
  activity = null,
}: StatusLineProps) {
  const t = useTranslations("sessions.status");
  // Der Hook muss VOR jedem fruehen Return laufen (Regeln der Hooks); er ist nur
  // aktiv, wenn wirklich gearbeitet wird, und laesst sonst keinen Timer laufen.
  const workingWord = useWorkingWord(connected && state?.status === "working" && !sending);
  const display = resolveDisplay(state, connected, aliveness, sending, workingWord, activity, t);
  const working = !sending && aliveness !== "ended" && connected && state?.status === "working";
  // Buchstaben-Lauf nur fuer das Verb; ein Werkzeug-Titel steht ruhig da
  // (Monospace, eine Zeile, abgeschnitten — nichts springt).
  const showVerb = working && !activity;

  return (
    // Left edge lines up with the message column (px-4 md:px-5), so the status
    // reads as the last line of the conversation rather than composer chrome.
    <div
      className="flex items-center gap-2 px-4 md:px-5 pb-1.5 text-[12px]"
      style={{ color: display.textColor }}
      aria-live="polite"
    >
      {/* Leucht-Punkt mit auslaufendem Ring, solange etwas passiert (CSS
          `.status-dot[data-live]`); still und ohne Schein in jedem Ruhe- oder
          Warnzustand. Farbe bleibt die des Zustands. */}
      <span
        data-testid="status-dot"
        data-live={display.pulse}
        className="status-dot relative inline-flex h-1.5 w-1.5 shrink-0 rounded-full"
        style={{ backgroundColor: display.dotColor, ["--status-dot" as string]: display.dotColor }}
      />
      <span
        data-testid="status-label"
        className={activity && working ? "font-mono truncate" : undefined}
        title={activity && working ? activity.title : undefined}
      >
        {showVerb ? <LetterWord word={workingWord} /> : display.label}
      </span>
    </div>
  );
}
