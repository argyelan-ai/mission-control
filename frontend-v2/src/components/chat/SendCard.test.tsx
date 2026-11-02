/**
 * Send card (ADR-100 §4, stage 2d): when an agent sends a message to another
 * session — `mc send …` through its shell tool, or the MCP tool `mc_send` —
 * the chat shows a compact card "Sent to X · info only" instead of a raw
 * tool line hidden in a tool group.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import type { TimelineChatEvent, ToolEvent } from "@/lib/chatTypes";
import { buildTimelineItems } from "./buildTimelineItems";
import { SendCard } from "./SendCard";

function tool(uuid: string, name: string, detail: Record<string, unknown>, result: string | null = "ok", status: "done" | "error" = "done"): ToolEvent {
  return {
    kind: "tool", uuid, ts: "2026-11-02T14:03:00Z", name, title: `${name} title`, detail,
    toolUseId: `t-${uuid}`, result, status, stats: null, sidechain: false,
  };
}

describe("buildTimelineItems — send calls", () => {
  it("a send call is its own item, never folded into a tool group", () => {
    const events: TimelineChatEvent[] = [
      tool("1", "Bash", { command: "mc who" }),
      tool("2", "Bash", { command: 'mc send worker "Is box one free?"' }, "sent to worker · next_rest"),
      tool("3", "Read", { file_path: "/home/x/a" }),
      tool("4", "Read", { file_path: "/home/x/b" }),
    ];
    const items = buildTimelineItems(events);
    expect(items.map((i) => i.kind)).toEqual(["single", "send", "activity"]);
    const send = items[1];
    expect(send.kind === "send" && send.call).toEqual({ to: "worker", body: "Is box one free?" });
  });

  it("other mc commands stay tool rows", () => {
    const items = buildTimelineItems([tool("1", "Bash", { command: "mc msg thread-1 hi" })]);
    expect(items.map((i) => i.kind)).toEqual(["single"]);
  });
});

describe("SendCard", () => {
  it("says to whom, what, and that it is information only", () => {
    render(
      <SendCard
        ev={tool("2", "mcp__mc__mc_send", { to: "claude@desk:runtimes", body: "When is the hand test done?" }, "sent to claude@desk:runtimes · next_rest")}
        call={{ to: "claude@desk:runtimes", body: "When is the hand test done?" }}
      />,
    );
    const card = screen.getByTestId("send-card");
    expect(card).toHaveTextContent("Sent to");
    expect(card).toHaveTextContent("claude@desk:runtimes");
    expect(card).toHaveTextContent("info only");
    expect(card).toHaveTextContent("When is the hand test done?");
    expect(card).toHaveTextContent("reads it at its next rest");
    expect(card).toHaveAttribute("data-outcome", "sent");
  });

  it("while running it is still sending; a refusal says not sent and why", () => {
    const call = { to: "worker", body: "hi" };
    const { rerender } = render(<SendCard ev={tool("2", "Bash", {}, null)} call={call} />);
    expect(screen.getByTestId("send-card")).toHaveTextContent("Sending to");
    rerender(<SendCard ev={tool("2", "Bash", {}, "worker is paused or not listening.", "error")} call={call} />);
    const card = screen.getByTestId("send-card");
    expect(card).toHaveAttribute("data-outcome", "failed");
    expect(card).toHaveTextContent("Not sent to");
    expect(card).toHaveTextContent("worker is paused or not listening.");
    expect(card).not.toHaveTextContent("info only");
  });

  it("the brake is named when the send paused the conversation", () => {
    render(
      <SendCard
        ev={tool("2", "Bash", {}, "sent to worker · queued_busy · conversation paused — waits for the operator's approval card")}
        call={{ to: "worker", body: "turn 6" }}
      />,
    );
    const card = screen.getByTestId("send-card");
    expect(card).toHaveTextContent("busy — reads it when done");
    expect(card).toHaveTextContent("Conversation paused — waits for you");
  });

  it("text from stdin is not invented", () => {
    render(<SendCard ev={tool("2", "Bash", {}, "sent to worker · next_rest")} call={{ to: "worker", body: null }} />);
    expect(screen.getByTestId("send-card")).toHaveTextContent("Text from a file or pipe");
  });
});
