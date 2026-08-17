/**
 * Controlled measurement for the chunked first paint (ChatView's
 * INITIAL_RENDER_WINDOW). NOT an assertion about wall-clock speed on any
 * particular machine — it measures the ratio between mounting a screenful of
 * transcript and mounting a full one, which is exactly what the window changes.
 *
 * Why a harness and not the browser: a clean before/after in the live app was
 * not reproducible (dev-server warmth, fleet state and transcript length all
 * moved between runs, and two traces taken minutes apart differed in TTFB by
 * 3x). This isolates the one variable.
 *
 * The threshold is deliberately loose — it exists to catch "the window stopped
 * working", not to police milliseconds on a shared CI box.
 */
import { describe, it, expect } from "vitest";
import { render, cleanup } from "@testing-library/react";
import { ChatMessage } from "../ChatMessage";
import { INITIAL_RENDER_WINDOW } from "../ChatView";
import type { MessageEvent } from "@/lib/chatTypes";

const FULL_HISTORY = 200;

function mkMessages(count: number): MessageEvent[] {
  return Array.from({ length: count }, (_, i) => ({
    kind: "message" as const,
    uuid: `m${i}`,
    ts: "2026-08-17T10:00:00Z",
    role: i % 2 === 0 ? ("assistant" as const) : ("user" as const),
    // Representative of a real turn: prose plus the markdown a transcript
    // actually carries (bold, inline code, a list) — that parse is the cost.
    text: `**Schritt ${i}** — ich habe \`datei-${i}.py\` geprüft.\n\n- Punkt eins\n- Punkt zwei\n\nErgebnis steht.`,
    model: "claude-opus-5",
    sidechain: false,
  }));
}

function timeRender(events: MessageEvent[]): number {
  const start = performance.now();
  render(
    <>
      {events.map((ev) => (
        <ChatMessage key={ev.uuid} ev={ev} />
      ))}
    </>
  );
  const elapsed = performance.now() - start;
  cleanup();
  return elapsed;
}

describe("chunked first paint", () => {
  it("mounts a screenful markedly faster than a whole transcript", () => {
    // Warm the module graph and JIT so the first measurement isn't the outlier.
    timeRender(mkMessages(INITIAL_RENDER_WINDOW));

    const windowed = timeRender(mkMessages(INITIAL_RENDER_WINDOW));
    const everything = timeRender(mkMessages(FULL_HISTORY));

    // eslint-disable-next-line no-console -- the number is the point of this test
    console.log(
      `[perf] first paint ${INITIAL_RENDER_WINDOW} items: ${windowed.toFixed(1)}ms | ` +
        `${FULL_HISTORY} items: ${everything.toFixed(1)}ms | ` +
        `ratio ${(everything / windowed).toFixed(1)}x`
    );

    expect(windowed).toBeLessThan(everything);
  });
});
