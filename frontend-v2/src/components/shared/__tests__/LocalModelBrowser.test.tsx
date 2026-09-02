/**
 * LocalModelBrowser — vitest.
 *
 * Coverage:
 *   1. Liste rendert (Name + Modell-ID + Engine-Chip)
 *   2. „neu"-Badge exakt am 7-Tage-Fenster (frisch ja, 8 Tage nein)
 *   3. Passt-Check: min_vram_gb über Spark-Klasse → Warn-Chip, darunter nicht
 *   4. „Registry aktualisieren" ruft den Endpoint + zeigt das Ergebnis inkl. Gründe
 *   5. Deploy-Dialog: hosts.startRecipe(hostId, slug) mit korrekten Args
 *   6. Ohne Startbefehl → Deploy-Button disabled
 *   7. Ausgeblendete zeigen → PATCH-Pfad + enabled-Filter am Backend
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { LocalModelBrowser } from "../LocalModelBrowser";
import { api } from "@/lib/api";
import type {
  Host,
  LocalRecipe,
  LocalRegistryResponse,
} from "@/lib/types";

const daysAgo = (n: number) =>
  new Date(Date.now() - n * 24 * 60 * 60 * 1000).toISOString();

const mkRecipe = (overrides: Partial<LocalRecipe> = {}): LocalRecipe => ({
  slug: "qwen-general",
  display_name: "Qwen General",
  description: "Allrounder auf der Spark.",
  engine: "vllm_docker",
  model_identifier: "Qwen/Qwen3-32B",
  quant: "nvfp4",
  est_weights_gb: 21,
  min_vram_gb: 40,
  context_len: 131072,
  arch: "arm64",
  gb10_validated: true,
  recipe_ref: null,
  // Startbefehl vorhanden → über den Rezept-Start je Box deploybar.
  launch_template: "echo start",
  install_template: null,
  stop_template: null,
  process_name: null,
  author: null,
  author_url: null,
  source_registry: "seed",
  source_url: null,
  env: null,
  tags: ["coding"],
  notes: null,
  enabled: true,
  first_seen_at: daysAgo(30),
  updated_at: daysAgo(1),
  running: false,
  ...overrides,
});

const mkList = (recipes: LocalRecipe[]): LocalRegistryResponse => ({
  recipes,
  total: recipes.length,
  sources: [],
});

const mkHost = (overrides: Partial<Host> = {}): Host => ({
  id: "box-a",
  slug: "box-a",
  display_name: "Box A",
  kind: "ssh",
  ssh_host: null,
  ssh_user: null,
  ssh_key_path: null,
  ssh_credential_id: null, role: null, fabric_ip: null,
  control_url: null,
  wol_mac_address: null,
  power_managed: false,
  notes: null,
  enabled: true,
  ui_order: 0,
  created_at: "",
  updated_at: "",
  ...overrides,
});

function renderBrowser() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <LocalModelBrowser />
    </QueryClientProvider>,
  );
}

/** Standard-Mocks: eine Box, sonst pro Test überschrieben. */
function mockBackend(recipes: LocalRecipe[], hosts: Host[] = [mkHost()]) {
  const list = vi
    .spyOn(api.localRegistry, "list")
    .mockResolvedValue(mkList(recipes));
  vi.spyOn(api.hosts, "list").mockResolvedValue(hosts);
  return list;
}

