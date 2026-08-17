import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ChatMessage } from "./ChatMessage";
import type { MessageEvent } from "@/lib/chatTypes";

function mkEvent(overrides: Partial<MessageEvent> = {}): MessageEvent {
  return {
    kind: "message",
    uuid: "u1",
    ts: "2026-08-15T10:00:00Z",
    role: "assistant",
    text: "Hello there",
    model: "claude-sonnet-5",
    sidechain: false,
    ...overrides,
  };
}

describe("ChatMessage", () => {
  it("renders a user row with the 'Du' label", () => {
    render(<ChatMessage ev={mkEvent({ role: "user", text: "Mach mal X" })} />);
    expect(screen.getByText("Du")).toBeInTheDocument();
    expect(screen.getByText("Mach mal X")).toBeInTheDocument();
  });

  it("renders an assistant row without the 'Du' label", () => {
    render(<ChatMessage ev={mkEvent({ role: "assistant", text: "Erledigt." })} />);
    expect(screen.queryByText("Du")).not.toBeInTheDocument();
    expect(screen.getByText("Erledigt.")).toBeInTheDocument();
  });

  it("keeps the 'Du' attribution available to screen readers only", () => {
    // The right-aligned bubble carries the speaker visually; a visible label
    // would repeat it. Screen readers can't hear alignment, so the text stays.
    render(<ChatMessage ev={mkEvent({ role: "user", text: "Mach mal X" })} />);
    expect(screen.getByText("Du")).toHaveClass("sr-only");
  });

  it("hides the model name by default", () => {
    render(<ChatMessage ev={mkEvent({ model: "claude-sonnet-5" })} />);
    expect(screen.queryByText("claude-sonnet-5")).not.toBeInTheDocument();
  });

  it("shows the model name when the caller flags it as a change", () => {
    render(<ChatMessage ev={mkEvent({ model: "claude-sonnet-5" })} showModel />);
    expect(screen.getByText("claude-sonnet-5")).toBeInTheDocument();
  });

  it("shows no model line for a flagged message that carries no model", () => {
    render(<ChatMessage ev={mkEvent({ model: null, text: "Ohne Modell" })} showModel />);
    expect(screen.getByText("Ohne Modell")).toBeInTheDocument();
  });

  it("renders markdown content: bold as <strong>", () => {
    render(<ChatMessage ev={mkEvent({ text: "Das ist **wichtig**." })} />);
    const strong = screen.getByText("wichtig");
    expect(strong.tagName).toBe("STRONG");
  });

  it("renders fenced code blocks as a block-level <code> (mirrors MemoryPage styling)", () => {
    render(<ChatMessage ev={mkEvent({ text: "```js\nconst x = 1;\n```" })} />);
    const code = screen.getByText((_, el) => el?.tagName === "CODE" && !!el.textContent?.includes("const x = 1;"));
    expect(code.className).toContain("block");
  });
});
