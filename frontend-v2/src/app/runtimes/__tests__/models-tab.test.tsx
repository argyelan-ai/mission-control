import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ModelsTab } from "../ModelsTab";
import { api } from "@/lib/api";

vi.mock("../VllmContainerCatalog", () => ({
  VllmContainerCatalog: () => <div data-testid="vllm-container-catalog-stub" />,
}));

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("ModelsTab", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("lists installed models without a runtime and adds one as runtime", async () => {
    vi.spyOn(api.lmstudio, "list").mockResolvedValue({ models: [
      { id: "qwen3-8b", display_name: "Qwen3 8B", size_gb: 4.2, is_loaded: false, is_embedding: false },
    ], reachable: true });
    vi.spyOn(api.lmstudio, "downloads").mockResolvedValue({ downloads: [] });
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [] });
    const add = vi.spyOn(api.runtimes, "addLmstudio").mockResolvedValue({} as never);

    renderWithQuery(<ModelsTab />);
    await waitFor(() => expect(screen.getByText("Qwen3 8B")).toBeInTheDocument());
    screen.getByRole("button", { name: /add as runtime/i }).click();
    await waitFor(() => expect(add).toHaveBeenCalledWith({ lms_identifier: "qwen3-8b", display_name: "Qwen3 8B" }));
  });

  it("does not list models that already have a runtime", async () => {
    vi.spyOn(api.lmstudio, "list").mockResolvedValue({ models: [
      { id: "qwen3-8b", display_name: "Qwen3 8B", size_gb: 4.2, is_loaded: false, is_embedding: false },
    ], reachable: true });
    vi.spyOn(api.lmstudio, "downloads").mockResolvedValue({ downloads: [] });
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [
      { id: "rt", slug: "rt", display_name: "Qwen3 8B", runtime_type: "lmstudio", lms_identifier: "qwen3-8b" } as never,
    ] });

    renderWithQuery(<ModelsTab />);
    await waitFor(() => expect(screen.getByTestId("vllm-container-catalog-stub")).toBeInTheDocument());
    expect(screen.queryByText("Qwen3 8B")).not.toBeInTheDocument();
  });

  it("does not hide a model because of a stray lms_identifier on a non-lmstudio runtime", async () => {
    vi.spyOn(api.lmstudio, "list").mockResolvedValue({ models: [
      { id: "qwen3-8b", display_name: "Qwen3 8B", size_gb: 4.2, is_loaded: false, is_embedding: false },
    ], reachable: true });
    vi.spyOn(api.lmstudio, "downloads").mockResolvedValue({ downloads: [] });
    // A vllm_docker runtime that happens to carry the same lms_identifier must
    // NOT count as "this model already has a runtime" — only lmstudio-typed
    // runtimes do (matches the old page.tsx pre-filter).
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [
      { id: "rt", slug: "rt", display_name: "Some vLLM Runtime", runtime_type: "vllm_docker", lms_identifier: "qwen3-8b" } as never,
    ] });

    renderWithQuery(<ModelsTab />);
    await waitFor(() => expect(screen.getByText("Qwen3 8B")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /add as runtime/i })).toBeInTheDocument();
  });

  it("renders the Spark recipes section with the vLLM container catalog", async () => {
    vi.spyOn(api.lmstudio, "list").mockResolvedValue({ models: [], reachable: true });
    vi.spyOn(api.lmstudio, "downloads").mockResolvedValue({ downloads: [] });
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [] });

    renderWithQuery(<ModelsTab />);
    expect(screen.getByText("Spark recipes")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("vllm-container-catalog-stub")).toBeInTheDocument());
  });

  it("renders active downloads above the download-model catalog (spec ordering)", async () => {
    vi.spyOn(api.lmstudio, "list").mockResolvedValue({ models: [], reachable: true });
    vi.spyOn(api.lmstudio, "downloads").mockResolvedValue({ downloads: [
      { id: "dl-1", name: "Some Model", type: "lmstudio", progress_pct: 40, progress_text: "" },
    ] });
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [] });

    renderWithQuery(<ModelsTab />);

    const downloadsHeading = await screen.findByText("Downloads");
    const catalogHeading = await screen.findByText("Download model");
    // DOCUMENT_POSITION_FOLLOWING on catalog (relative to downloads) means
    // downloads comes first in document order.
    expect(
      downloadsHeading.compareDocumentPosition(catalogHeading) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
  });
});
