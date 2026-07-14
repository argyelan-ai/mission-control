import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AgentActions } from "../AgentActions";

const { archive, restore, del, errorToast } = vi.hoisted(() => ({
  archive: vi.fn(async () => ({ id: "a1", archived_at: "2026-07-14T00:00:00Z" })),
  restore: vi.fn(async () => ({ id: "a1", archived_at: null })),
  del: vi.fn(async () => undefined),
  errorToast: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: { agents: { archive, restore, delete: del } },
}));

vi.mock("@/lib/notify", () => ({
  notify: { success: vi.fn(), error: errorToast, warning: vi.fn(), info: vi.fn() },
}));

function wrap(ui: React.ReactNode) {
  return render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
}

const agent = (archived_at: string | null) =>
  ({ id: "a1", name: "Dev", archived_at }) as never;

describe("AgentActions lifecycle", () => {
  beforeEach(() => {
    archive.mockClear();
    restore.mockClear();
    del.mockClear();
    errorToast.mockClear();
  });

  it("active agent: Delete disabled, Archive shown", () => {
    wrap(<AgentActions agent={agent(null)} />);
    const del = screen.getByRole("button", { name: /löschen|delete/i }) as HTMLButtonElement;
    expect(del.disabled).toBe(true);
    expect(del.title).toBe("Erst archivieren");
    expect(screen.getByRole("button", { name: /archiv/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /wiederherstel|restore/i })).toBeNull();
  });

  it("archived agent: Delete enabled, Restore shown", () => {
    wrap(<AgentActions agent={agent("2026-07-14T00:00:00Z")} />);
    const del = screen.getByRole("button", { name: /löschen|delete/i }) as HTMLButtonElement;
    expect(del.disabled).toBe(false);
    expect(screen.getByRole("button", { name: /wiederherstel|restore/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /archiv/i })).toBeNull();
  });

  it("Archivieren calls api.agents.archive", async () => {
    wrap(<AgentActions agent={agent(null)} />);
    fireEvent.click(screen.getByRole("button", { name: /archiv/i }));
    await waitFor(() => expect(archive).toHaveBeenCalledWith("a1"));
  });

  it("Löschen (archived) requires confirm before deleting", async () => {
    wrap(<AgentActions agent={agent("2026-07-14T00:00:00Z")} />);
    fireEvent.click(screen.getByRole("button", { name: /löschen|delete/i }));
    // reveals inline confirm — nothing deleted yet
    expect(del).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /ja, löschen/i }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("a1"));
  });

  it("surfaces backend detail (409) in the error toast, not swallowed", async () => {
    archive.mockRejectedValueOnce(
      new Error('API 409: {"detail":"Agent arbeitet gerade an einem Task."}'),
    );
    wrap(<AgentActions agent={agent(null)} />);
    fireEvent.click(screen.getByRole("button", { name: /archiv/i }));
    await waitFor(() =>
      expect(errorToast).toHaveBeenCalledWith("Agent arbeitet gerade an einem Task."),
    );
  });
});
