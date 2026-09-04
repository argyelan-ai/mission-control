/**
 * EventCard — eine Zeile „von aussen" (Auftrag, Hinweis, Nachricht,
 * Rueckmeldung, System) als zugeklappte Karte mit Motiv.
 *
 * Bis 04.09.2026 sah jede dieser Zeilen gleich aus: Personen-Icon, Dateiname,
 * bis zu zehn Zeilen Rohtext. Die Karte sagt zugeklappt, WAS es ist und
 * WORUM es geht, und zeigt den Text erst auf Wunsch.
 */
import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EventCard } from "./EventCard";
import type { MessageEvent, MessageSource } from "@/lib/chatTypes";

function mk(source: MessageSource, overrides: Partial<MessageEvent> = {}): { ev: MessageEvent; source: MessageSource } {
  return { ev: {
    kind: "message",
    uuid: "e1",
    ts: "2026-09-04T10:00:00Z",
    role: "teammate",
    teammate: "task-8a8d2f0e.md",
    source,
    text: "# Operating Card\nlots of rules\n# New Task: Beweis #414",
    model: null,
    sidechain: false,
    ...overrides,
  }, source };
}

describe("EventCard", () => {
  it("names the kind, the origin and the title while collapsed", () => {
    render(<EventCard {...mk({ kind: "task", title: "Beweis #414: Chat-Vorschau" })} live={false} />);
    const btn = screen.getByRole("button", { name: /Task/ });
    expect(btn).toHaveAttribute("aria-expanded", "false");
    expect(btn).toHaveTextContent("Mission Control");
    expect(btn).toHaveTextContent("Beweis #414: Chat-Vorschau");
    expect(screen.queryByTestId("teammate-text")).toBeNull();
    expect(screen.getByTestId("motif")).toHaveAttribute("data-kind", "woge");
  });

  it("falls back to the sender when there is no title", () => {
    render(<EventCard {...mk({ kind: "inbox", title: null }, { teammate: "00000003__a1b2.msg" })} live={false} />);
    expect(screen.getByRole("button")).toHaveTextContent("00000003__a1b2.msg");
    expect(screen.getByTestId("motif")).toHaveAttribute("data-kind", "strom");
  });

  it("credits a teammate reply to the teammate, not to Mission Control", () => {
    render(<EventCard {...mk({ kind: "teammate", title: null }, { teammate: "qwen-research" })} live={false} />);
    const btn = screen.getByRole("button");
    expect(btn).toHaveTextContent("qwen-research");
    expect(btn).not.toHaveTextContent("Mission Control");
    expect(screen.getByTestId("motif")).toHaveAttribute("data-kind", "iris");
  });

  it("reveals the full text on click and folds it again", async () => {
    render(<EventCard {...mk({ kind: "system", title: "async-result" })} live={false} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button"));
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("teammate-text")).toHaveTextContent("lots of rules");
    await user.click(screen.getByRole("button"));
    expect(screen.queryByTestId("teammate-text")).toBeNull();
  });

  it("passes liveness through to the motif", () => {
    render(<EventCard {...mk({ kind: "task", title: "x" })} live />);
    expect(screen.getByTestId("event-card")).toHaveAttribute("data-live", "true");
  });
});