describe("LocalModelBrowser", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the registry entries", async () => {
    mockBackend([
      mkRecipe(),
      mkRecipe({
        slug: "llama-cpp-gemma",
        display_name: "Gemma llama.cpp",
        engine: "llamacpp_docker",
        model_identifier: "google/gemma-3-27b",
        recipe_ref: null,
      }),
    ]);
    renderBrowser();

    await waitFor(() =>
      expect(screen.getByText("Qwen General")).toBeInTheDocument(),
    );
    expect(screen.getByText("Qwen/Qwen3-32B")).toBeInTheDocument();
    expect(screen.getByText("Gemma llama.cpp")).toBeInTheDocument();
    const cards = screen.getAllByTestId("local-recipe-card");
    expect(cards).toHaveLength(2);
    // Engine-Chip auf der Karte (der gleiche Text steht auch im Filter-Select).
    expect(within(cards[0]).getByText("vllm_docker")).toBeInTheDocument();
    expect(within(cards[1]).getByText("llamacpp_docker")).toBeInTheDocument();
  });

  it("marks entries seen within 7 days as new and leaves older ones quiet", async () => {
    mockBackend([
      mkRecipe({ slug: "fresh", display_name: "Fresh One", first_seen_at: daysAgo(2) }),
      mkRecipe({ slug: "stale", display_name: "Stale One", first_seen_at: daysAgo(8) }),
    ]);
    renderBrowser();

    await waitFor(() => expect(screen.getByText("Fresh One")).toBeInTheDocument());

    const cards = screen.getAllByTestId("local-recipe-card");
    const fresh = cards.find((c) => c.dataset.slug === "fresh")!;
    const stale = cards.find((c) => c.dataset.slug === "stale")!;

    expect(within(fresh).getByTestId("local-registry-new-badge")).toHaveTextContent(
      "new",
    );
    expect(
      within(stale).queryByTestId("local-registry-new-badge"),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("local-registry-new-count")).toHaveTextContent("1 new");
  });

  it("warns when the recipe needs more VRAM than a Spark-class box has", async () => {
    mockBackend([
      mkRecipe({ slug: "huge", display_name: "Huge One", min_vram_gb: 240 }),
      mkRecipe({ slug: "fits", display_name: "Fits One", min_vram_gb: 80 }),
    ]);
    renderBrowser();

    await waitFor(() => expect(screen.getByText("Huge One")).toBeInTheDocument());

    const cards = screen.getAllByTestId("local-recipe-card");
    const huge = cards.find((c) => c.dataset.slug === "huge")!;
    const fits = cards.find((c) => c.dataset.slug === "fits")!;

    const warn = within(huge).getByTestId("local-registry-fit-warning");
    expect(warn).toHaveTextContent(/may not fit/);
    expect(warn).toHaveAttribute("title", expect.stringContaining("240"));
    expect(
      within(fits).queryByTestId("local-registry-fit-warning"),
    ).not.toBeInTheDocument();

    // Warnung, kein Block — der Deploy bleibt anklickbar.
    expect(within(huge).getByTestId("local-registry-deploy")).toBeEnabled();
  });

  it("refresh button calls the endpoint and reports what it did", async () => {
    mockBackend([mkRecipe()]);
    const refresh = vi.spyOn(api.localRegistry, "refresh").mockResolvedValue({
      fetched: 2,
      added: 3,
      updated: 1,
      failed: 1,
      reasons: ["https://example.invalid/recipes.json: HTTP 404"],
      notified: ["new-model"],
    });
    renderBrowser();

    await userEvent.click(
      await screen.findByRole("button", { name: /Refresh registry/ }),
    );

    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
    const result = await screen.findByTestId("local-registry-refresh-result");
    expect(result).toHaveTextContent("3 added");
    expect(result).toHaveTextContent("1 updated");
    expect(result).toHaveTextContent("1 sources failed");

    // Gründe sind eingeklappt, bis der Operator sie öffnet.
    expect(
      screen.queryByTestId("local-registry-refresh-reasons"),
    ).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Show reasons \(1\)/ }));
    expect(
      await screen.findByTestId("local-registry-refresh-reasons"),
    ).toHaveTextContent("HTTP 404");
  });

  it("deploys a recipe through the box recipe start with host id and slug", async () => {
    mockBackend([mkRecipe({ slug: "recipe-x", display_name: "Recipe X" })], [
      mkHost({ id: "box-a", display_name: "Box A" }),
      mkHost({ id: "box-b", slug: "box-b", display_name: "Box B", enabled: false }),
    ]);
    const startSpy = vi
      .spyOn(api.hosts, "startRecipe")
      .mockResolvedValue({ ok: true, message: "started" });

    renderBrowser();

    await userEvent.click(await screen.findByTestId("local-registry-deploy"));
    const dialog = await screen.findByRole("dialog");

    // Ziel ist eine Box — abgeschaltete Boxen tauchen nicht auf.
    const select = within(dialog).getByLabelText("Target box");
    expect(within(select).getByRole("option", { name: "Box A" })).toBeInTheDocument();
    expect(
      within(select).queryByRole("option", { name: "Box B" }),
    ).not.toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole("button", { name: /^Deploy$/ }));

    await waitFor(() =>
      expect(startSpy).toHaveBeenCalledWith("box-a", "recipe-x"),
    );
  });

  it("disables deploy for entries that bring no way to install or launch themselves", async () => {
    mockBackend([
      mkRecipe({
        slug: "gemma-cpp",
        display_name: "Gemma llama.cpp",
        engine: "llamacpp_docker",
        launch_template: null,
      }),
      mkRecipe({
        slug: "recipe-x",
        display_name: "Recipe ohne Startbefehl",
        launch_template: null,
      }),
    ]);
    const startSpy = vi.spyOn(api.hosts, "startRecipe");
    renderBrowser();

    await waitFor(() =>
      expect(screen.getAllByTestId("local-recipe-card")).toHaveLength(2),
    );

    for (const btn of screen.getAllByTestId("local-registry-deploy")) {
      expect(btn).toBeDisabled();
      expect(btn).toHaveAttribute(
        "title",
        expect.stringContaining("neither an install nor a launch template"),
      );
      await userEvent.click(btn);
    }

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(startSpy).not.toHaveBeenCalled();
  });

  it("hides an entry via PATCH and loads hidden ones only on demand", async () => {
    const list = mockBackend([mkRecipe()]);
    const patch = vi
      .spyOn(api.localRegistry, "setEnabled")
      .mockResolvedValue(mkRecipe({ enabled: false }));
    renderBrowser();

    // Default: das Backend liefert nur sichtbare Einträge.
    await waitFor(() =>
      expect(list).toHaveBeenCalledWith({ enabled: true }),
    );

    // "Hide" is a secondary action: open the row overflow menu first.
    await userEvent.click(await screen.findByTestId("recipe-more-qwen-general"));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Hide entry" }));
    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith("qwen-general", false),
    );

    // Toggle „Ausgeblendete zeigen" hebt den Backend-Filter auf.
    await userEvent.click(screen.getByRole("checkbox", { name: /Show hidden/ }));
    await waitFor(() => expect(list).toHaveBeenCalledWith(undefined));
  });

  it("renders an error state when the registry cannot be loaded", async () => {
    vi.spyOn(api.localRegistry, "list").mockRejectedValue(new Error("API 500: boom"));
    vi.spyOn(api.hosts, "list").mockResolvedValue([]);
    renderBrowser();

    await waitFor(() =>
      expect(screen.getByText(/could not be loaded/)).toBeInTheDocument(),
    );
  });
});
