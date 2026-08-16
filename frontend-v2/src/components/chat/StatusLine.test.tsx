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
});
