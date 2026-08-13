import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { LmStudioLocalModels } from "../LmStudioLocalModels";
import { api } from "@/lib/api";
import type { LMStudioModel, LMStudioModelsResponse } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const mkModel = (over: Partial<LMStudioModel> = {}): LMStudioModel => ({
  id: "unattached-model",
  display_name: "Unattached Model",
  size_gb: 4.2,
  is_loaded: false,
  is_embedding: false,
  ...over,
});

describe("LmStudioLocalModels", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [] } as never);
  });

  it("renders nothing when every LM Studio model already has a configured runtime", async () => {
    vi.spyOn(api.runtimes, "list").mockResolvedValue({
      runtimes: [{ id: "rt-1", runtime_type: "lmstudio", lms_identifier: "configured-model" }],
    } as never);
    vi.spyOn(api.lmstudio, "list").mockResolvedValue({
      models: [mkModel({ id: "configured-model" })],
      reachable: true,
    } as LMStudioModelsResponse);

    const { container } = renderWithQuery(<LmStudioLocalModels />);
    await waitFor(() => expect(api.lmstudio.list).toHaveBeenCalled());

    expect(container).toBeEmptyDOMElement();
  });

  it("renders a Load button for an unattached, unloaded model and calls api.lmstudio.load", async () => {
    vi.spyOn(api.lmstudio, "list").mockResolvedValue({
      models: [mkModel({ id: "m1", display_name: "Qwen 3.6 Local", is_loaded: false })],
      reachable: true,
    } as LMStudioModelsResponse);
    const loadSpy = vi.spyOn(api.lmstudio, "load").mockResolvedValue({ ok: true, message: "Loading…" });

    renderWithQuery(<LmStudioLocalModels />);

    expect(await screen.findByText("Qwen 3.6 Local")).toBeInTheDocument();
    const loadBtn = screen.getByTitle("Load");
    loadBtn.click();

    await waitFor(() => expect(loadSpy).toHaveBeenCalledWith("m1", undefined));
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("renders an Unload button for an unattached, loaded model and calls api.lmstudio.unload", async () => {
    vi.spyOn(api.lmstudio, "list").mockResolvedValue({
      models: [mkModel({ id: "m2", display_name: "Loaded Local Model", is_loaded: true })],
      reachable: true,
    } as LMStudioModelsResponse);
    const unloadSpy = vi.spyOn(api.lmstudio, "unload").mockResolvedValue({ ok: true, message: "Unloading…" });

    renderWithQuery(<LmStudioLocalModels />);

    expect(await screen.findByText("Loaded Local Model")).toBeInTheDocument();
    const unloadBtn = screen.getByTitle("Unload");
    unloadBtn.click();

    await waitFor(() => expect(unloadSpy).toHaveBeenCalledWith("m2"));
    expect(await screen.findByText("Unloading…")).toBeInTheDocument();
  });
});
