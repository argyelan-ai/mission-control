import { describe, it, expect, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ChatMessage, USER_CLAMP_MAX_PX } from "./ChatMessage";
import type { MessageEvent } from "@/lib/chatTypes";

/** jsdom reports 0 for every layout metric, so the clamp can never see an
 *  overflow on its own. Standing in for scrollHeight is the only way to test
 *  the branch that matters — and mirrors what a long dispatch brief does. */
function stubScrollHeight(px: number) {
  const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollHeight");
  Object.defineProperty(HTMLElement.prototype, "scrollHeight", { configurable: true, get: () => px });
  return () => {
    if (original) Object.defineProperty(HTMLElement.prototype, "scrollHeight", original);
    else delete (HTMLElement.prototype as unknown as Record<string, unknown>).scrollHeight;
  };
}

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

  // ── User bubble: clamp + compact register ─────────────────────────────────
  describe("long user messages", () => {
    let restore: (() => void) | null = null;

    afterEach(() => {
      restore?.();
      restore = null;
    });

    function renderUser(text: string) {
      return render(<ChatMessage ev={mkEvent({ role: "user", text })} />);
    }

    it("leaves a short message unclamped and offers no expander", () => {
      restore = stubScrollHeight(60);
      renderUser("Kurzer Auftrag");
      expect(screen.getByTestId("user-message-content")).toHaveAttribute("data-clamped", "false");
      expect(screen.queryByRole("button", { name: "Mehr anzeigen" })).not.toBeInTheDocument();
    });

    it("clamps a long dispatch brief and offers an expander", () => {
      restore = stubScrollHeight(USER_CLAMP_MAX_PX * 4);
      renderUser("# Auftrag\n\nSehr langer Brief …");
      const content = screen.getByTestId("user-message-content");
      expect(content).toHaveAttribute("data-clamped", "true");
      expect(content.style.maxHeight).toBe(`${USER_CLAMP_MAX_PX}px`);
      expect(screen.getByRole("button", { name: "Mehr anzeigen" })).toBeInTheDocument();
    });

    it("expands and collapses again on the expander", async () => {
      restore = stubScrollHeight(USER_CLAMP_MAX_PX * 4);
      const user = userEvent.setup();
      renderUser("# Auftrag\n\nSehr langer Brief …");

      await user.click(screen.getByRole("button", { name: "Mehr anzeigen" }));
      expect(screen.getByTestId("user-message-content")).toHaveAttribute("data-clamped", "false");

      await user.click(screen.getByRole("button", { name: "Weniger anzeigen" }));
      expect(screen.getByTestId("user-message-content")).toHaveAttribute("data-clamped", "true");
    });

    it("reports nothing hidden while the element cannot be measured (hidden pane)", () => {
      restore = stubScrollHeight(0);
      renderUser("# Auftrag\n\nSehr langer Brief …");
      expect(screen.queryByRole("button", { name: "Mehr anzeigen" })).not.toBeInTheDocument();
    });

    it("flattens headings inside a user bubble to one body-weight step", () => {
      restore = stubScrollHeight(60);
      renderUser("# Grosse Überschrift");
      const heading = screen.getByText("Grosse Überschrift");
      expect(heading.className).toContain("text-[14px]");
      expect(heading.className).not.toContain("text-lg");
    });

    it("keeps the assistant's own headings at document scale", () => {
      render(<ChatMessage ev={mkEvent({ role: "assistant", text: "# Grosse Überschrift" })} />);
      expect(screen.getByText("Grosse Überschrift").className).toContain("text-lg");
    });
  });
});
