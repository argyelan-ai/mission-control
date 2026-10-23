/**
 * /memory on the phone: opening a note must add a history step, so the
 * browser back button / iOS swipe returns to the note list instead of
 * leaving Memory (round-3 user test t4 S6). The desktop list keeps
 * switching in place (no history entry per click).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { VaultNote } from "@/lib/types";

const nav = vi.hoisted(() => ({
  params: new URLSearchParams(),
  push: vi.fn(),
  replace: vi.fn(),
  back: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: nav.back }),
  useSearchParams: () => nav.params,
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

const NOTES: VaultNote[] = [
  {
    path: "agents/x/a.md", agent: "x", type: "note", tags: "", project: "",
    title: "Alpha note", date: "2026-10-23", content: "first body",
  } as VaultNote,
  {
    path: "agents/x/b.md", agent: "x", type: "note", tags: "", project: "",
    title: "Beta note", date: "2026-10-22", content: "second body",
  } as VaultNote,
];

vi.mock("@/hooks/useVaultSearch", () => ({
  VAULT_SEARCH_LIMIT: 100,
  useVaultSearch: () => ({ data: undefined, isLoading: false, debouncedQ: "" }),
}));
vi.mock("@/hooks/useVaultList", () => ({
  useVaultList: () => ({
    notes: NOTES, totalCount: 2, isLoading: false, isError: false,
    fetchNextPage: vi.fn(), hasNextPage: false, isFetchingNextPage: false,
  }),
}));
vi.mock("@/hooks/useVaultNote", () => ({
  useVaultNote: (path: string) => ({
    data: { path, content: "body of " + path, frontmatter: {} },
    isLoading: false,
    isError: false,
  }),
  useVaultNotePreview: () => ({ data: undefined }),
}));
vi.mock("@/hooks/useVaultStream", () => ({ useVaultStream: vi.fn() }));
vi.mock("@/hooks/useVoiceHighlight", () => ({
  useVoiceHighlight: () => ({ voiceFilter: null, onVoiceHighlight: vi.fn(), clearVoiceHighlight: vi.fn() }),
}));
vi.mock("@/components/memory/VoiceHighlightBridge", () => ({ VoiceHighlightBridge: () => null }));
vi.mock("../VaultHeader", () => ({ VaultHeader: () => null }));
vi.mock("../CreateVaultNoteModal", () => ({ CreateVaultNoteModal: () => null }));
vi.mock("@/components/pages/VaultGraphPage", () => ({ VaultGraphPage: () => null }));
vi.mock("../VaultTrashPage", () => ({ VaultTrashPage: () => null }));

import VaultMemoryPage from "../VaultMemoryPage";

function setViewport(phone: boolean) {
  // Only the page's own "(max-width: 767px)" query answers `phone`; other
  // queries (framer-motion's reduced-motion probe) stay false.
  vi.stubGlobal("matchMedia", (q: string) => ({
    matches: phone && q.includes("max-width"),
    media: q,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
  }));
}

vi.stubGlobal(
  "IntersectionObserver",
  class {
    observe() {}
    disconnect() {}
    unobserve() {}
  },
);

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui = () => (
    <QueryClientProvider client={qc}>
      <VaultMemoryPage />
    </QueryClientProvider>
  );
  const r = render(ui());
  return { ...r, rerenderPage: () => r.rerender(ui()) };
}

beforeEach(() => {
  nav.params = new URLSearchParams();
  nav.push.mockReset();
  nav.replace.mockReset();
  nav.back.mockReset();
});

describe("VaultMemoryPage — note navigation", () => {
  it("phone: opening a note pushes a history entry (swipe back → list)", () => {
    setViewport(true);
    renderPage();
    fireEvent.click(screen.getByText("Alpha note"));
    expect(nav.push).toHaveBeenCalledWith(
      "/memory?path=agents%2Fx%2Fa.md",
      { scroll: false },
    );
    expect(nav.replace).not.toHaveBeenCalledWith(
      expect.stringContaining("path="),
      expect.anything(),
    );
  });

  it("phone: the in-app back button steps back in history after opening from the list", () => {
    setViewport(true);
    renderPage();
    fireEvent.click(screen.getByText("Alpha note"));
    fireEvent.click(screen.getByRole("button", { name: "Back to list" }));
    expect(nav.back).toHaveBeenCalledTimes(1);
  });

  it("phone: a deep link has no list behind it — back button replaces to /memory", () => {
    setViewport(true);
    nav.params = new URLSearchParams("path=agents/x/a.md");
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Back to list" }));
    expect(nav.back).not.toHaveBeenCalled();
    expect(nav.replace).toHaveBeenCalledWith("/memory", { scroll: false });
  });

  it("phone: the browser going back (path leaves the URL) closes the note", async () => {
    setViewport(true);
    nav.params = new URLSearchParams("path=agents/x/a.md");
    const { rerenderPage } = renderPage();
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    nav.params = new URLSearchParams();
    act(() => rerenderPage());
    // The sheet slides out (exit animation) before it unmounts.
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("desktop: switching notes stays in place (replace, no history entry)", () => {
    setViewport(false);
    renderPage();
    nav.replace.mockClear();
    fireEvent.click(screen.getByText("Beta note"));
    expect(nav.replace).toHaveBeenCalledWith(
      "/memory?path=agents%2Fx%2Fb.md",
      { scroll: false },
    );
    expect(nav.push).not.toHaveBeenCalled();
  });
});
