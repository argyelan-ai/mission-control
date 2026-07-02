import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DeleteFilesDialog } from "../DeleteFilesDialog";
import type { FsRoot } from "@/lib/types";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return { qc, ...render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>) };
}

const ROOT: FsRoot = {
  key: "vault",
  label: "Vault",
  icon: "BookOpen",
  native_open: true,
  indexed_count: 12,
  deletable: true,
};

const SUBPATHS = ["a.pdf", "sub/b.txt"];

describe("DeleteFilesDialog", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the trash note and both basenames", () => {
    renderWithQuery(
      <DeleteFilesDialog open root={ROOT} subpaths={SUBPATHS} onClose={() => {}} onDone={() => {}} />
    );
    expect(screen.getByText(/Papierkorb/)).toBeInTheDocument();
    expect(screen.getByText(/wiederherstellbar/)).toBeInTheDocument();
    // "a.pdf" appears twice (basename + full mono subpath, which are equal here).
    expect(screen.getAllByText("a.pdf").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("b.txt")).toBeInTheDocument();
  });

  it("confirm calls api.files.delete, invalidates both query keys, and calls onDone", async () => {
    const del = vi.spyOn(api.files, "delete").mockResolvedValue({
      trashed: [
        { root: "vault", subpath: "a.pdf", trash_path: "x" },
        { root: "vault", subpath: "sub/b.txt", trash_path: "y" },
      ],
      skipped: [],
      cascaded_deliverables: 0,
    });
    const onDone = vi.fn();
    const { qc } = renderWithQuery(
      <DeleteFilesDialog open root={ROOT} subpaths={SUBPATHS} onClose={() => {}} onDone={onDone} />
    );
    const invalidate = vi.spyOn(qc, "invalidateQueries");

    await userEvent.click(screen.getByRole("button", { name: /Löschen/ }));

    await waitFor(() => expect(del).toHaveBeenCalledWith("vault", SUBPATHS));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["files-list"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["files-roots"] });
    await waitFor(() => expect(onDone).toHaveBeenCalled());
  });

  it("shows a skipped summary toast when the response has skipped entries", async () => {
    vi.spyOn(api.files, "delete").mockResolvedValue({
      trashed: [{ root: "vault", subpath: "a.pdf", trash_path: "x" }],
      skipped: [{ root: "vault", subpath: "sub/b.txt", reason: "locked" }],
      cascaded_deliverables: 0,
    });
    const success = vi.spyOn(notify, "success");
    renderWithQuery(
      <DeleteFilesDialog open root={ROOT} subpaths={SUBPATHS} onClose={() => {}} onDone={() => {}} />
    );
    await userEvent.click(screen.getByRole("button", { name: /Löschen/ }));
    await waitFor(() => expect(success).toHaveBeenCalled());
    expect(success.mock.calls[0][0]).toContain("übersprungen");
  });
});
