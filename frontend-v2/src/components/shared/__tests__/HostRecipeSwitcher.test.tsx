/**
 * HostRecipeSwitcher — der eine Rezept-Umschalter je Box (Vertrag 02.09.2026).
 *
 * Abgedeckt:
 *   1. zwei Gruppen nach topology.nodes, Reihenfolge laufend → startbar → grau
 *   2. grau = startable:false mit reason als Satz IN der Zeile (kein Tooltip)
 *   3. laufendes Rezept markiert und nicht anklickbar
 *   4. belegte Boxen sichtbar (busy_hosts)
 *   5. Klick → Bestätigen → POST start mit host id + slug; „startet …" bis
 *      die Liste running meldet
 *   6. Fehler (409) bleibt als Satz stehen, bis er weggeklickt wird
 *   7. Zweibox-Rezept gesperrt mit dem Grund vom Backend
 *   8. leere Liste: Auslöser bleibt, Liste sagt es (Kachel) / versteckt
 *      sich (Panel, hideWhenEmpty)
 *
 * Sabotage-Probe (manuell gefahren): `disabled={r.running || !r.startable}`
 * auf `disabled={false}` gesetzt → Tests 2, 3 und 7 fallen.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HostRecipeSwitcher, groupHostRecipes } from "../HostRecipeSwitcher";
import { api } from "@/lib/api";
import type { HostRecipe } from "@/lib/types";

function makeRecipe(over: Partial<HostRecipe> & { slug: string }): HostRecipe {
  return {
    display_name: over.slug, engine: "vllm_docker", topology: { nodes: 1 }, port: 8000,
    instance_runtime_id: null, running: false, startable: true, fit: "solo", reason: null,
    busy_hosts: [], candidate_workers: [],
    ...over,
  };
}

const FIXTURE: HostRecipe[] = [
  makeRecipe({ slug: "recipe-grey", display_name: "Recipe Grey", startable: false, fit: "none", reason: "Start command missing" }),
  makeRecipe({ slug: "recipe-y", display_name: "Recipe Y", port: 8888 }),
  makeRecipe({ slug: "recipe-x", display_name: "Recipe X", running: true, instance_runtime_id: "rt-1" }),
  makeRecipe({
    slug: "recipe-duo", display_name: "Recipe Duo", topology: { nodes: 2 }, fit: "duo",
    startable: false, reason: "Two-box start comes in phase 3",
    candidate_workers: [{ host_id: "box-b", slug: "box-b", role: "worker" }],
  }),
  makeRecipe({
    slug: "recipe-duo-busy", display_name: "Recipe Duo Busy", topology: { nodes: 2 }, fit: "none",
    startable: false, reason: "needs 2 boxes — no free second box", busy_hosts: ["box-b"],
  }),
];

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return { ...render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>), qc };
}

async function openList() {
  const trigger = await screen.findByTestId("recipe-dropdown-trigger");
  await act(async () => { trigger.click(); });
  return screen.findByTestId("recipe-dropdown-list");
}

describe("groupHostRecipes", () => {
  it("splits by topology.nodes and orders running → startable → grey within each group", () => {
    const { solo, duo } = groupHostRecipes(FIXTURE);
    expect(solo.map((r) => r.slug)).toEqual(["recipe-x", "recipe-y", "recipe-grey"]);
    expect(duo.map((r) => r.slug)).toEqual(["recipe-duo", "recipe-duo-busy"]);
  });
});

describe("HostRecipeSwitcher", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.hosts, "recipes").mockResolvedValue(FIXTURE);
  });

  it("renders both groups with their recipes in order", async () => {
    renderWithQuery(<HostRecipeSwitcher hostId="box-a" />);
    await openList();

    const solo = screen.getByTestId("recipe-group-solo");
    const duo = screen.getByTestId("recipe-group-duo");
    expect(solo).toHaveTextContent("This box only");
    expect(duo).toHaveTextContent("Both boxes");
    const soloSlugs = Array.from(solo.querySelectorAll('[role="option"]')).map((el) => el.getAttribute("data-testid"));
    expect(soloSlugs).toEqual(["recipe-option-recipe-x", "recipe-option-recipe-y", "recipe-option-recipe-grey"]);
    const duoSlugs = Array.from(duo.querySelectorAll('[role="option"]')).map((el) => el.getAttribute("data-testid"));
    expect(duoSlugs).toEqual(["recipe-option-recipe-duo", "recipe-option-recipe-duo-busy"]);
  });

  it("marks the running recipe, shows it on the trigger, and does not let it be started again", async () => {
    const start = vi.spyOn(api.hosts, "startRecipe");
    renderWithQuery(<HostRecipeSwitcher hostId="box-a" />);
    const trigger = await screen.findByTestId("recipe-dropdown-trigger");
    await waitFor(() => expect(trigger).toHaveTextContent("Recipe X"));
    await openList();

    const running = screen.getByTestId("recipe-option-recipe-x");
    expect(running).toHaveAttribute("aria-selected", "true");
    expect(running).toHaveTextContent("running");
    expect(running).toBeDisabled();
    running.click();
    expect(screen.queryByTestId("recipe-confirm")).not.toBeInTheDocument();
    expect(start).not.toHaveBeenCalled();
  });

  it("greys out a non-startable recipe and prints the backend reason inside the row", async () => {
    renderWithQuery(<HostRecipeSwitcher hostId="box-a" />);
    await openList();

    const grey = screen.getByTestId("recipe-option-recipe-grey");
    expect(grey).toBeDisabled();
    expect(grey).not.toHaveAttribute("title");
    expect(screen.getByTestId("recipe-reason-recipe-grey")).toHaveTextContent("Start command missing");
    // Ein startbares Rezept trägt keinen Grund.
    expect(screen.queryByTestId("recipe-reason-recipe-y")).not.toBeInTheDocument();
  });

  it("locks a two-box recipe with the reason the backend gave and shows which box is busy", async () => {
    const start = vi.spyOn(api.hosts, "startRecipe");
    renderWithQuery(<HostRecipeSwitcher hostId="box-a" />);
    await openList();

    const duo = screen.getByTestId("recipe-option-recipe-duo");
    expect(duo).toBeDisabled();
    expect(screen.getByTestId("recipe-reason-recipe-duo")).toHaveTextContent("Two-box start comes in phase 3");
    duo.click();
    expect(start).not.toHaveBeenCalled();

    expect(screen.getByTestId("recipe-busy-recipe-duo-busy")).toHaveTextContent("busy on: box-b");
  });

  it("select → confirm → POST start; shows starting… until the list reports running", async () => {
    const start = vi.spyOn(api.hosts, "startRecipe").mockResolvedValue({ ok: true });
    const recipes = vi.spyOn(api.hosts, "recipes").mockResolvedValue(FIXTURE);
    const { qc } = renderWithQuery(<HostRecipeSwitcher hostId="box-a" servingName="Recipe X" />);
    await openList();

    await act(async () => { screen.getByTestId("recipe-option-recipe-y").click(); });
    expect(start).not.toHaveBeenCalled();
    expect(screen.getByTestId("recipe-confirm")).toHaveTextContent("Recipe Y");

    await act(async () => { screen.getByTestId("recipe-confirm-start").click(); });
    await waitFor(() => expect(start).toHaveBeenCalledWith("box-a", "recipe-y"));

    // Der Befehl ist raus, die Quelle meldet Recipe Y aber noch nicht als
    // laufend → „startet …", und der Auslöser behauptet nichts.
    expect(screen.getByTestId("recipe-starting")).toHaveTextContent("starting Recipe Y …");
    expect(screen.queryByTestId("recipe-dropdown-trigger")).not.toBeInTheDocument();

    // Erst wenn die Liste umschlägt, endet der Zwischenzustand.
    recipes.mockResolvedValue(
      FIXTURE.map((r) => ({ ...r, running: r.slug === "recipe-y" })),
    );
    await act(async () => { await qc.invalidateQueries({ queryKey: ["hosts", "box-a", "recipes"] }); });
    const trigger = await screen.findByTestId("recipe-dropdown-trigger");
    expect(trigger).toHaveTextContent("Recipe Y");
    expect(screen.queryByTestId("recipe-starting")).not.toBeInTheDocument();
  });

  it("keeps a start error visible as a sentence until dismissed", async () => {
    vi.spyOn(api.hosts, "startRecipe").mockRejectedValue(
      new Error('API 409: {"detail":"Port 8888 on this box is busy"}'),
    );
    renderWithQuery(<HostRecipeSwitcher hostId="box-a" />);
    await openList();
    await act(async () => { screen.getByTestId("recipe-option-recipe-y").click(); });
    await act(async () => { screen.getByTestId("recipe-confirm-start").click(); });

    const err = await screen.findByTestId("recipe-start-error");
    // Der Satz aus `detail`, nicht das JSON drumherum.
    expect(err).toHaveTextContent("Start failed: Port 8888 on this box is busy");
    expect(err).not.toHaveTextContent("detail");
    // Kein Fake-Zustand nach dem Fehler: weder „startet …" noch „läuft".
    expect(screen.queryByTestId("recipe-starting")).not.toBeInTheDocument();
    expect(screen.getByTestId("recipe-dropdown-trigger")).toHaveTextContent("Recipe X");

    // Bleibt stehen, bis der Operator sie schliesst.
    await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
    expect(screen.getByTestId("recipe-start-error")).toBeInTheDocument();
    await act(async () => { screen.getByLabelText("Dismiss message").click(); });
    expect(screen.queryByTestId("recipe-start-error")).not.toBeInTheDocument();
  });

  it("with zero recipes keeps the trigger and says so in the list — unless hideWhenEmpty", async () => {
    vi.spyOn(api.hosts, "recipes").mockResolvedValue([]);
    const { unmount } = renderWithQuery(<HostRecipeSwitcher hostId="box-a" servingName="Engine X" />);
    const trigger = await screen.findByTestId("recipe-dropdown-trigger");
    expect(trigger).toHaveTextContent("Engine X");
    await openList();
    expect(screen.getByTestId("recipe-empty")).toHaveTextContent("No recipes for this box.");
    unmount();

    renderWithQuery(<HostRecipeSwitcher hostId="box-a" hideWhenEmpty />);
    await waitFor(() => expect(api.hosts.recipes).toHaveBeenCalled());
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByTestId("host-recipe-switcher")).not.toBeInTheDocument();
  });

  it("shows a load error as a sentence in the row", async () => {
    vi.spyOn(api.hosts, "recipes").mockRejectedValue(new Error("API 500: boom"));
    renderWithQuery(<HostRecipeSwitcher hostId="box-a" />);
    expect(await screen.findByTestId("recipe-load-error")).toHaveTextContent("Could not load recipes: API 500: boom");
  });
});
