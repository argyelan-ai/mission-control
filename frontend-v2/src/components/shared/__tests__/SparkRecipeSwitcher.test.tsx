/**
 * SparkRecipeSwitcher — ADR-059 solo-capability UI.
 *
 * Coverage:
 *   1. solo-capable recipes render clickable, no warning badge
 *   2. non-solo-capable recipes (tp/nodes exceed the host) render disabled
 *      with a tp/nodes hint and are NOT selectable
 *   3. clicking a disabled recipe never opens the confirm/switch UI
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SparkRecipeSwitcher } from "../SparkRecipeSwitcher";
import { api } from "@/lib/api";
import type { SparkrunRecipe } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const soloRecipe: SparkrunRecipe = {
  name: "@official/qwen3.6-35b-a3b-fp8-vllm",
  model: "Qwen/Qwen3.6-35B-A3B-FP8",
  registry: "official",
  tp: 1,
  nodes: 1,
  solo_capable: true,
};

const clusterRecipe: SparkrunRecipe = {
  name: "@eugr/qwen3.6-35b-a3b-fp8",
  model: "Qwen/Qwen3.6-35B-A3B-FP8",
  registry: "eugr",
  tp: 2,
  nodes: 1,
  solo_capable: false,
};

describe("SparkRecipeSwitcher", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("marks a non-solo-capable recipe as disabled with a tp/nodes hint", async () => {
    vi.spyOn(api.runtimes.sparkrun, "currentRecipe").mockResolvedValue({
      slug: "qwen-general",
      current_recipe: soloRecipe.name,
      sparkrun_managed: true,
    });
    vi.spyOn(api.runtimes.sparkrun, "listRecipes").mockResolvedValue({
      recipes: [soloRecipe, clusterRecipe],
    });

    const user = userEvent.setup();
    renderWithQuery(<SparkRecipeSwitcher runtimeId="qwen-general" />);

    await waitFor(() =>
      expect(screen.getByTitle(/active recipe/i)).toBeInTheDocument(),
    );
    await user.click(screen.getByTitle(/active recipe/i));

    await waitFor(() => expect(screen.getByText(clusterRecipe.name)).toBeInTheDocument());

    // Disabled recipe shows its tp/nodes requirement (badge) and a rejection
    // hint below it — both render the same "tp=2, nodes=1" text.
    expect(screen.getAllByText(/tp=2, nodes=1/).length).toBeGreaterThan(0);
    expect(screen.getByText(/nicht solo-startbar/i)).toBeInTheDocument();

    // Clicking the disabled entry must NOT reveal the confirm/switch button.
    await user.click(screen.getByText(clusterRecipe.name));
    expect(screen.queryByText(/confirm switch/i)).not.toBeInTheDocument();
  });

  it("allows selecting a solo-capable recipe", async () => {
    vi.spyOn(api.runtimes.sparkrun, "currentRecipe").mockResolvedValue({
      slug: "qwen-general",
      current_recipe: clusterRecipe.name,
      sparkrun_managed: true,
    });
    vi.spyOn(api.runtimes.sparkrun, "listRecipes").mockResolvedValue({
      recipes: [soloRecipe, clusterRecipe],
    });

    const user = userEvent.setup();
    renderWithQuery(<SparkRecipeSwitcher runtimeId="qwen-general" />);

    await waitFor(() =>
      expect(screen.getByTitle(/active recipe/i)).toBeInTheDocument(),
    );
    await user.click(screen.getByTitle(/active recipe/i));

    await waitFor(() => expect(screen.getByText(soloRecipe.name)).toBeInTheDocument());
    await user.click(screen.getByText(soloRecipe.name));

    expect(await screen.findByText(/confirm switch/i)).toBeInTheDocument();
  });
});
