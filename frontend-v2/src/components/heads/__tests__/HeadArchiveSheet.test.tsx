/**
 * HeadArchiveSheet — read-only Archive sheet (bauplan `heads-sichtbar` PR 2
 * §3.2): fetches `archived=true`, filters client-side, selecting a row
 * closes the sheet.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { mkRun } from "@/lib/__tests__/headFixtures";
import { HeadArchiveSheet } from "../HeadArchiveSheet";

function renderSheet(onSelectHead = vi.fn(), onClose = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <HeadArchiveSheet open onClose={onClose} onSelectHead={onSelectHead} />
    </QueryClientProvider>,
  );
  return { onSelectHead, onClose };
}

beforeEach(() => vi.restoreAllMocks());

describe("HeadArchiveSheet", () => {
  it("asks for archived=true at the shared recent_days window", async () => {
    const list = vi.spyOn(api.heads, "list").mockResolvedValue({ runs: [] });
    renderSheet();
    await waitFor(() => expect(list).toHaveBeenCalledWith({ recentDays: 7, archived: true }));
  });

  it("empty archive shows the empty hint, not the no-results one", async () => {
    vi.spyOn(api.heads, "list").mockResolvedValue({ runs: [] });
    renderSheet();
    await waitFor(() => expect(screen.getByText("No older heads yet.")).toBeInTheDocument());
  });

  it("filters by title or pair, case-insensitively", async () => {
    vi.spyOn(api.heads, "list").mockResolvedValue({
      runs: [
        mkRun({ run_id: "a", title: "Fix flaky retry test", harness: "omp", model: "GLM-5.3" }),
        mkRun({ run_id: "b", title: "Rotate the API key", harness: "claude", model: "claude-opus" }),
      ],
    });
    renderSheet();
    await waitFor(() => expect(screen.getAllByTestId("head-chat-row")).toHaveLength(2));
    await userEvent.type(screen.getByTestId("head-archive-search"), "flaky");
    expect(screen.getAllByTestId("head-chat-row")).toHaveLength(1);
    expect(screen.getByTestId("head-chat-row")).toHaveTextContent("Fix flaky retry test");

    await userEvent.clear(screen.getByTestId("head-archive-search"));
    await userEvent.type(screen.getByTestId("head-archive-search"), "claude");
    expect(screen.getAllByTestId("head-chat-row")).toHaveLength(1);
    expect(screen.getByTestId("head-chat-row")).toHaveTextContent("Rotate the API key");
  });

  it("a query matching nothing shows the no-results hint", async () => {
    vi.spyOn(api.heads, "list").mockResolvedValue({ runs: [mkRun({ title: "Fix flaky retry test" })] });
    renderSheet();
    await waitFor(() => expect(screen.getAllByTestId("head-chat-row")).toHaveLength(1));
    await userEvent.type(screen.getByTestId("head-archive-search"), "nonsense-query");
    expect(screen.getByText("No matches.")).toBeInTheDocument();
  });

  it("selecting a row calls onSelectHead and closes the sheet", async () => {
    vi.spyOn(api.heads, "list").mockResolvedValue({ runs: [mkRun({ run_id: "archived-1" })] });
    const { onSelectHead, onClose } = renderSheet();
    await waitFor(() => expect(screen.getByTestId("head-chat-row")).toBeInTheDocument());
    await userEvent.click(screen.getByTestId("head-chat-row"));
    expect(onSelectHead).toHaveBeenCalledWith("archived-1");
    expect(onClose).toHaveBeenCalled();
  });
});
