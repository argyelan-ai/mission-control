import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MCPAddServerModal } from "../MCPAddServerModal";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("MCPAddServerModal", () => {
  beforeEach(() => { vi.restoreAllMocks(); });

  it("renders title, name input, transport select, and submit button", () => {
    renderWithQuery(<MCPAddServerModal onClose={() => {}} onSuccess={() => {}} />);
    expect(screen.getByText("MCP-Server hinzufügen")).toBeInTheDocument();
    expect(screen.getByLabelText(/name/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/transport/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /hinzufügen|submit|speichern/i })).toBeInTheDocument();
  });

  it("transport=stdio shows command and args inputs (and no url)", () => {
    renderWithQuery(<MCPAddServerModal onClose={() => {}} onSuccess={() => {}} />);
    expect(screen.getByLabelText(/command/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/args/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/^url$/i)).toBeNull();
  });

  it("transport=http shows url input (and no command)", async () => {
    renderWithQuery(<MCPAddServerModal onClose={() => {}} onSuccess={() => {}} />);
    await userEvent.selectOptions(screen.getByLabelText(/transport/i), "http");
    expect(screen.getByLabelText(/^url$/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/command/i)).toBeNull();
  });

  it("invalid name disables submit and shows hint", async () => {
    renderWithQuery(<MCPAddServerModal onClose={() => {}} onSuccess={() => {}} />);
    const nameInput = screen.getByLabelText(/name/i);
    await userEvent.type(nameInput, "bad/name");
    expect(screen.getByText(/nur a-z, 0-9, _, - erlaubt/i)).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: /hinzufügen|submit|speichern/i });
    expect(submit).toBeDisabled();
  });

  it("happy submit calls api.create with correct body and fires onSuccess", async () => {
    const create = vi.spyOn(api.mcpServers, "create").mockResolvedValue({
      name: "filesystem", transport: "stdio", command: "uvx fs-mcp",
    } as never);
    const successSpy = vi.spyOn(notify, "success").mockImplementation(() => {});
    const onSuccess = vi.fn();
    renderWithQuery(<MCPAddServerModal onClose={() => {}} onSuccess={onSuccess} />);
    await userEvent.type(screen.getByLabelText(/name/i), "filesystem");
    await userEvent.type(screen.getByLabelText(/command/i), "uvx fs-mcp");
    await userEvent.click(screen.getByRole("button", { name: /hinzufügen|submit|speichern/i }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalled());
    expect(create).toHaveBeenCalledWith(expect.objectContaining({
      name: "filesystem", transport: "stdio", command: "uvx fs-mcp",
    }));
    expect(successSpy).toHaveBeenCalled();
  });

  it("error response surfaces via notify.error and modal stays open", async () => {
    vi.spyOn(api.mcpServers, "create").mockRejectedValue(new Error("Server already exists"));
    const errorSpy = vi.spyOn(notify, "error").mockImplementation(() => {});
    const onSuccess = vi.fn();
    const onClose = vi.fn();
    renderWithQuery(<MCPAddServerModal onClose={onClose} onSuccess={onSuccess} />);
    await userEvent.type(screen.getByLabelText(/name/i), "filesystem");
    await userEvent.type(screen.getByLabelText(/command/i), "uvx fs-mcp");
    await userEvent.click(screen.getByRole("button", { name: /hinzufügen|submit|speichern/i }));
    await waitFor(() => expect(errorSpy).toHaveBeenCalledWith("Server already exists"));
    expect(onSuccess).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("Escape key calls onClose", async () => {
    const onClose = vi.fn();
    renderWithQuery(<MCPAddServerModal onClose={onClose} onSuccess={() => {}} />);
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });
});
