import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { PurgeTrashDialog } from "../PurgeTrashDialog";
import { api } from "@/lib/api";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return { qc, ...render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>) };
}

const TRASH_IDS = [
  "20260101-120000/deliverables/a.pdf",
  "20260102-090000/vault/note.md",
];

describe("PurgeTrashDialog", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the irreversible warning copy and the basenames", () => {
    renderWithQuery(
      <PurgeTrashDialog open trashIds={TRASH_IDS} onClose={() => {}} onDone={() => {}} />
    );
    expect(screen.getByText(/unwiderruflich|NICHT wiederhergestellt/i)).toBeInTheDocument();
    expect(screen.getByText("a.pdf")).toBeInTheDocument();
    expect(screen.getByText("note.md")).toBeInTheDocument();
  });

  it("does not purge on mount — requires the explicit confirm click", async () => {
    const purge = vi.spyOn(api.files.trash, "purge").mockResolvedValue({ purged: [], skipped: [] });
    renderWithQuery(
      <PurgeTrashDialog open trashIds={TRASH_IDS} onClose={() => {}} onDone={() => {}} />
    );
    // No call before any interaction.
    expect(purge).not.toHaveBeenCalled();
  });

  it("confirm calls api.files.trash.purge, invalidates 3 keys + calls onDone", async () => {
    const purge = vi.spyOn(api.files.trash, "purge").mockResolvedValue({
      purged: TRASH_IDS,
      skipped: [],
    });
    const onDone = vi.fn();
    const { qc } = renderWithQuery(
      <PurgeTrashDialog open trashIds={TRASH_IDS} onClose={() => {}} onDone={onDone} />
    );
    const invalidate = vi.spyOn(qc, "invalidateQueries");

    await userEvent.click(screen.getByRole("button", { name: /Endgültig löschen/ }));

    await waitFor(() => expect(purge).toHaveBeenCalledWith(TRASH_IDS));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["files-trash"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["files-list"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["files-roots"] });
    await waitFor(() => expect(onDone).toHaveBeenCalled());
  });
});
