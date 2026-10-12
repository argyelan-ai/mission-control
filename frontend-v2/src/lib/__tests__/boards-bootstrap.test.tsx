/**
 * useBoardsBootstrap — loads the board list and picks an active board even
 * when no board switcher is rendered.
 *
 * Regression (12.10.2026): with one board the sidebar stopped rendering the
 * BoardPicker, which was the only place that loaded the boards and set the
 * active one. A fresh browser then showed "No board selected" on Tasks.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

const listBoards = vi.fn();
vi.mock("@/lib/api", () => ({ api: { boards: { list: () => listBoards() } } }));

import { useAppStore } from "@/lib/store";
import { useBoardsBootstrap } from "@/lib/boards";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const board = (id: string) => ({ id, name: id, slug: id, description: null, icon: null, color: null });

describe("useBoardsBootstrap", () => {
  beforeEach(() => {
    listBoards.mockReset();
    useAppStore.setState({ activeBoardId: null, boards: [] });
  });

  it("selects the only board when none is active", async () => {
    listBoards.mockResolvedValue([board("b1")]);
    renderHook(() => useBoardsBootstrap(true), { wrapper });
    await waitFor(() => expect(useAppStore.getState().activeBoardId).toBe("b1"));
  });

  it("replaces a stored board id that no longer exists", async () => {
    useAppStore.setState({ activeBoardId: "gone" });
    listBoards.mockResolvedValue([board("b1"), board("b2")]);
    renderHook(() => useBoardsBootstrap(true), { wrapper });
    await waitFor(() => expect(useAppStore.getState().activeBoardId).toBe("b1"));
  });

  it("keeps a valid active board", async () => {
    useAppStore.setState({ activeBoardId: "b2" });
    listBoards.mockResolvedValue([board("b1"), board("b2")]);
    renderHook(() => useBoardsBootstrap(true), { wrapper });
    await waitFor(() => expect(useAppStore.getState().boards).toHaveLength(2));
    expect(useAppStore.getState().activeBoardId).toBe("b2");
  });

  it("does not fetch before the user is authorized", async () => {
    listBoards.mockResolvedValue([board("b1")]);
    renderHook(() => useBoardsBootstrap(false), { wrapper });
    await new Promise((r) => setTimeout(r, 20));
    expect(listBoards).not.toHaveBeenCalled();
  });
});
