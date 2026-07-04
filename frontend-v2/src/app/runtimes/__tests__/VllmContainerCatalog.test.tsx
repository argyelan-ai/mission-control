import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { VllmContainerCatalog } from "../VllmContainerCatalog";
import { api } from "@/lib/api";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const REGISTERED_CONTAINER = {
  container_name: "mc-already-vllm",
  image: "vllm/vllm-openai:v0.6.3",
  endpoint: "http://192.0.2.10:8001/v1",
  state: "running",
  is_registered: true,
  registered_id: "already-vllm",
};

const UNREGISTERED_CONTAINER = {
  container_name: "mc-qwen36-vllm",
  image: "vllm/vllm-openai:v0.6.3",
  endpoint: "http://192.0.2.10:8003/v1",
  state: "running",
  is_registered: false,
  registered_id: null,
};

describe("VllmContainerCatalog", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders nothing when all containers are already registered", async () => {
    vi.spyOn(api.runtimes.vllm, "discover").mockResolvedValue({
      containers: [REGISTERED_CONTAINER],
    });
    const { container } = renderWithQuery(<VllmContainerCatalog />);
    await waitFor(() =>
      expect(api.runtimes.vllm.discover).toHaveBeenCalled()
    );
    expect(container.querySelector("button")).toBeNull();
    expect(screen.queryByText(/Discovered/i)).toBeNull();
  });

  it("clicking + opens the AddVllmModal", async () => {
    vi.spyOn(api.runtimes.vllm, "discover").mockResolvedValue({
      containers: [UNREGISTERED_CONTAINER],
    });
    renderWithQuery(<VllmContainerCatalog />);
    const addBtn = await screen.findByRole("button", { name: /add/i });
    await userEvent.click(addBtn);
    expect(screen.getByText(/Add vLLM Runtime/i)).toBeInTheDocument();
    expect(screen.getByDisplayValue("mc-qwen36-vllm")).toBeInTheDocument();
    expect(screen.getByDisplayValue("http://192.0.2.10:8003/v1")).toBeInTheDocument();
  });

  it("submitting the modal calls api.runtimes.vllm.add with correct body", async () => {
    vi.spyOn(api.runtimes.vllm, "discover").mockResolvedValue({
      containers: [UNREGISTERED_CONTAINER],
    });
    const addSpy = vi
      .spyOn(api.runtimes.vllm, "add")
      .mockResolvedValue({} as never);

    renderWithQuery(<VllmContainerCatalog />);
    const cardBtn = await screen.findByRole("button", { name: /add/i });
    await userEvent.click(cardBtn);

    await userEvent.click(screen.getByRole("button", { name: /^coder$/i }));
    const submitBtn = screen.getAllByRole("button", { name: /add/i }).at(-1)!;
    await userEvent.click(submitBtn);

    await waitFor(() => expect(addSpy).toHaveBeenCalled());
    expect(addSpy).toHaveBeenCalledWith({
      container_name: "mc-qwen36-vllm",
      display_name: "mc-qwen36-vllm",
      endpoint: "http://192.0.2.10:8003/v1",
      role_tags: ["coder"],
    });
  });
});
