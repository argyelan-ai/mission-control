import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";

vi.mock("@/lib/notify", () => ({
  notify: { success: vi.fn(), error: vi.fn() },
}));

import { CreateAgentModal } from "../page";

const RUNTIMES = {
  runtimes: [
    {
      id: "rt-1",
      slug: "lmstudio-local",
      display_name: "LM Studio",
      runtime_type: "lmstudio",
      model_identifier: "qwen3-coder",
      enabled: true,
      provider: "local",
      endpoint: "http://example.com/v1",
      healthcheck_path: "/health",
      container_name: null,
    },
  ],
};

function renderModal() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <CreateAgentModal
        boards={[]}
        defaultBoardId={null}
        onClose={() => undefined}
        onCreated={() => undefined}
      />
    </QueryClientProvider>
  );
}

describe("CreateAgentModal — one-click create (Day-2 basics)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.runtimes, "list").mockResolvedValue(RUNTIMES as never);
  });

  it("shows the LLM runtime dropdown for cli-bridge agents", async () => {
    vi.spyOn(api.cliBridge, "health").mockResolvedValue({
      reachable: true,
      bridge_url: "http://host.docker.internal:18792",
    });
    renderModal();
    expect(await screen.findByText("LLM Runtime")).toBeInTheDocument();
    expect(
      await screen.findByText(/LM Studio · lmstudio · qwen3-coder/)
    ).toBeInTheDocument();
    // Reassures instead of dead-ending: provisioning runs by itself.
    expect(
      screen.getByText(/Provisions automatically after create/)
    ).toBeInTheDocument();
  });

  it("warns with a start command when the bridge is down", async () => {
    vi.spyOn(api.cliBridge, "health").mockResolvedValue({
      reachable: false,
      bridge_url: "http://host.docker.internal:18792",
    });
    renderModal();
    expect(
      await screen.findByText(/cli-bridge helper not reachable/)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/python3 scripts\/cli-bridge\.py/)
    ).toBeInTheDocument();
  });

  it("hides the LLM runtime section for manual agents", async () => {
    vi.spyOn(api.cliBridge, "health").mockResolvedValue({
      reachable: true,
      bridge_url: "http://host.docker.internal:18792",
    });
    renderModal();
    await screen.findByText("LLM Runtime");
    await userEvent.selectOptions(screen.getByDisplayValue("CLI Bridge (lokal)"), "manual");
    await waitFor(() =>
      expect(screen.queryByText("LLM Runtime")).not.toBeInTheDocument()
    );
  });

  it("sends the selected runtime_id on create", async () => {
    vi.spyOn(api.cliBridge, "health").mockResolvedValue({
      reachable: true,
      bridge_url: "http://host.docker.internal:18792",
    });
    const create = vi
      .spyOn(api.agents, "create")
      .mockResolvedValue({ id: "a-1", token: "t" } as never);

    renderModal();
    await userEvent.type(screen.getByPlaceholderText("z.B. Cody"), "Testo");
    const llmSelect = (await screen.findByText("LLM Runtime"))
      .parentElement!.querySelector("select")!;
    await userEvent.selectOptions(llmSelect, "rt-1");
    await userEvent.click(screen.getByText("Erstellen"));

    await waitFor(() =>
      expect(create).toHaveBeenCalledWith(
        expect.objectContaining({
          name: "Testo",
          agent_runtime: "cli-bridge",
          runtime_id: "rt-1",
        })
      )
    );
  });
});
