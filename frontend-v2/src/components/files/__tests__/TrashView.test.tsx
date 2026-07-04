import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TrashView } from "../TrashView";
import type { TrashEntry } from "@/lib/types";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return { qc, ...render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>) };
}

const ENTRY_A: TrashEntry = {
  trash_id: "20260101-120000/deliverables/a.pdf",
  original_root: "deliverables",
  original_subpath: "a.pdf",
  name: "a.pdf",
  size: 2048,
  mtime: 1_700_000_000,
  deleted_at: "2026-01-01T12:00:00",
};

const ENTRY_B: TrashEntry = {
  trash_id: "20260102-090000/vault/note.md",
  original_root: "vault",
  original_subpath: "notes/note.md",
  name: "note.md",
  size: 512,
  mtime: 1_700_100_000,
  deleted_at: "2026-01-02T09:00:00",
};

describe("TrashView", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders entries grouped by deleted_at, newest group first, with location strings", async () => {
    vi.spyOn(api.files.trash, "list").mockResolvedValue({ entries: [ENTRY_A, ENTRY_B] });
    renderWithQuery(<TrashView />);

    // Names appear.
    expect(await screen.findByText("a.pdf")).toBeInTheDocument();
    expect(screen.getByText("note.md")).toBeInTheDocument();

    // Location strings — "root · subpath" (split across element, so match by text fn).
    expect(screen.getByText(/deliverables · a\.pdf/)).toBeInTheDocument();
    expect(screen.getByText(/vault · notes\/note\.md/)).toBeInTheDocument();

    // Two distinct group headers, newest (Jan 02) before older (Jan 01).
    const jan02 = screen.getByText(/02\.01\.2026/);
    const jan01 = screen.getByText(/01\.01\.2026/);
    expect(jan02).toBeInTheDocument();
    expect(jan01).toBeInTheDocument();
    // DOM order: the newer header precedes the older one.
    expect(jan02.compareDocumentPosition(jan01) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("clicking a row's restore calls api with [that trash_id], invalidates 3 keys + toasts", async () => {
    vi.spyOn(api.files.trash, "list").mockResolvedValue({ entries: [ENTRY_A] });
    const restore = vi.spyOn(api.files.trash, "restore").mockResolvedValue({
      restored: [{ trash_id: ENTRY_A.trash_id, root: "deliverables", subpath: "a.pdf" }],
      skipped: [],
    });
    const success = vi.spyOn(notify, "success");
    const { qc } = renderWithQuery(<TrashView />);
    const invalidate = vi.spyOn(qc, "invalidateQueries");

    await screen.findByText("a.pdf");
    await userEvent.click(screen.getByRole("button", { name: /Restore/ }));

    await waitFor(() => expect(restore).toHaveBeenCalledWith([ENTRY_A.trash_id]));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["files-trash"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["files-list"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["files-roots"] });
    await waitFor(() => expect(success).toHaveBeenCalled());
  });

  it("renders the empty state and no purge button when the trash is empty", async () => {
    vi.spyOn(api.files.trash, "list").mockResolvedValue({ entries: [] });
    renderWithQuery(<TrashView />);

    expect(await screen.findByText("Trash is empty")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Empty trash/ })).not.toBeInTheDocument();
  });
});
