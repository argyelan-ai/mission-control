import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AddRuntimeModal } from "../AddRuntimeModal";
import { api } from "@/lib/api";
import type { ProbeEndpointResult, Runtime } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("AddRuntimeModal", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("probes the URL and shows the detected type + preselected model", async () => {
    const probeResult: ProbeEndpointResult = {
      reachable: true,
      models: ["m1", "m2"],
      detected_type: "vllm_docker",
      suggested_model: "m1",
      error: null,
    };
    const probeSpy = vi.spyOn(api.runtimes, "probeEndpoint").mockResolvedValue(probeResult);

    renderWithQuery(<AddRuntimeModal open={true} onClose={vi.fn()} />);

    const urlInput = screen.getByPlaceholderText(/http/i);
    await userEvent.type(urlInput, "http://192.0.2.10:8000/v1");
    await userEvent.click(screen.getByRole("button", { name: /probe/i }));

    await waitFor(() => expect(probeSpy).toHaveBeenCalledWith("http://192.0.2.10:8000/v1"));

    expect(await screen.findByText("vLLM")).toBeInTheDocument();
    const select = await screen.findByRole("combobox");
    expect((select as HTMLSelectElement).value).toBe("m1");
    expect(screen.getByText("m1")).toBeInTheDocument();
    expect(screen.getByText("m2")).toBeInTheDocument();
  });

  it("shows the error text and no create button when unreachable", async () => {
    const probeResult: ProbeEndpointResult = {
      reachable: false,
      models: [],
      detected_type: null,
      suggested_model: null,
      error: "timeout",
    };
    vi.spyOn(api.runtimes, "probeEndpoint").mockResolvedValue(probeResult);

    renderWithQuery(<AddRuntimeModal open={true} onClose={vi.fn()} />);

    await userEvent.type(screen.getByPlaceholderText(/http/i), "http://192.0.2.99:8000/v1");
    await userEvent.click(screen.getByRole("button", { name: /probe/i }));

    expect(await screen.findByText("timeout")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add runtime/i })).toBeNull();
  });

  it("calls create with the right body when confirming", async () => {
    const probeResult: ProbeEndpointResult = {
      reachable: true,
      models: ["m1"],
      detected_type: "vllm_docker",
      suggested_model: "m1",
      error: null,
    };
    vi.spyOn(api.runtimes, "probeEndpoint").mockResolvedValue(probeResult);
    const createSpy = vi
      .spyOn(api.runtimes.db, "create")
      .mockResolvedValue({ id: "rt-1" } as Runtime);

    renderWithQuery(<AddRuntimeModal open={true} onClose={vi.fn()} />);

    await userEvent.type(screen.getByPlaceholderText(/http/i), "http://192.0.2.10:8000/v1");
    await userEvent.click(screen.getByRole("button", { name: /probe/i }));

    await screen.findByText("vLLM");

    const nameInput = screen.getByPlaceholderText(/name/i);
    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, "Spark vLLM");

    await userEvent.click(screen.getByRole("button", { name: /add runtime/i }));

    await waitFor(() =>
      expect(createSpy).toHaveBeenCalledWith({
        slug: "spark-vllm",
        display_name: "Spark vLLM",
        runtime_type: "vllm_docker",
        endpoint: "http://192.0.2.10:8000/v1",
        model_identifier: "m1",
        enabled: true,
      })
    );
  });

  it("normalizes a URL ending in /v1/ to a single /v1 suffix (no double-append)", async () => {
    const probeResult: ProbeEndpointResult = {
      reachable: true,
      models: ["m1"],
      detected_type: "vllm_docker",
      suggested_model: "m1",
      error: null,
    };
    vi.spyOn(api.runtimes, "probeEndpoint").mockResolvedValue(probeResult);
    const createSpy = vi
      .spyOn(api.runtimes.db, "create")
      .mockResolvedValue({ id: "rt-1" } as Runtime);

    renderWithQuery(<AddRuntimeModal open={true} onClose={vi.fn()} />);

    await userEvent.type(screen.getByPlaceholderText(/http/i), "http://192.0.2.10:8000/v1/");
    await userEvent.click(screen.getByRole("button", { name: /probe/i }));

    await screen.findByText("vLLM");

    const nameInput = screen.getByPlaceholderText(/name/i);
    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, "Spark vLLM");

    await userEvent.click(screen.getByRole("button", { name: /add runtime/i }));

    await waitFor(() =>
      expect(createSpy).toHaveBeenCalledWith(
        expect.objectContaining({ endpoint: "http://192.0.2.10:8000/v1" })
      )
    );
  });

  it("disables the submit button when the name slugifies to an empty string", async () => {
    const probeResult: ProbeEndpointResult = {
      reachable: true,
      models: ["m1"],
      detected_type: "vllm_docker",
      suggested_model: "m1",
      error: null,
    };
    vi.spyOn(api.runtimes, "probeEndpoint").mockResolvedValue(probeResult);

    renderWithQuery(<AddRuntimeModal open={true} onClose={vi.fn()} />);

    await userEvent.type(screen.getByPlaceholderText(/http/i), "http://192.0.2.10:8000/v1");
    await userEvent.click(screen.getByRole("button", { name: /probe/i }));

    await screen.findByText("vLLM");

    const nameInput = screen.getByPlaceholderText(/name/i);
    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, "###");

    expect(screen.getByRole("button", { name: /add runtime/i })).toBeDisabled();
  });
});
