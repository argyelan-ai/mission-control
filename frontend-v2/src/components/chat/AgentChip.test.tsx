/**
 * AgentChip — the "New chat" draft's agent chip (concept P4): its sentence
 * says who answers, or honestly that the old chat opens; its menu has
 * "New chat", the dimmed "Only one chat" and "New group".
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AgentChip } from "./AgentChip";
import type { NewChatAgent, NewChatChoices } from "@/lib/chatList";

const sparky: NewChatAgent = { id: "s", name: "Sparky", status: "idle", agent_runtime: "cli-bridge", container_state: "running", can_start_chat: true };
const boss: NewChatAgent = { id: "b", name: "Boss", status: "idle", agent_runtime: "host" };

function renderChip(over: Partial<React.ComponentProps<typeof AgentChip>> = {}) {
  const props = {
    agent: sparky,
    choices: { canStart: [sparky], onlyOne: [boss] } as NewChatChoices,
    newChatsOn: true,
    onPick: vi.fn(),
    onOpenOwnChat: vi.fn(),
    onNewGroup: vi.fn(),
    ...over,
  };
  render(<AgentChip {...props} />);
  return props;
}

describe("AgentChip", () => {
  it("names who answers the new chat", () => {
    renderChip();
    const chip = screen.getByTestId("agent-chip-button");
    expect(chip).toHaveTextContent("With Sparky");
    expect(chip).toHaveAttribute("data-mode", "new");
  });

  it("new chats off: says so before anything is typed", () => {
    renderChip({ newChatsOn: false, choices: { canStart: [], onlyOne: [sparky, boss] } });
    const chip = screen.getByTestId("agent-chip-button");
    expect(chip).toHaveTextContent("New chats are off — your chat with Sparky opens.");
    expect(chip).toHaveAttribute("data-mode", "off");
  });

  it("an agent with only one chat (new chats on): says that chat opens", () => {
    renderChip({ agent: boss, choices: { canStart: [], onlyOne: [boss] } });
    expect(screen.getByTestId("agent-chip-button")).toHaveTextContent("Boss has only one chat — it opens.");
  });

  it("nobody alive: says so, the menu still offers a new group", async () => {
    const user = userEvent.setup();
    const props = renderChip({ agent: null, choices: { canStart: [], onlyOne: [] } });
    expect(screen.getByTestId("agent-chip-button")).toHaveTextContent("No agent is running right now.");
    await user.click(screen.getByTestId("agent-chip-button"));
    await user.click(screen.getByTestId("agent-chip-new-group"));
    expect(props.onNewGroup).toHaveBeenCalledTimes(1);
  });

  it("menu: a 'New chat' row picks the agent, a dimmed row opens its own chat", async () => {
    const user = userEvent.setup();
    const props = renderChip();
    await user.click(screen.getByTestId("agent-chip-button"));
    const menu = screen.getByTestId("agent-chip-menu");
    expect(within(menu).getByTestId("agent-chip-section-new")).toHaveTextContent("New chat");
    expect(within(menu).getByTestId("agent-chip-section-only-one")).toHaveTextContent("Only one chat");
    await user.click(within(menu).getByTestId("agent-chip-row"));
    expect(props.onPick).toHaveBeenCalledWith("s");

    await user.click(screen.getByTestId("agent-chip-button"));
    await user.click(within(screen.getByTestId("agent-chip-menu")).getByTestId("agent-chip-row-dimmed"));
    expect(props.onOpenOwnChat).toHaveBeenCalledWith("b");
  });
});
