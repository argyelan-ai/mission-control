/**
 * /memory?view=trash — the trash list must scroll.
 *
 * AppShell runs this page in fullHeight mode (outer <main> overflow:hidden),
 * so the trash view needs its own scroll frame. Without it, a long trash list
 * is cut off at the bottom of the viewport with no way to reach the rest.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams("view=trash"),
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
      related: vi.fn(),
      graph: vi.fn().mockResolvedValue({}),
    },
  },
}));
vi.mock("@/hooks/useVaultSearch", () => ({
  useVaultSearch: () => ({ data: undefined, isLoading: false, debouncedQ: "" }),
}));
vi.mock("@/hooks/useVaultList", () => ({
  useVaultList: () => ({
    notes: [], totalCount: 0, isLoading: false, isError: false,
    fetchNextPage: vi.fn(), hasNextPage: false, isFetchingNextPage: false,
  }),
}));
vi.mock("@/hooks/useVaultStream", () => ({ useVaultStream: vi.fn() }));
vi.mock("@/hooks/useVoiceHighlight", () => ({
  useVoiceHighlight: () => ({ voiceFilter: null, onVoiceHighlight: vi.fn(), clearVoiceHighlight: vi.fn() }),
}));
vi.mock("@/components/memory/VoiceHighlightBridge", () => ({ VoiceHighlightBridge: () => null }));
vi.mock("../VaultHeader", () => ({ VaultHeader: () => null }));
vi.mock("../CreateVaultNoteModal", () => ({ CreateVaultNoteModal: () => null }));
vi.mock("@/components/pages/VaultGraphPage", () => ({ VaultGraphPage: () => null }));
vi.mock("../VaultTrashPage", () => ({ VaultTrashPage: () => <div data-testid="trash-page" /> }));

import VaultMemoryPage from "../VaultMemoryPage";

describe("VaultMemoryPage trash view", () => {
  it("mounts the trash list inside its own scroll frame", () => {
    vi.stubGlobal("matchMedia", (q: string) => ({
      matches: false, media: q, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    }));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <VaultMemoryPage />
      </QueryClientProvider>,
    );
    const frame = screen.getByTestId("trash-page").parentElement!;
    expect(frame.className).toMatch(/\bflex-1\b/);
    expect(frame.className).toMatch(/\bmin-h-0\b/);
    expect(frame.className).toMatch(/\boverflow-y-auto\b/);
  });
});
