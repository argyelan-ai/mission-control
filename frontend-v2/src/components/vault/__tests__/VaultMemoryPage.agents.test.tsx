/**
 * Round-4 user test t4 SHOULD-1: "1.881 notes · 4 agents" and an agent
 * filter with 4 choices, in a vault with notes from 19 agents. Header and
 * filter now take the agents of the whole vault (the list's agent facet),
 * not the agents that happen to be on the first page.
 */
import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { VaultNote } from "@/lib/types";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/memory",
}));
vi.mock("@/components/layout/AppShell", () => ({
  __esModule: true,
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/lib/api", () => ({
  api: {
    vault: {
      trash: { list: vi.fn().mockResolvedValue({ count: 0, items: [] }) },
      related: vi.fn().mockResolvedValue({ notes: [], count: 0 }),
      graph: vi.fn().mockResolvedValue({}),
    },
  },
}));

const PAGE: VaultNote[] = [
  { path: "agents/head/a.md", agent: "head", type: "note", tags: "", project: "", title: "A", date: "", content: "" } as VaultNote,
  { path: "agents/sparky/b.md", agent: "sparky", type: "note", tags: "", project: "", title: "B", date: "", content: "" } as VaultNote,
];
const ALL_AGENTS = ["boss", "head", "jarvis", "researcher", "rex", "sparky", "system"];

vi.mock("@/hooks/useVaultSearch", () => ({
  VAULT_SEARCH_LIMIT: 100,
  useVaultSearch: () => ({ data: undefined, isLoading: false, debouncedQ: "" }),
}));
vi.mock("@/hooks/useVaultList", () => ({
  useVaultList: () => ({
    notes: PAGE, totalCount: 1881, agents: ALL_AGENTS, isLoading: false, isError: false,
    fetchNextPage: vi.fn(), hasNextPage: true, isFetchingNextPage: false,
  }),
}));
vi.mock("@/hooks/useVaultNote", () => ({
  useVaultNote: () => ({ data: undefined, isLoading: false, isError: false }),
  useVaultNotePreview: () => ({ data: undefined }),
}));
vi.mock("@/hooks/useVaultStream", () => ({ useVaultStream: vi.fn() }));
vi.mock("@/hooks/useVoiceHighlight", () => ({
  useVoiceHighlight: () => ({ voiceFilter: null, onVoiceHighlight: vi.fn(), clearVoiceHighlight: vi.fn() }),
}));
vi.mock("@/components/memory/VoiceHighlightBridge", () => ({ VoiceHighlightBridge: () => null }));
vi.mock("../VaultHeader", () => ({
  VaultHeader: ({ agentCount }: { agentCount: number }) => <div data-testid="agent-count">{agentCount}</div>,
}));
vi.mock("../VaultFilterStrip", () => ({
  VaultFilterStrip: ({ agents }: { agents: string[] }) => <div data-testid="filter-agents">{agents.join(",")}</div>,
}));
vi.mock("../CreateVaultNoteModal", () => ({ CreateVaultNoteModal: () => null }));
vi.mock("@/components/pages/VaultGraphPage", () => ({ VaultGraphPage: () => null }));
vi.mock("../VaultTrashPage", () => ({ VaultTrashPage: () => null }));
vi.stubGlobal("matchMedia", (q: string) => ({
  matches: false, media: q, addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn(),
}));
vi.stubGlobal("IntersectionObserver", class { observe() {} disconnect() {} unobserve() {} });

import VaultMemoryPage from "../VaultMemoryPage";

describe("Memory — agents in header and filter", () => {
  it("counts and offers every agent of the vault, not the first page's two", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <VaultMemoryPage />
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("agent-count")).toHaveTextContent("7");
    expect(screen.getByTestId("filter-agents")).toHaveTextContent(ALL_AGENTS.join(","));
  });
});
