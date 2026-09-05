/**
 * HostRecipeSwitcher — Zweibox-Start (Rezept-Umschalter P3).
 *
 * Abgedeckt:
 *   1. mehrere Kandidaten → Auswahlfeld; die GEWÄHLTE Box geht als
 *      {"worker_host_id"} an das Backend (nicht die Vorauswahl)
 *   2. genau ein Kandidat → Name als Text, kein Auswahlfeld, trotzdem gesendet
 *   3. kein Kandidat → „Starten" gesperrt, Grund als Satz, kein POST
 *   4. env_ready:false → gesperrt mit dem Grund vom Backend, kein POST
 *   5. Erfolgsmeldung nennt beide Boxen (Kopf + die Box aus der Antwort)
 *   6. Solo-Start schickt weiterhin KEINEN Körper, invalidiert aber ["hosts"]
 *
 * Sabotage-Probe (manuell gefahren): `disabled={blocker != null}` am
 * Bestätigen-Knopf auf `disabled={false}` gesetzt → Tests 3 und 4 fallen;
 * `worker: workerHostId` durch `worker: null` ersetzt → Tests 1 und 2 fallen.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HostRecipeSwitcher, duoStartBlocker } from "../HostRecipeSwitcher";
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

const SOLO = makeRecipe({ slug: "recipe-x", display_name: "Recipe X" });

const DUO_TWO = makeRecipe({
  slug: "recipe-duo", display_name: "Recipe Duo", topology: { nodes: 2 }, fit: "duo",
  env_ready: true,
  candidate_workers: [
    { host_id: "id-box-b", slug: "box-b", role: "worker" },
    { host_id: "id-box-c", slug: "box-c", role: null },
  ],
});

const DUO_ONE = makeRecipe({
  slug: "recipe-duo-one", display_name: "Recipe Duo One", topology: { nodes: 2 }, fit: "duo",
  env_ready: true,
  candidate_workers: [{ host_id: "id-box-b", slug: "box-b", role: "worker" }],
});

const DUO_NO_WORKER = makeRecipe({
  slug: "recipe-duo-free", display_name: "Recipe Duo Free", topology: { nodes: 2 }, fit: "duo",
  env_ready: true, candidate_workers: [],
});

const DUO_NO_ENV = makeRecipe({
  slug: "recipe-duo-env", display_name: "Recipe Duo Env", topology: { nodes: 2 }, fit: "duo",
  env_ready: false,
  reason: "Recipe has no environment mapping (env_file/env_map) — add it to the catalog.",
  candidate_workers: [{ host_id: "id-box-b", slug: "box-b", role: "worker" }],
});

const FIXTURE = [SOLO, DUO_TWO, DUO_ONE, DUO_NO_WORKER, DUO_NO_ENV];

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return { ...render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>), qc };
}

async function pick(slug: string) {
  const trigger = await screen.findByTestId("recipe-dropdown-trigger");
  await act(async () => { trigger.click(); });
  await screen.findByTestId("recipe-dropdown-list");
  await act(async () => { screen.getByTestId(`recipe-option-${slug}`).click(); });
  return screen.getByTestId("recipe-confirm");
}

describe("duoStartBlocker", () => {
  it("blocks only two-box recipes, and says which of the two reasons applies", () => {
    expect(duoStartBlocker(SOLO)).toBeNull();
    expect(duoStartBlocker(DUO_TWO)).toBeNull();
    expect(duoStartBlocker(DUO_NO_WORKER)).toBe("no-worker");
    expect(duoStartBlocker(DUO_NO_ENV)).toBe("env");
    // env_ready fehlt (älteres Backend) → nicht blockieren, sonst wäre die
    // Anzeige strenger als das Backend.
    expect(duoStartBlocker({ ...DUO_TWO, env_ready: undefined })).toBeNull();
  });
});

describe("HostRecipeSwitcher — two-box start", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.hosts, "recipes").mockResolvedValue(FIXTURE);
  });

  it("lets the operator choose the worker box and sends exactly that one", async () => {
    const start = vi.spyOn(api.hosts, "startRecipe").mockResolvedValue({ ok: true });
    renderWithQuery(<HostRecipeSwitcher hostId="id-box-a" hostName="box-a" />);
    await pick("recipe-duo");

    const select = screen.getByTestId("recipe-worker-select") as HTMLSelectElement;
    // Vorauswahl = erster Kandidat, wie das Backend ohne Angabe wählen würde.
    expect(select.value).toBe("id-box-b");
    expect(Array.from(select.options).map((o) => o.textContent)).toEqual(["box-b", "box-c"]);

    await userEvent.selectOptions(select, "id-box-c");
    await act(async () => { screen.getByTestId("recipe-confirm-start").click(); });

    await waitFor(() =>
      expect(start).toHaveBeenCalledWith("id-box-a", "recipe-duo", { worker_host_id: "id-box-c" }),
    );
  });

  it("with a single candidate shows its name as text and still sends it", async () => {
    const start = vi.spyOn(api.hosts, "startRecipe").mockResolvedValue({ ok: true });
    renderWithQuery(<HostRecipeSwitcher hostId="id-box-a" hostName="box-a" />);
    await pick("recipe-duo-one");

    expect(screen.queryByTestId("recipe-worker-select")).not.toBeInTheDocument();
    expect(screen.getByTestId("recipe-worker-single")).toHaveTextContent("box-b");

    await act(async () => { screen.getByTestId("recipe-confirm-start").click(); });
    await waitFor(() =>
      expect(start).toHaveBeenCalledWith("id-box-a", "recipe-duo-one", { worker_host_id: "id-box-b" }),
    );
  });

  it("with no free second box the start stays locked and says why", async () => {
    const start = vi.spyOn(api.hosts, "startRecipe");
    renderWithQuery(<HostRecipeSwitcher hostId="id-box-a" hostName="box-a" />);
    await pick("recipe-duo-free");

    expect(screen.getByTestId("recipe-confirm-start")).toBeDisabled();
    expect(screen.queryByTestId("recipe-worker-choice")).not.toBeInTheDocument();
    expect(screen.getByTestId("recipe-worker-blocked")).toHaveTextContent(
      "No free second box — this recipe needs two boxes.",
    );
    await act(async () => { screen.getByTestId("recipe-confirm-start").click(); });
    expect(start).not.toHaveBeenCalled();
  });

  it("without an environment mapping the start stays locked with the backend's reason", async () => {
    const start = vi.spyOn(api.hosts, "startRecipe");
    renderWithQuery(<HostRecipeSwitcher hostId="id-box-a" hostName="box-a" />);
    const confirm = await pick("recipe-duo-env");

    expect(screen.getByTestId("recipe-confirm-start")).toBeDisabled();
    const blocked = screen.getByTestId("recipe-worker-blocked");
    expect(blocked).toHaveAttribute("data-blocker", "env");
    expect(blocked).toHaveTextContent("Recipe has no environment mapping");
    expect(confirm).toBeInTheDocument();
    await act(async () => { screen.getByTestId("recipe-confirm-start").click(); });
    expect(start).not.toHaveBeenCalled();
  });

  it("names both boxes while starting — the head and the worker the backend took", async () => {
    vi.spyOn(api.hosts, "startRecipe").mockResolvedValue({
      ok: true, worker_host_id: "id-box-c", worker_slug: "box-c", env_written: ["HEAD_IP", "WORKER_IP"],
    });
    renderWithQuery(<HostRecipeSwitcher hostId="id-box-a" hostName="box-a" />);
    await pick("recipe-duo");
    await act(async () => { screen.getByTestId("recipe-confirm-start").click(); });

    const starting = await screen.findByTestId("recipe-starting");
    await waitFor(() =>
      expect(starting).toHaveTextContent("starting Recipe Duo on box-a + box-c …"),
    );
  });

  it("a solo start sends no body and still refreshes the host list", async () => {
    const start = vi.spyOn(api.hosts, "startRecipe").mockResolvedValue({ ok: true });
    const { qc } = renderWithQuery(<HostRecipeSwitcher hostId="id-box-a" hostName="box-a" />);
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    await pick("recipe-x");

    expect(screen.queryByTestId("recipe-worker-choice")).not.toBeInTheDocument();
    await act(async () => { screen.getByTestId("recipe-confirm-start").click(); });

    await waitFor(() => expect(start).toHaveBeenCalledWith("id-box-a", "recipe-x"));
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ["hosts"] }),
    );
  });
});
