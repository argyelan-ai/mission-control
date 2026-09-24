/**
 * TerminalPanel — the agent terminal is admin-only (backend closes the
 * WebSocket with 4003 for everyone else). Non-admins get a hint and the
 * panel never asks for a terminal stream ticket.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    cols = 80;
    rows = 24;
    element = null;
    options = {};
    loadAddon() {}
    open() {}
    write() {}
    writeln() {}
    clear() {}
    focus() {}
    resize() {}
    refresh() {}
    dispose() {}
    onData() {
      return { dispose() {} };
    }
    onResize() {
      return { dispose() {} };
    }
  },
}));
vi.mock("@xterm/xterm/css/xterm.css", () => ({}));

const apiMock = vi.hoisted(() => ({
  cliSessions: {
    ptyWsUrl: vi.fn(() => new Promise<string>(() => {})),
    hostPtyWsUrl: vi.fn(() => new Promise<string>(() => {})),
  },
}));
vi.mock("@/lib/api", () => ({ api: apiMock }));

// The admin gate reads the role through useIsAdmin (the persisted store
// needs a real localStorage, which this jsdom lacks) — stub the hook.
const roleMock = vi.hoisted(() => ({ role: "admin" }));
vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: () => roleMock.role === "admin" }));
function setRole(role: string) {
  roleMock.role = role;
}
import { TerminalPanel, type AgentWithState } from "./TerminalPanel";


const RUNNING = {
  id: "agent-1",
  name: "Cody",
  agent_runtime: "cli-bridge",
  container_state: "running",
} as unknown as AgentWithState;

beforeEach(() => {
  apiMock.cliSessions.ptyWsUrl.mockClear();
  apiMock.cliSessions.hostPtyWsUrl.mockClear();
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});

describe("TerminalPanel — admin-only", () => {
  it.each(["viewer", "operator"])("%s sees a hint and opens no terminal", async (role) => {
    setRole(role);
    render(<TerminalPanel agent={RUNNING} />);
    expect(screen.getByText(/Only admins can open agent terminals/)).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 30));
    expect(apiMock.cliSessions.ptyWsUrl).not.toHaveBeenCalled();
  });

  it("a viewer is not offered a host-agent terminal either", async () => {
    setRole("viewer");
    render(<TerminalPanel agent={{ ...RUNNING, agent_runtime: "host", session_running: true } as AgentWithState} />);
    expect(screen.getByTestId("admin-only-notice")).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 30));
    expect(apiMock.cliSessions.hostPtyWsUrl).not.toHaveBeenCalled();
  });

  it("an admin gets the terminal (asks for its stream ticket)", async () => {
    setRole("admin");
    render(<TerminalPanel agent={RUNNING} />);
    expect(screen.queryByTestId("admin-only-notice")).not.toBeInTheDocument();
    await waitFor(() => expect(apiMock.cliSessions.ptyWsUrl).toHaveBeenCalledWith("agent-1"));
  });
});
