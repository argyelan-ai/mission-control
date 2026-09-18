
/**
 * Composer ueber der Bildschirmtastatur (Defekt 2, Operator-Befund 15.09.2026).
 *
 * Die Shell-Hoehe allein reicht nicht: `globals.css` schrumpft sie unterhalb
 * von md um `--keyboard-inset`, aber der Scroll-Container zieht seinen Inhalt
 * nicht nach. In Chromium 390x844 mit 300px Tastatur blieb die Eingabezeile
 * 222px unterhalb der sichtbaren Kante stehen — shell-korrekt und trotzdem
 * unerreichbar. 430x932: 134px. Die Zahlen kommen aus der Browser-Messung
 * (/tmp/mc-proof/defect2), hier steht der Vertrag.
 *
 * jsdom rechnet kein Layout, `scrollIntoView` ist ein No-op. Geprueft wird
 * deshalb der Aufruf samt Vorbedingungen: nur bei offener Tastatur, nur wenn
 * das Feld den Fokus hat, und nur einmal je Oeffnen.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { ThreadPanel } from "../ThreadPanel";

const listMock = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    tasks: {
      thread: {
        list: (...args: unknown[]) => listMock(...args),
        post: vi.fn(),
        markRead: vi.fn().mockResolvedValue(undefined),
      },
    },
  },
}));

const mkResponse = () => ({
  task_id: "t1",
  recipient: { kind: "agent", id: "boss", display: "Boss", listening: true, reason: "assignee" },
  messages: [
    {
      seq: 1,
      id: "m1",
      direction: "agent_to_user",
      author: { kind: "agent", id: "boss", display: "Boss" },
      body: "Erster Befund.",
      body_format: "text",
      created_at: "2026-09-16T09:00:00Z",
    },
  ],
  has_more_before: false,
  latest_seq: 1,
  my_read_seq: 1,
});

/** Setzt `--keyboard-inset` wie `useKeyboardInset` und meldet den Style-Twist. */
function setInset(px: number) {
  const root = document.documentElement;
  if (px > 0) root.style.setProperty("--keyboard-inset", `${px}px`);
  else root.style.removeProperty("--keyboard-inset");
}

/** Wartet, bis die MutationObserver-Kette (Microtask) gelaufen ist. */
const flush = () => new Promise((r) => setTimeout(r, 0));

let scrollSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  vi.clearAllMocks();
  listMock.mockResolvedValue(mkResponse());
  setInset(0);
  scrollSpy = vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(() => {});
});

afterEach(() => {
  scrollSpy.mockRestore();
  setInset(0);
});

describe("ThreadPanel zieht den Composer aus dem Tastatur-Bereich", () => {
  it("scrollt das fokussierte Eingabefeld heran, sobald die Tastatur aufgeht", async () => {
    render(<ThreadPanel taskId="t1" />);
    const input = await screen.findByLabelText("Thread message");

    input.focus();
    expect(document.activeElement).toBe(input);
    // Tastatur noch zu: kein Scrollen.
    setInset(0);
    await flush();
    expect(scrollSpy).not.toHaveBeenCalled();

    // Tastatur auf — die Variable wird am <html> gesetzt, wie im Betrieb.
    setInset(300);
    await flush();
    expect(scrollSpy).toHaveBeenCalled();
    expect(scrollSpy.mock.instances.at(-1)).toBe(input);
    expect(scrollSpy.mock.calls.at(-1)?.[0]).toEqual({ block: "nearest" });
  });

  it("laesst das Feld in Ruhe, wenn es den Fokus nicht hat", async () => {
    render(<ThreadPanel taskId="t1" />);
    await screen.findByLabelText("Thread message");

    // Nur die Tastatur-Oeffnung, ohne Fokus im Feld (z. B. Fokus in der Suche).
    setInset(300);
    await flush();
    expect(scrollSpy).not.toHaveBeenCalled();
  });

  it("ignoriert URL-Bar-Wackler unterhalb der Tastatur-Schwelle", async () => {
    render(<ThreadPanel taskId="t1" />);
    const input = await screen.findByLabelText("Thread message");
    input.focus();

    setInset(40);
    await flush();
    expect(scrollSpy).not.toHaveBeenCalled();
  });
});
