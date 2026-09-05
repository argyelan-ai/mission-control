import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RuntimeDetailPanel } from "../RuntimeDetailPanel";
import { api } from "@/lib/api";
import type { Runtime } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function makeRuntime(over: Partial<Runtime>): Runtime {
  return {
    id: over.slug ?? "rt", slug: "rt", display_name: "RT",
    runtime_type: "vllm_docker", provider: "vllm",
    endpoint: "http://192.0.2.10:8001/v1", healthcheck_path: "/health",
    container_name: null, role_tags: [], supports_tools: true,
    supports_reasoning: false, supports_streaming: true,
    preferred_context_len: 8192, max_context_len: 32768,
    gpu_profile: "default", memory_notes: "", startup_notes: "",
    ui_order: 0, enabled: true, state: "ready",
    ...over,
  };
}

describe("RuntimeDetailPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({
      runtime_slug: "rt", count: 0, agents: [],
    });
  });

  it("cloud runtime: no Start/Stop, but model editor present", async () => {
    renderWithQuery(<RuntimeDetailPanel open runtime={makeRuntime({ runtime_type: "cloud", model_identifier: "claude-opus-5" })} onClose={() => {}} />);
    expect(screen.queryByTitle("Start")).not.toBeInTheDocument();
    expect(screen.getByText("claude-opus-5")).toBeInTheDocument();
    expect(screen.getByTitle("Edit model")).toBeInTheDocument();
  });

  it("vllm runtime: Start enabled when stopped, calls api.runtimes.start", async () => {
    const start = vi.spyOn(api.runtimes, "start").mockResolvedValue({ ok: true, message: "ok" });
    renderWithQuery(<RuntimeDetailPanel open runtime={makeRuntime({ runtime_type: "vllm_docker", state: "stopped" })} onClose={() => {}} />);
    const btn = screen.getByTitle("Start");
    expect(btn).toBeEnabled();
    btn.click();
    await waitFor(() => expect(start).toHaveBeenCalledWith("rt", undefined));
  });

  // Rezept-Umschalter (Vertrag 02.09.2026): dieselbe Quelle wie die Kachel.
  it("host-bound runtime: shows the box recipe switcher fed by GET /hosts/{id}/recipes", async () => {
    const recipes = vi.spyOn(api.hosts, "recipes").mockResolvedValue([{
      slug: "recipe-x", display_name: "Recipe X", engine: "lmstudio", topology: { nodes: 1 }, port: 1234,
      instance_runtime_id: "rt", running: true, startable: true, fit: "solo", reason: null,
      busy_hosts: [], candidate_workers: [],
    }]);
    renderWithQuery(
      <RuntimeDetailPanel
        open
        runtime={makeRuntime({ runtime_type: "lmstudio", host: { id: "box-a", slug: "box-a", display_name: "Box A" } })}
        onClose={() => {}}
      />,
    );
    const trigger = await screen.findByTestId("recipe-dropdown-trigger");
    await waitFor(() => expect(trigger).toHaveTextContent("Recipe X"));
    expect(recipes).toHaveBeenCalledWith("box-a");
  });

  it("host-bound runtime without recipes: no switcher, no phantom control", async () => {
    const recipes = vi.spyOn(api.hosts, "recipes").mockResolvedValue([]);
    renderWithQuery(
      <RuntimeDetailPanel
        open
        runtime={makeRuntime({ runtime_type: "vllm_docker", host: { id: "box-a", slug: "box-a", display_name: "Box A" } })}
        onClose={() => {}}
      />,
    );
    await waitFor(() => expect(recipes).toHaveBeenCalledWith("box-a"));
    await waitFor(() => expect(screen.queryByTestId("recipe-dropdown-trigger")).not.toBeInTheDocument());
  });

  it("renders nothing when runtime is null", () => {
    const { container } = renderWithQuery(<RuntimeDetailPanel open runtime={null} onClose={() => {}} />);
    expect(container.querySelector('[role="dialog"]')).toBeNull();
  });

  it("resets internal state (mutation feedback) when the panel switches to a different runtime", async () => {
    vi.spyOn(api.runtimes, "start").mockResolvedValue({ ok: true, message: "Started ok" });
    const rtA = makeRuntime({ slug: "rt-a", display_name: "Runtime A", runtime_type: "vllm_docker", state: "stopped" });
    const rtB = makeRuntime({ slug: "rt-b", display_name: "Runtime B", runtime_type: "vllm_docker", state: "stopped" });
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });

    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <RuntimeDetailPanel open runtime={rtA} onClose={() => {}} />
      </QueryClientProvider>
    );

    screen.getByTitle("Start").click();
    await waitFor(() => expect(screen.getByText("Started ok")).toBeInTheDocument());

    // Same panel instance, different runtime — without `key={runtime.id}` the
    // body would keep its old actionMsg/settingsOpen/storedCtx state.
    rerender(
      <QueryClientProvider client={qc}>
        <RuntimeDetailPanel open runtime={rtB} onClose={() => {}} />
      </QueryClientProvider>
    );

    expect(screen.queryByText("Started ok")).not.toBeInTheDocument();
  });

  it("shows a Name-drift chip when display_name_drift is non-empty, even without a live prop", async () => {
    renderWithQuery(
      <RuntimeDetailPanel
        open
        runtime={makeRuntime({
          runtime_type: "cloud",
          model_identifier: "claude-opus-5",
          display_name: "Claude Opus 4.7",
          display_name_drift: ["4.7"],
        })}
        onClose={() => {}}
      />
    );
    expect(await screen.findByText("Name")).toBeInTheDocument();
  });

  it("stays quiet when display_name_drift is empty", async () => {
    renderWithQuery(
      <RuntimeDetailPanel
        open
        runtime={makeRuntime({ runtime_type: "cloud", model_identifier: "claude-opus-5", display_name_drift: [] })}
        onClose={() => {}}
      />
    );
    await screen.findByText("claude-opus-5");
    expect(screen.queryByText("Name")).not.toBeInTheDocument();
  });

  // ── Slot-Runtime (ADR-078) ──────────────────────────────────────────────
  describe("Slot-Zeile", () => {
    const slot = makeRuntime({
      slug: "box-a-slot",
      display_name: "BOX-A :8000 (aktuell: recipe-x)",
      runtime_type: "openai_compatible",
      endpoint: "http://192.0.2.10:8000/v1",
      model_identifier: "recipe-x",
      is_slot: true,
      autostart_supported: false,
      host: { id: "box-a", slug: "box-a", display_name: "BOX-A" },
    });

    it("zeigt Modell, Endpunkt und den Hinweis — aber keine Knöpfe", async () => {
      const recipes = vi.spyOn(api.hosts, "recipes").mockResolvedValue([]);
      renderWithQuery(<RuntimeDetailPanel open runtime={slot} onClose={() => {}} />);

      // Modell: nur Anzeige, kein Stift (die Zeile folgt der Engine).
      expect(await screen.findByText("recipe-x")).toBeInTheDocument();
      expect(screen.queryByTitle("Edit model")).not.toBeInTheDocument();
      // Endpunkt — was ein Agent tatsächlich anspricht.
      expect(screen.getByText("http://192.0.2.10:8000/v1")).toBeInTheDocument();
      expect(
        screen.getByText(
          "This row is never started or stopped — it only shows what the box answers with."
        )
      ).toBeInTheDocument();
      // Keine Steuerung, kein Rezept-Umschalter, kein Autostart.
      expect(screen.queryByTitle("Start")).not.toBeInTheDocument();
      expect(screen.queryByTitle("Stop")).not.toBeInTheDocument();
      expect(screen.queryByText("Automation")).not.toBeInTheDocument();
      await waitFor(() => expect(recipes).not.toHaveBeenCalled());
    });

    it("listet die gebundenen Agenten", async () => {
      vi.spyOn(api.hosts, "recipes").mockResolvedValue([]);
      vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({
        runtime_slug: "box-a-slot",
        count: 1,
        agents: [{ id: "a1", name: "agent-one", agent_runtime: "cli-bridge" }],
      });
      renderWithQuery(<RuntimeDetailPanel open runtime={slot} onClose={() => {}} />);
      expect(await screen.findByText("agent-one")).toBeInTheDocument();
    });
  });
});
