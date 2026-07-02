import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MCPServerMatrix } from "../MCPServerMatrix";
import type { Agent, MCPServer } from "@/lib/types";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const servers: MCPServer[] = [
  { name: "filesystem", transport: "stdio", description: "files" },
  { name: "supabase", transport: "sse", description: "DB" },
];

const baseServers: MCPServer[] = [
  { name: "filesystem", transport: "stdio", description: "files" },
  { name: "supabase", transport: "sse", description: "DB" },
];

const agentNullMcp: Agent = {
  id: "agent-1",
  name: "Cody",
  emoji: "🤖",
  mcp_servers: null,
} as Agent;

const agentWithSupabase: Agent = {
  id: "agent-1",
  name: "Cody",
  emoji: "🤖",
  mcp_servers: ["supabase"],
} as Agent;

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("MCPServerMatrix", () => {
  it("renders MCP server names", () => {
    renderWithQuery(<MCPServerMatrix servers={servers} agents={[]} />);
    expect(screen.getByText("filesystem")).toBeInTheDocument();
    expect(screen.getByText("supabase")).toBeInTheDocument();
  });

  it("renders transport descriptions", () => {
    renderWithQuery(<MCPServerMatrix servers={servers} agents={[]} />);
    expect(screen.getByText("files")).toBeInTheDocument();
    expect(screen.getByText("DB")).toBeInTheDocument();
  });

  it("shows empty state when no servers", () => {
    renderWithQuery(<MCPServerMatrix servers={[]} agents={[]} />);
    expect(screen.getByText(/keine mcp-server|no mcp servers/i)).toBeInTheDocument();
  });

  it("toggle on null-mcp agent sends explicit list minus toggled server", async () => {
    const setForAgent = vi
      .spyOn(api.mcpServers, "setForAgent")
      .mockResolvedValue(undefined as never);
    renderWithQuery(<MCPServerMatrix servers={baseServers} agents={[agentNullMcp]} />);
    await userEvent.click(screen.getByLabelText(/Deactivate filesystem for Cody/i));
    expect(setForAgent).toHaveBeenCalledTimes(1);
    expect(setForAgent).toHaveBeenCalledWith("agent-1", ["supabase"]);
  });

  it("toggle re-add adds server to existing explicit list", async () => {
    const setForAgent = vi
      .spyOn(api.mcpServers, "setForAgent")
      .mockResolvedValue(undefined as never);
    renderWithQuery(<MCPServerMatrix servers={baseServers} agents={[agentWithSupabase]} />);
    await userEvent.click(screen.getByLabelText(/Activate filesystem for Cody/i));
    expect(setForAgent).toHaveBeenCalledWith("agent-1", ["supabase", "filesystem"]);
  });

  it("renders command in server row when present (MCP-06)", () => {
    const withCmd: MCPServer[] = [
      { name: "myserver", transport: "stdio", command: "uvx myserver-mcp", description: "test" },
    ];
    renderWithQuery(<MCPServerMatrix servers={withCmd} agents={[]} />);
    expect(screen.getByText("uvx myserver-mcp")).toBeInTheDocument();
  });

  it("renders url in server row when transport is http/sse (MCP-06)", () => {
    const withUrl: MCPServer[] = [
      {
        name: "remote",
        transport: "sse",
        url: "https://example.com/mcp",
        description: "test",
      },
    ];
    renderWithQuery(<MCPServerMatrix servers={withUrl} agents={[]} />);
    expect(screen.getByText("https://example.com/mcp")).toBeInTheDocument();
  });

  it("shows delete button only when showDeleteButton=true", async () => {
    const onDelete = vi.fn();
    const { rerender } = renderWithQuery(
      <MCPServerMatrix servers={baseServers} agents={[]} />,
    );
    expect(screen.queryByTitle("filesystem entfernen")).toBeNull();
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MCPServerMatrix
          servers={baseServers}
          agents={[]}
          showDeleteButton
          onDeleteServer={onDelete}
        />
      </QueryClientProvider>,
    );
    await userEvent.click(screen.getByTitle("filesystem entfernen"));
    expect(onDelete).toHaveBeenCalledWith("filesystem");
  });

  it("rolls back optimistic toggle and shows error toast on API failure", async () => {
    vi.spyOn(api.mcpServers, "setForAgent").mockRejectedValue(new Error("boom"));
    const errorSpy = vi.spyOn(notify, "error").mockImplementation(() => {});
    renderWithQuery(<MCPServerMatrix servers={baseServers} agents={[agentNullMcp]} />);
    const btn = screen.getByLabelText(/Deactivate filesystem for Cody/i);
    await userEvent.click(btn);
    // Wait one microtask for mutation rejection to settle
    await new Promise((r) => setTimeout(r, 0));
    expect(errorSpy).toHaveBeenCalled();
    // After rollback, button label is back to "Deactivate" (state restored to enabled)
    expect(screen.getByLabelText(/Deactivate filesystem for Cody/i)).toBeInTheDocument();
  });
});
