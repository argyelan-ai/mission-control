import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";

// next/navigation is mocked so AppShell's auth guard + Sidebar/MobileNav render
// without a real Next router.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/files",
}));

import FilesPage from "../page";

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <FilesPage />
    </QueryClientProvider>
  );
}

describe("FilesPage — root selector", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    // AppShell auth guard requires a token to authorize and render content.
    const store: Record<string, string> = { mc_auth_token: "tok" };
    Object.defineProperty(globalThis, "localStorage", {
      value: {
        getItem: (k: string) => store[k] ?? null,
        setItem: (k: string, v: string) => { store[k] = v; },
        removeItem: (k: string) => { delete store[k]; },
        clear: () => undefined,
      },
      configurable: true,
      writable: true,
    });

    // Generic fetch stub for any unmocked api call (approvals badge, etc.).
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );

    vi.spyOn(api.files, "roots").mockResolvedValue({
      native_open_available: true,
      roots: [
        { key: "deliverables", label: "Deliverables", icon: "Package", native_open: true, indexed_count: 7, deletable: true },
        { key: "vault", label: "Vault", icon: "BookOpen", native_open: true, indexed_count: 42, deletable: true },
      ],
    });
    vi.spyOn(api.files, "list").mockResolvedValue({
      root: "deliverables", subpath: "", entries: [],
    });
  });

  it("renders the page header and the roots from api.files.roots()", async () => {
    renderPage();

    // Header.
    expect(await screen.findByRole("heading", { name: "Files" })).toBeInTheDocument();

    // Both roots appear as selectable tabs, with their indexed_count badges.
    expect(await screen.findByRole("button", { name: /Deliverables/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Vault/ })).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
  });

  it("clears the selection (action bar disappears) when switching root or navigating", async () => {
    // A directory plus a file so we can both select and navigate.
    vi.spyOn(api.files, "list").mockResolvedValue({
      root: "deliverables", subpath: "",
      entries: [
        {
          name: "report.pdf", type: "file", size: 100, mime: null,
          mtime: 1_700_000_000, is_directory: false,
        },
        {
          name: "archive", type: "directory", size: 0, mime: null,
          mtime: 1_700_000_000, is_directory: true,
        },
      ],
    });

    renderPage();

    // Select the file → the floating action bar appears.
    const cb = await screen.findByRole("checkbox", { name: "Select report.pdf" });
    await userEvent.click(cb);
    expect(await screen.findByText("1 selected")).toBeInTheDocument();

    // Switching root clears the selection.
    await userEvent.click(screen.getByRole("button", { name: /Vault/ }));
    await waitFor(() => expect(screen.queryByText("1 selected")).not.toBeInTheDocument());

    // Re-select on deliverables, then navigate into a folder → clears again.
    await userEvent.click(screen.getByRole("button", { name: /Deliverables/ }));
    const cb2 = await screen.findByRole("checkbox", { name: "Select report.pdf" });
    await userEvent.click(cb2);
    expect(await screen.findByText("1 selected")).toBeInTheDocument();
    await userEvent.click(screen.getByText("archive"));
    await waitFor(() => expect(screen.queryByText("1 selected")).not.toBeInTheDocument());
  });

  it("shows a Trash tab even though /roots returns only real roots; clicking it renders TrashView", async () => {
    const trashList = vi.spyOn(api.files.trash, "list").mockResolvedValue({ entries: [] });

    renderPage();

    // The Trash pseudo-root is appended to the strip, not fetched from /roots.
    const trashTab = await screen.findByRole("button", { name: /Trash/ });
    expect(trashTab).toBeInTheDocument();

    await userEvent.click(trashTab);

    // TrashView renders its empty state and queries the trash endpoint.
    await waitFor(() => expect(trashList).toHaveBeenCalled());
    expect(await screen.findByText("Trash is empty")).toBeInTheDocument();
  });
});
