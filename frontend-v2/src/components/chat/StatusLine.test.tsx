/**
 * StatusLine — Task B3 vitest.
 *
 * Coverage: one line above the composer, one copy string per StateEvent
 * status, plus the truthful-status fallback for "unknown" and for a
 * disconnected stream (never pretend a status we don't actually have).
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { StatusLine } from "./StatusLine";
import type { StateEvent } from "@/lib/chatTypes";

const mkState = (status: StateEvent["status"]): StateEvent => ({
  kind: "state",
  status,
  prompt: null,
});

describe("StatusLine", () => {
  it('shows "Arbeitet…" for working', () => {
    render(<StatusLine state={mkState("working")} connected />);
    expect(screen.getByText("Arbeitet…")).toBeInTheDocument();
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
    expect(screen.getByText("Arbeitet…")).toBeInTheDocument();
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
