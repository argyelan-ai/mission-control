/**
 * HostRecipeSwitcher — Vorflug-Hinweise (Rezept-Umschalter P4).
 *
 * Das Backend rechnet die Kapazität fertig aus (`capacity.warnings`); die
 * Oberfläche zeigt die Sätze, sie rechnet NICHTS nach. Abgedeckt:
 *   1. Warnungen stehen als Hinweistext unter dem Eintrag in der Liste
 *   2. ohne Warnungen gibt es keinen Hinweistext
 *   3. die Warnungen stehen auch in der Bestätigungszeile — dort, wo geklickt wird
 *   4. ein harter Verstoss läuft über `startable:false` + `reason` (wie bisher),
 *      die Warnungen daneben verschwinden dadurch nicht
 *   5. ein altes Backend ohne `capacity` bricht nichts
 *
 * Sabotage-Probe (gefahren 05.09.): `capacityWarnings` auf `[]` gesetzt →
 * 4 von 7 Tests fallen (Helfer, Liste, harter Verstoss, Bestätigungszeile).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HostRecipeSwitcher, capacityWarnings } from "../HostRecipeSwitcher";
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

const TIGHT = "Box 'box-a': nur 20 GB frei, Start kann am Speicher scheitern.";
const UNKNOWN = "Box 'box-b': Kapazität unbekannt (keine Telemetrie).";
const TOO_SMALL = "Box 'box-a' hat 60 GB, Rezept braucht 100 GB.";

const WARNED = makeRecipe({
  slug: "recipe-warned", display_name: "Recipe Warned",
  capacity: { ok: true, warnings: [TIGHT, UNKNOWN], boxes: [] },
});

const CLEAN = makeRecipe({
  slug: "recipe-clean", display_name: "Recipe Clean",
  capacity: { ok: true, warnings: [], boxes: [] },
});

const BLOCKED = makeRecipe({
  slug: "recipe-blocked", display_name: "Recipe Blocked",
  startable: false, reason: TOO_SMALL,
  capacity: { ok: false, warnings: [UNKNOWN], boxes: [] },
});

const LEGACY = makeRecipe({ slug: "recipe-legacy", display_name: "Recipe Legacy" });

const FIXTURE = [WARNED, CLEAN, BLOCKED, LEGACY];

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

async function openList() {
  const trigger = await screen.findByTestId("recipe-dropdown-trigger");
  await act(async () => { trigger.click(); });
  return screen.findByTestId("recipe-dropdown-list");
}

describe("capacityWarnings", () => {
  it("liefert die Sätze des Backends unverändert", () => {
    expect(capacityWarnings(WARNED)).toEqual([TIGHT, UNKNOWN]);
  });

  it("ist leer, wenn das Backend das Feld nicht schickt", () => {
    expect(capacityWarnings(LEGACY)).toEqual([]);
  });
});

describe("HostRecipeSwitcher — Vorflug-Hinweise", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.hosts, "recipes").mockResolvedValue(FIXTURE as never);
  });

  it("zeigt die Warnungen unter dem Eintrag", async () => {
    renderWithQuery(<HostRecipeSwitcher hostId="id-box-a" />);
    await openList();

    const hint = screen.getByTestId("recipe-capacity-recipe-warned");
    expect(hint.textContent).toContain(TIGHT);
    expect(hint.textContent).toContain(UNKNOWN);
  });

  it("zeigt keinen Hinweis ohne Warnungen — und keinen bei altem Backend", async () => {
    renderWithQuery(<HostRecipeSwitcher hostId="id-box-a" />);
    await openList();

    expect(screen.queryByTestId("recipe-capacity-recipe-clean")).toBeNull();
    expect(screen.queryByTestId("recipe-capacity-recipe-legacy")).toBeNull();
  });

  it("zeigt beim harten Verstoss den Grund UND die Warnung", async () => {
    renderWithQuery(<HostRecipeSwitcher hostId="id-box-a" />);
    await openList();

    expect(screen.getByTestId("recipe-reason-recipe-blocked").textContent).toBe(TOO_SMALL);
    expect(screen.getByTestId("recipe-capacity-recipe-blocked").textContent).toContain(UNKNOWN);
    expect(screen.getByTestId("recipe-option-recipe-blocked")).toBeDisabled();
  });

  it("wiederholt die Warnungen in der Bestätigungszeile", async () => {
    renderWithQuery(<HostRecipeSwitcher hostId="id-box-a" />);
    await openList();
    await act(async () => { screen.getByTestId("recipe-option-recipe-warned").click(); });

    const hint = await screen.findByTestId("recipe-confirm-capacity");
    expect(hint.textContent).toContain(TIGHT);
    expect(hint.textContent).toContain(UNKNOWN);
    // Gewarnt ist nicht gesperrt — der Knopf bleibt klickbar.
    expect(screen.getByTestId("recipe-confirm-start")).not.toBeDisabled();
  });

  it("zeigt in der Bestätigungszeile nichts, wenn es nichts zu warnen gibt", async () => {
    renderWithQuery(<HostRecipeSwitcher hostId="id-box-a" />);
    await openList();
    await act(async () => { screen.getByTestId("recipe-option-recipe-clean").click(); });

    await screen.findByTestId("recipe-confirm");
    expect(screen.queryByTestId("recipe-confirm-capacity")).toBeNull();
  });
});
