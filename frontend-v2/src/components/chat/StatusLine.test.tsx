/**
 * StatusLine — Task B3 vitest.
 *
 * Coverage: one line above the composer, one copy string per StateEvent
 * status, plus the truthful-status fallback for "unknown" and for a
 * disconnected stream (never pretend a status we don't actually have).
 */
import React from "react";
import { describe, it, expect, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { StatusLine, WORKING_WORDS, WORKING_WORD_INTERVAL_MS } from "./StatusLine";
import type { StateEvent } from "@/lib/chatTypes";

const mkState = (status: StateEvent["status"]): StateEvent => ({
  kind: "state",
  status,
  prompt: null,
});

describe("StatusLine", () => {
  /** Der Arbeits-Text wechselt jetzt (Operator-Wunsch: wie in der Claude-Code-CLI).
   *  Getestet wird darum die Menge der erlaubten Woerter, nicht ein festes. */
  const workingText = () =>
    screen.getByText((t) => WORKING_WORDS.some((w) => t === `${w}…`)).textContent;

  it("shows one of the rotating working verbs for working", () => {
    render(<StatusLine state={mkState("working")} connected />);
    expect(WORKING_WORDS.map((w) => `${w}…`)).toContain(workingText());
  });

  it("rotates the verb while the agent keeps working", () => {
    vi.useFakeTimers();
    try {
      render(<StatusLine state={mkState("working")} connected />);
      const first = workingText();
      act(() => {
        vi.advanceTimersByTime(WORKING_WORD_INTERVAL_MS + 10);
      });
      const second = workingText();
      expect(second).not.toBe(first);
      expect(WORKING_WORDS.map((w) => `${w}…`)).toContain(second);
    } finally {
      vi.useRealTimers();
    }
  });

  it("runs no timer while the agent is idle", () => {
    vi.useFakeTimers();
    try {
      render(<StatusLine state={mkState("idle")} connected />);
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows "Wartet auf dich" for waiting_input', () => {
    render(<StatusLine state={mkState("waiting_input")} connected />);
    expect(screen.getByText("Wartet auf dich")).toBeInTheDocument();
  });

  it('shows "Wartet auf Genehmigung" for permission_prompt', () => {
    render(<StatusLine state={mkState("permission_prompt")} connected />);
    expect(screen.getByText("Wartet auf Genehmigung")).toBeInTheDocument();
  });

  it('shows "Bereit" for idle', () => {
    render(<StatusLine state={mkState("idle")} connected />);
    expect(screen.getByText("Bereit")).toBeInTheDocument();
  });

  it('shows the truthful fallback for status "unknown"', () => {
    render(<StatusLine state={mkState("unknown")} connected />);
    expect(screen.getByText("Status unklar — Terminal prüfen")).toBeInTheDocument();
  });

  it("shows the truthful fallback when disconnected, even with a stale non-unknown state", () => {
    render(<StatusLine state={mkState("working")} connected={false} />);
    expect(screen.getByText("Status unklar — Terminal prüfen")).toBeInTheDocument();
  });

  it("shows the truthful fallback when state is null", () => {
    render(<StatusLine state={null} connected />);
    expect(screen.getByText("Status unklar — Terminal prüfen")).toBeInTheDocument();
  });

  // ── Ended session: a known end state, not an unknown one ──────────────────

  it("reports an ended session plainly instead of as an unknown status", () => {
    render(<StatusLine state={null} connected aliveness="ended" />);
    expect(
      screen.getByText("Session beendet — neue Nachricht startet die nächste Session")
    ).toBeInTheDocument();
    expect(screen.queryByText("Status unklar — Terminal prüfen")).not.toBeInTheDocument();
  });

  it("does not paint an ended session in the warning tone", () => {
    const { container } = render(<StatusLine state={mkState("unknown")} connected aliveness="ended" />);
    const line = container.firstElementChild as HTMLElement;
    // C.textMuted, not STATUS_TEXT.warning — amber stays reserved for
    // "live but unreadable", the one case that needs the operator's attention.
    expect(line.style.color).toBe("rgb(143, 143, 143)");
  });

  it("never pulses on an ended session", () => {
    const { container } = render(<StatusLine state={mkState("working")} connected aliveness="ended" />);
    expect(container.querySelector(".animate-ping")).toBeNull();
  });

  it("still shows live statuses while the session is active", () => {
    render(<StatusLine state={mkState("working")} connected aliveness="active" />);
    expect(WORKING_WORDS.map((w) => `${w}…`)).toContain(workingText());
  });

  it('reads an IDLE session as "Bereit", never as ended', () => {
    // The complaint this replaces: a running CLI waiting at its prompt writes
    // nothing, so the mtime heuristic called it finished and the UI announced
    // "Session beendet" at a session sitting right there.
    render(<StatusLine state={mkState("idle")} connected aliveness="idle" />);
    expect(screen.getByText("Bereit")).toBeInTheDocument();
    expect(
      screen.queryByText("Session beendet — neue Nachricht startet die nächste Session")
    ).not.toBeInTheDocument();
  });

  it("still reports an unreadable IDLE session honestly", () => {
    // Idle is not a licence to invent a status: a dead stream is still unknown.
    render(<StatusLine state={null} connected={false} aliveness="idle" />);
    expect(screen.getByText("Status unklar — Terminal prüfen")).toBeInTheDocument();
  });
});

/* ──────────────────────────────────────────────────────────────────────────
 * Review 20.08.2026 — Befund 8: `useWorkingWord` schrieb im Render-Koerper
 * Refs (`seed.current = Math.random()…` und `wasActive.current = active`).
 * React verbietet das: die Render-Funktion muss rein sein. Direkt sichtbare
 * Folge — das Verb wird schon beim SERVER-Render gewuerfelt, der Client
 * wuerfelt beim Hydrieren ein anderes: Hydration-Mismatch. Unter Concurrent
 * Rendering kommt dazu, dass ein verworfener Render `wasActive.current = true`
 * stehen laesst und der naechste Zug dann NICHT neu wuerfelt — genau das, was
 * die Zufallsauswahl verhindern soll.
 * ────────────────────────────────────────────────────────────────────────── */
describe("StatusLine — Arbeits-Verb (Render-Reinheit)", () => {
  it("wuerfelt das Verb nicht waehrend des Renders", async () => {
    const { renderToStaticMarkup } = await import("react-dom/server");
    const spy = vi.spyOn(Math, "random");
    try {
      renderToStaticMarkup(<StatusLine state={mkState("working")} connected />);
      expect(spy).not.toHaveBeenCalled();
    } finally {
      spy.mockRestore();
    }
  });

  it("liefert zwei Server-Renders dasselbe Markup (kein Hydration-Mismatch)", async () => {
    const { renderToStaticMarkup } = await import("react-dom/server");
    const a = renderToStaticMarkup(<StatusLine state={mkState("working")} connected />);
    const b = renderToStaticMarkup(<StatusLine state={mkState("working")} connected />);
    expect(a).toBe(b);
  });

  it("zieht bei jedem neuen Arbeitsabschnitt ein neues Verb — auch unter StrictMode", () => {
    const { StrictMode } = React;
    const folge = [0, 0.5];
    let i = 0;
    const spy = vi.spyOn(Math, "random").mockImplementation(() => folge[i++ % folge.length]);
    try {
      const { rerender } = render(
        <StrictMode><StatusLine state={mkState("idle")} connected /></StrictMode>
      );
      rerender(<StrictMode><StatusLine state={mkState("working")} connected /></StrictMode>);
      const erstes = workingWordOf();
      rerender(<StrictMode><StatusLine state={mkState("idle")} connected /></StrictMode>);
      rerender(<StrictMode><StatusLine state={mkState("working")} connected /></StrictMode>);
      const zweites = workingWordOf();
      expect(erstes).toBe(`${WORKING_WORDS[0]}…`);
      expect(zweites).toBe(`${WORKING_WORDS[Math.floor(0.5 * WORKING_WORDS.length)]}…`);
      expect(zweites).not.toBe(erstes);
    } finally {
      spy.mockRestore();
    }
  });
});

function workingWordOf() {
  return screen.getByText((t) => WORKING_WORDS.some((w) => t === `${w}…`)).textContent;
}
