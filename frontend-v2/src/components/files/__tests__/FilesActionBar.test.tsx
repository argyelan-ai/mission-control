import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { FilesActionBar } from "../FilesActionBar";
import type { FsRoot } from "@/lib/types";
import { api } from "@/lib/api";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const ROOT: FsRoot = {
  key: "vault",
  label: "Vault",
  icon: "BookOpen",
  native_open: true,
  indexed_count: 12,
  deletable: true,
};

const READONLY: FsRoot = { ...ROOT, key: "deliverables", deletable: false };

describe("FilesActionBar", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows the count + Download/Delete/Cancel for a non-empty selection", () => {
    renderWithQuery(
      <FilesActionBar root={ROOT} selected={new Set(["a.pdf", "b.txt"])} onClear={() => {}} />
    );
    expect(screen.getByText("2 selected")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Download/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Delete/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Cancel/ })).toBeInTheDocument();
  });

  it("disables Delete with a read-only title when the root is not deletable; click does not open the dialog", async () => {
    renderWithQuery(
      <FilesActionBar root={READONLY} selected={new Set(["a.pdf"])} onClear={() => {}} />
    );
    const del = screen.getByRole("button", { name: /Delete/ });
    expect(del).toBeDisabled();
    expect(del).toHaveAttribute("title", "This area is read-only — delete not available");
    await userEvent.click(del);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("opens the confirm dialog listing filenames when the root is deletable", async () => {
    renderWithQuery(
      <FilesActionBar root={ROOT} selected={new Set(["a.pdf", "sub/b.txt"])} onClear={() => {}} />
    );
    await userEvent.click(screen.getByRole("button", { name: /Delete/ }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toBeInTheDocument();
    // "a.pdf" appears twice (basename + full mono subpath, equal here).
    expect(screen.getAllByText("a.pdf").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("b.txt")).toBeInTheDocument();
  });

  it("Cancel calls onClear", async () => {
    const onClear = vi.fn();
    renderWithQuery(
      <FilesActionBar root={ROOT} selected={new Set(["a.pdf"])} onClear={onClear} />
    );
    await userEvent.click(screen.getByRole("button", { name: /Cancel/ }));
    expect(onClear).toHaveBeenCalled();
  });

  it("Download calls api.files.fetchBlob once per selected file (sequential)", async () => {
    const fetchBlob = vi.spyOn(api.files, "fetchBlob").mockResolvedValue("blob:fake");
    const createObjURL = vi.fn(() => "blob:fake");
    const revokeObjURL = vi.fn();
    // jsdom doesn't implement these on URL.
    (URL as unknown as { createObjectURL: typeof createObjURL }).createObjectURL = createObjURL;
    (URL as unknown as { revokeObjectURL: typeof revokeObjURL }).revokeObjectURL = revokeObjURL;
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    renderWithQuery(
      <FilesActionBar root={ROOT} selected={new Set(["a.pdf", "b.txt"])} onClear={() => {}} />
    );
    await userEvent.click(screen.getByRole("button", { name: /Download/ }));

    await waitFor(() => expect(fetchBlob).toHaveBeenCalledTimes(2));
    expect(fetchBlob).toHaveBeenCalledWith("vault", "a.pdf");
    expect(fetchBlob).toHaveBeenCalledWith("vault", "b.txt");
    expect(clickSpy).toHaveBeenCalledTimes(2);
    expect(revokeObjURL).toHaveBeenCalledTimes(2);
  });
});
