/**
 * Round-4 user test t4 SHOULD-1: the memory header said "4 agents" and the
 * agent filter offered 4, in a vault with notes from 19 agents — both came
 * from the 50 notes of the first page. The list hook hands on the server's
 * agent facet (every agent of the whole filtered set).
 */
import React from "react";
import { describe, it, expect, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useVaultList } from "../useVaultList";

describe("useVaultList — agents", () => {
  it("names every agent of the vault, not only those on the loaded page", async () => {
    vi.spyOn(api.vault, "list").mockResolvedValue({
      count: 62,
      notes: [
        { id: "r1", path: "agents/researcher/k/r1.md", agent: "researcher", type: "knowledge", tags: "", project: "", title: "r1", date: "", content: "" },
      ],
      agents: [
        { agent: "cody", count: 1 },
        { agent: "researcher", count: 60 },
        { agent: "rex", count: 1 },
      ],
    });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useVaultList({}), { wrapper });
    await waitFor(() => expect(result.current.notes).toHaveLength(1));
    expect(result.current.agents).toEqual(["cody", "researcher", "rex"]);
  });

  it("an older backend without the facet: falls back to the loaded notes", async () => {
    vi.spyOn(api.vault, "list").mockResolvedValue({
      count: 2,
      notes: [
        { id: "a", path: "a.md", agent: "beta", type: "note", tags: "", project: "", title: "a", date: "", content: "" },
        { id: "b", path: "b.md", agent: "boss", type: "note", tags: "", project: "", title: "b", date: "", content: "" },
      ],
    });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useVaultList({}), { wrapper });
    await waitFor(() => expect(result.current.notes).toHaveLength(2));
    expect(result.current.agents).toEqual(["beta", "boss"]);
  });
});
