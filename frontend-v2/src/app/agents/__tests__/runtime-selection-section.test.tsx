/**
 * RuntimeSelectionSection — der Runtime-Picker der Agenten-Detailseite.
 *
 * Prüft die Bindungsregeln aus ADR-078 (Slot-Runtime), so wie sie ein
 * Bediener sieht:
 *   - Slot-Zeilen („Box-Adressen") stehen oben, mit dem Namen vom SERVER
 *   - für omp gibt es keinen „ohne Runtime"-Fallback (Bindung ist Pflicht,
 *     das Backend lehnt das Lösen mit 422 ab)
 *   - für claude bleibt der Fallback stehen
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RuntimeSelectionSection } from "../[id]/page";
import { api } from "@/lib/api";
import type { Agent, Runtime } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const mkAgent = (over: Partial<Agent> = {}): Agent =>
  ({
    id: "agent-1",
    name: "agent-one",
    agent_runtime: "cli-bridge",
    runtime_id: "slot-a",
    runtime_switchable: true,
    runtime_switch_blocked_reason: null,
    harness: "claude",
    ...over,
  }) as Agent;

const mkRuntime = (over: Partial<Runtime>): Runtime =>
  ({
    id: over.slug ?? "rt",
    slug: "rt",
    display_name: "RT",
    runtime_type: "vllm_docker",
    provider: "vllm",
    provider_label: null,
    endpoint: "http://192.0.2.10:8001/v1",
    healthcheck_path: "/health",
    container_name: null,
    role_tags: [],
    supports_tools: true,
    supports_reasoning: false,
    supports_streaming: true,
    preferred_context_len: 8192,
    max_context_len: 32768,
    gpu_profile: "default",
    memory_notes: "",
    startup_notes: "",
    ui_order: 0,
    enabled: true,
    state: "ready",
    ...over,
  }) as Runtime;

const slotA = mkRuntime({
  slug: "slot-a",
  display_name: "BOX-A :8000 (aktuell: recipe-x)",
  runtime_type: "openai_compatible",
  is_slot: true,
});
const recipeX = mkRuntime({
  slug: "recipe-x",
  display_name: "Recipe X",
  model_identifier: "recipe-x",
});

describe("RuntimeSelectionSection", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [recipeX, slotA] });
  });

  it("zeigt die Slot-Zeile ganz oben, mit dem Namen vom Server", async () => {
    renderWithQuery(<RuntimeSelectionSection agent={mkAgent()} agentId="agent-1" />);
    const select = await screen.findByRole("combobox");
    await waitFor(() =>
      expect(
        within(select).getByRole("option", { name: "BOX-A :8000 (aktuell: recipe-x)" }),
      ).toBeInTheDocument(),
    );
    // Nichts angehängt — das Modell steht schon im Servernamen.
    const names = within(select)
      .getAllByRole("option")
      .map((o) => o.textContent);
    expect(names).toContain("BOX-A :8000 (aktuell: recipe-x)");
    // Reihenfolge: Fallback, dann Slot, dann der Rest.
    expect(names.indexOf("BOX-A :8000 (aktuell: recipe-x)")).toBeLessThan(
      names.findIndex((n) => n?.startsWith("Recipe X")),
    );
  });

  it("blendet den Fallback für einen omp-Agenten aus (Bindung ist dort Pflicht)", async () => {
    renderWithQuery(
      <RuntimeSelectionSection agent={mkAgent({ harness: "omp" })} agentId="agent-1" />,
    );
    const select = await screen.findByRole("combobox");
    await waitFor(() =>
      expect(
        within(select).getByRole("option", { name: "BOX-A :8000 (aktuell: recipe-x)" }),
      ).toBeInTheDocument(),
    );
    expect(
      within(select).queryByRole("option", { name: "— Fallback (docker-compose env) —" }),
    ).not.toBeInTheDocument();
  });

  it("lässt den Fallback für einen claude-Agenten stehen", async () => {
    renderWithQuery(
      <RuntimeSelectionSection agent={mkAgent({ harness: "claude" })} agentId="agent-1" />,
    );
    const select = await screen.findByRole("combobox");
    await waitFor(() =>
      expect(
        within(select).getByRole("option", { name: "— Fallback (docker-compose env) —" }),
      ).toBeInTheDocument(),
    );
  });
});
