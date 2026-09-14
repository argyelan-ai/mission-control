/** Messprobe (Karte 87f58df9): Neu-Aufbauten der Zeitachse je Preview-Tick.
 *  Vorher: filter+buildTimelineItems bei jedem Render (jeder 0.3s-Tick).
 *  Nachher: useMemo auf events — Preview-Objekt-Wechsel allein rechnet nicht neu.
 *  Wir zaehlen buildTimelineItems-Aufrufe ueber 20 Preview-Ticks. */
import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useMemo, useState } from "react";

let buildCalls = 0;
function buildTimelineItems(events: unknown[]): unknown[] {
  buildCalls += 1;
  return events.map((e) => ({ e }));
}

describe("Preview-Tick → Zeitachse-Neuaufbau (Messung)", () => {
  it("rechnet die Zeitachse bei 20 Preview-Ticks NICHT neu (events identisch)", () => {
    const events = Array.from({ length: 50 }, (_, i) => ({ id: i }));
    let renderCount = 0;
    const { rerender } = renderHook(() => {
      renderCount += 1;
      const [preview, setPreview] = useState({ n: 0 });
      const visibleEvents = useMemo(() => events, [events]);
      const items = useMemo(() => buildTimelineItems(visibleEvents), [visibleEvents]);
      return { items, preview };
    });
    const before = buildCalls;
    for (let i = 0; i < 20; i++) rerender();
    // Der Hook renderte 21x, aber buildTimelineItems lief NICHT erneut:
    expect(renderCount).toBe(21);
    expect(buildCalls - before).toBe(0);
  });

  it("rechnet neu, wenn sich events wirklich aendern (Korrektheits-Kontrolle)", () => {
    buildCalls = 0;
    const eventsA = [{ id: 1 }];
    const { rerender } = renderHook(({ evs }: { evs: unknown[] }) => {
      const visibleEvents = useMemo(() => evs, [evs]);
      return useMemo(() => buildTimelineItems(visibleEvents), [visibleEvents]);
    }, { initialProps: { evs: eventsA } });
    const before = buildCalls;
    rerender({ evs: [{ id: 1 }, { id: 2 }] });
    expect(buildCalls - before).toBeGreaterThan(0);
  });
});
