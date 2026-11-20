import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useChatAgentIds } from "../useChatAgentIds";

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("useChatAgentIds — the agents the chat page can open", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("is the docker-session and host-session agents together", async () => {
    vi.spyOn(api.agents, "listDockerSessions").mockResolvedValue([{ id: "d1" }, { id: "d2" }] as never);
    vi.spyOn(api.agents, "listHostSessions").mockResolvedValue([{ id: "h1" }] as never);
    const { result } = renderHook(() => useChatAgentIds(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current?.size).toBe(3));
    expect([...result.current!].sort()).toEqual(["d1", "d2", "h1"]);
  });

  it("an agent in neither list (archived, other runtime) is not in the set", async () => {
    vi.spyOn(api.agents, "listDockerSessions").mockResolvedValue([{ id: "d1" }] as never);
    vi.spyOn(api.agents, "listHostSessions").mockResolvedValue([] as never);
    const { result } = renderHook(() => useChatAgentIds(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current).not.toBeNull());
    expect(result.current!.has("archived-downloader")).toBe(false);
  });

  it("is null while nothing has loaded, and stays usable when one list fails", async () => {
    vi.spyOn(api.agents, "listDockerSessions").mockRejectedValue(new Error("API 500"));
    let resolveHost: (v: never) => void = () => {};
    vi.spyOn(api.agents, "listHostSessions").mockReturnValue(new Promise((r) => { resolveHost = r; }) as never);
    const { result } = renderHook(() => useChatAgentIds(), { wrapper: wrapper() });
    expect(result.current).toBeNull();
    resolveHost([{ id: "h1" }] as never);
    await waitFor(() => expect(result.current?.has("h1")).toBe(true));
  });
});
