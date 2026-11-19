/**
 * Round-5 re-check (L1): a sleeping box offers "Wake" from its HOST flag.
 *
 * The case: a box whose host row says power_managed, while none of its
 * runtime rows carries the flag. The page put it in a FreeBox: "ASLEEP"
 * next to an armed "Start model", no way to wake it (waking existed only per
 * runtime). Now:
 * - the host flag alone makes it the sleeping card, and "Wake" goes to
 *   POST /hosts/{id}/wake (no runtime needed);
 * - while asleep or still being checked there is no "Start model";
 * - the cockpit of a sleeping box says it sleeps (no "collecting…", no
 *   "Restart") and offers "Wake";
 * - "Recipes fit" counts only recipes that fit this one box.
 *
 * Nothing is woken: every API call is mocked.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Host, HostRecipe, Runtime } from "@/lib/types";
import { FleetStage } from "../FleetStage";
import { FreeBox } from "../FreeBox";
import { BoxCockpit } from "../cockpit/BoxCockpit";

const mockStore = vi.hoisted(() => ({
  state: { currentUser: { id: "u1", email: "a@b.c", name: "Admin", role: "admin" } as { id: string; email: string; name: string; role: string } | null },
}));
vi.mock("@/lib/store", () => ({
  useAppStore: (selector: (s: typeof mockStore.state) => unknown) => selector(mockStore.state),
}));

window.matchMedia = vi.fn().mockImplementation((query: string) => ({
  matches: true,
  media: query,
  addListener: vi.fn(),
  removeListener: vi.fn(),
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
})) as unknown as typeof window.matchMedia;

function makeHost(over: Partial<Host>): Host {
  return {
    id: over.slug ?? "h", slug: "h", display_name: "H", kind: "ssh",
    ssh_host: null, ssh_user: null, ssh_key_path: null, ssh_credential_id: null, role: null, fabric_ip: null, control_url: null,
    wol_mac_address: null, power_managed: false, notes: null, enabled: true,
    ui_order: 0, created_at: "", updated_at: "",
    ...over,
  } as Host;
}

function makeRuntime(over: Partial<Runtime>): Runtime {
  return {
    id: over.slug ?? "rt", slug: "rt", display_name: "RT",
    runtime_type: "lmstudio", provider: "lmstudio",
    endpoint: "http://192.0.2.20:1234/v1", healthcheck_path: "/health",
    container_name: null, role_tags: [], supports_tools: true,
    supports_reasoning: false, supports_streaming: true,
    preferred_context_len: 8192, max_context_len: 32768,
    gpu_profile: "default", memory_notes: "", startup_notes: "",
    ui_order: 0, enabled: true, state: "stopped",
    ...over,
  };
}

function makeRecipe(over: Partial<HostRecipe>): HostRecipe {
  return {
    slug: "r", display_name: "R", engine: "vllm", topology: { nodes: 1 }, port: 8000,
    instance_runtime_id: null, running: false, startable: true, fit: "solo", reason: null,
    busy_hosts: [], candidate_workers: [],
    ...over,
  };
}

// The box of the finding: the host is power-managed, its runtime rows are not.
const wolHost = makeHost({
  slug: "wol", display_name: "Wake Box", kind: "flask_wol", power_managed: true,
  control_url: "http://192.0.2.20:5555", wol_mac_address: "00:00:5E:00:53:01",
});
const unflaggedRows = [
  makeRuntime({ id: "rt-a", slug: "rt-a", power_managed: false, host: { id: "wol", slug: "wol", display_name: "Wake Box" } }),
  makeRuntime({ id: "rt-slot", slug: "rt-slot", power_managed: false, is_slot: true, host: { id: "wol", slug: "wol", display_name: "Wake Box" } }),
];

const ASLEEP = { kind: "flask_wol", reachable: false, awake: false, status: "asleep" } as never;

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  vi.restoreAllMocks();
  mockStore.state.currentUser = { id: "u1", email: "a@b.c", name: "Admin", role: "admin" };
  vi.spyOn(api.hosts, "recipes").mockResolvedValue([makeRecipe({ slug: "duo", topology: { nodes: 2 }, fit: "duo" })]);
  vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({ runtime_slug: "rt-slot", count: 0, agents: [] });
  vi.spyOn(api.hosts, "pulse").mockResolvedValue({ points: [], now_tps: null, idle_seconds: null, available: false });
  vi.spyOn(api.hosts, "metricsHistory").mockResolvedValue({ points: [], window: 3600, sample_seconds: 5 });
  vi.spyOn(api.hosts, "autostart").mockResolvedValue({
    host_id: "wol", enabled: false, recipe_slug: null, recipe_display_name: null,
    role: null, via_head: null, last_attempt_at: null, last_result: null,
  } as never);
  vi.spyOn(api.localRegistry, "list").mockResolvedValue({ recipes: [], total: 0, sources: [] });
  vi.spyOn(api.nodes, "devices").mockResolvedValue([]);
});

describe("sleeping card from the host flag", () => {
  it("a power-managed host without a flagged runtime gets the sleeping card with Wake, never Start model", async () => {
    vi.spyOn(api.hosts, "metrics").mockResolvedValue(ASLEEP);
    const wake = vi.spyOn(api.hosts, "wake").mockResolvedValue({ ok: true, message: "ok" });
    const runtimeWake = vi.spyOn(api.runtimes, "wake").mockResolvedValue({ ok: true, message: "ok" });

    wrap(<FleetStage stageGroups={[]} sleepingGroups={[{ host: wolHost, runtimes: unflaggedRows }]} onOpen={() => {}} />);

    const card = await screen.findByTestId("asleep-box");
    await waitFor(() => expect(card).toHaveTextContent("asleep"));
    expect(card).not.toHaveTextContent("Start model");
    // Round-7 re-check t2 NICE-3: one word per state — the corner says
    // "asleep", so the line under it does not say "powered off".
    expect(card).not.toHaveTextContent(/powered off/i);
    fireEvent.click(within(card).getByTestId("wake-box"));
    await waitFor(() => expect(wake).toHaveBeenCalledWith("wol"));
    expect(runtimeWake).not.toHaveBeenCalled();
    // The wake is sent; the card says so instead of offering it again.
    await waitFor(() => expect(within(card).getByTestId("wake-sent")).toBeInTheDocument());
    expect(within(card).getByTestId("wake-box")).toBeDisabled();
  });

  it("a viewer sees the sleeping box but no Wake (an operator action)", async () => {
    mockStore.state.currentUser = { id: "u2", email: "v@b.c", name: "Viewer", role: "viewer" };
    vi.spyOn(api.hosts, "metrics").mockResolvedValue(ASLEEP);
    wrap(<FleetStage stageGroups={[]} sleepingGroups={[{ host: wolHost, runtimes: unflaggedRows }]} onOpen={() => {}} />);
    const card = await screen.findByTestId("asleep-box");
    await waitFor(() => expect(card).toHaveTextContent("asleep"));
    expect(within(card).queryByTestId("wake-box")).not.toBeInTheDocument();
  });

  it("a failed wake says so on the card", async () => {
    vi.spyOn(api.hosts, "metrics").mockResolvedValue(ASLEEP);
    vi.spyOn(api.hosts, "wake").mockRejectedValue(new Error("400"));
    wrap(<FleetStage stageGroups={[]} sleepingGroups={[{ host: wolHost, runtimes: unflaggedRows }]} onOpen={() => {}} />);
    const card = await screen.findByTestId("asleep-box");
    await waitFor(() => expect(card).toHaveTextContent("asleep"));
    fireEvent.click(within(card).getByTestId("wake-box"));
    await waitFor(() => expect(within(card).getByTestId("wake-failed")).toBeInTheDocument());
    expect(within(card).getByTestId("wake-box")).not.toBeDisabled();
  });

  it("no wake helper on the Mac (503): says the box was not woken, no 'Waking…'", async () => {
    vi.spyOn(api.hosts, "metrics").mockResolvedValue(ASLEEP);
    vi.spyOn(api.hosts, "wake").mockRejectedValue(
      new Error('API 503: {"detail":"Wake helper on the Mac is not running"}'),
    );
    wrap(<FleetStage stageGroups={[]} sleepingGroups={[{ host: wolHost, runtimes: unflaggedRows }]} onOpen={() => {}} />);
    const card = await screen.findByTestId("asleep-box");
    await waitFor(() => expect(card).toHaveTextContent("asleep"));
    fireEvent.click(within(card).getByTestId("wake-box"));
    const failed = await within(card).findByTestId("wake-failed");
    expect(failed).toHaveTextContent("wake helper on the Mac is not running");
    expect(within(card).queryByTestId("wake-sent")).not.toBeInTheDocument();
    expect(card).not.toHaveTextContent("Waking");
    expect(within(card).getByTestId("wake-box")).not.toBeDisabled();
  });
});

describe("FreeBox — no Start model while asleep or checking", () => {
  it("asleep (its own check) on a power-managed host: Wake, no Start model", async () => {
    vi.spyOn(api.hosts, "metrics").mockResolvedValue(ASLEEP);
    wrap(<FreeBox host={wolHost} slot={unflaggedRows[1]} onOpenCockpit={() => {}} />);
    const card = await screen.findByTestId("free-box");
    await waitFor(() => expect(card).toHaveTextContent("asleep"));
    expect(card).not.toHaveTextContent("Start model");
    expect(within(card).getByTestId("wake-box")).toBeInTheDocument();
  });

  it("a power-managed box while its own check is out: neither Start model nor Wake", async () => {
    vi.spyOn(api.hosts, "metrics").mockImplementation(() => new Promise(() => {}));
    wrap(<FreeBox host={wolHost} slot={null} onOpenCockpit={() => {}} />);
    const card = await screen.findByTestId("free-box");
    expect(card).toHaveTextContent("checking");
    // Give the recipes query time to land — the switcher must still stay away.
    await waitFor(() => expect(api.hosts.recipes).toHaveBeenCalled());
    expect(card).not.toHaveTextContent("Start model");
    expect(within(card).queryByTestId("wake-box")).not.toBeInTheDocument();
  });

  it("any other box keeps Start model while a slow metrics call is out (PR #976 review)", async () => {
    vi.spyOn(api.hosts, "metrics").mockImplementation(() => new Promise(() => {}));
    const spark = makeHost({ slug: "spark", display_name: "Spark" });
    wrap(<FreeBox host={spark} slot={null} onOpenCockpit={() => {}} />);
    const card = await screen.findByTestId("free-box");
    await waitFor(() => expect(card).toHaveTextContent("Start model"));
    expect(within(card).queryByTestId("wake-box")).not.toBeInTheDocument();
  });

  it("an awake free box still offers Start model", async () => {
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({ reachable: true, vram_total_mb: 8192, vram_used_mb: 1024 });
    const spark = makeHost({ slug: "spark", display_name: "Spark" });
    wrap(<FreeBox host={spark} slot={null} onOpenCockpit={() => {}} />);
    const card = await screen.findByTestId("free-box");
    await waitFor(() => expect(card).toHaveTextContent("Start model"));
    expect(within(card).queryByTestId("wake-box")).not.toBeInTheDocument();
  });

  it("'Recipes fit' counts only recipes that fit this one box", async () => {
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({ reachable: true, vram_total_mb: 8192, vram_used_mb: 1024 });
    vi.spyOn(api.hosts, "recipes").mockResolvedValue([
      makeRecipe({ slug: "duo", topology: { nodes: 2 }, fit: "duo", capacity: { ok: true, warnings: [] } as never }),
      makeRecipe({ slug: "trio", topology: { nodes: 3 } as never, fit: "duo", capacity: { ok: true, warnings: [] } as never }),
      makeRecipe({ slug: "solo-ok", capacity: { ok: true, warnings: [] } as never }),
      makeRecipe({ slug: "solo-big", capacity: { ok: false, warnings: [] } as never }),
    ]);
    const spark = makeHost({ slug: "spark", display_name: "Spark" });
    wrap(<FreeBox host={spark} slot={null} onOpenCockpit={() => {}} />);
    const card = await screen.findByTestId("free-box");
    const cell = await within(card).findByText("Recipes fit");
    await waitFor(() => expect(cell.parentElement).toHaveTextContent(/^1\s*Recipes fit$/));
  });
});

describe("BoxCockpit of a sleeping box", () => {
  it("says it sleeps, offers Wake, no Restart", async () => {
    vi.spyOn(api.hosts, "metrics").mockResolvedValue(ASLEEP);
    vi.spyOn(api.hosts, "wake").mockResolvedValue({ ok: true, message: "ok" });
    wrap(
      <BoxCockpit
        open
        onClose={() => {}}
        members={[{ host: wolHost, role: null, slot: unflaggedRows[1] }]}
        activeHostId="wol"
        onSwitchActive={() => {}}
        runtime={unflaggedRows[1]}
      />,
    );
    const telemetry = await screen.findByTestId("cockpit-group-telemetry");
    await waitFor(() => expect(telemetry).toHaveTextContent(/asleep/i));
    expect(telemetry).not.toHaveTextContent("collecting");
    expect(screen.queryByTestId("cockpit-restart")).not.toBeInTheDocument();
    // Round-7 re-check t2 NICE-2: nothing runs on a sleeping box — no Stop.
    expect(screen.queryByTestId("cockpit-stop")).not.toBeInTheDocument();
    fireEvent.click(within(screen.getByTestId("box-cockpit")).getByTestId("wake-box"));
    await waitFor(() => expect(api.hosts.wake).toHaveBeenCalledWith("wol"));
  });

  it("card and cockpit share the sent wake: no second 'Wake' in the cockpit", async () => {
    vi.spyOn(api.hosts, "metrics").mockResolvedValue(ASLEEP);
    const wake = vi.spyOn(api.hosts, "wake").mockResolvedValue({ ok: true, message: "ok" });
    wrap(<FleetStage stageGroups={[]} sleepingGroups={[{ host: wolHost, runtimes: unflaggedRows }]} onOpen={() => {}} />);
    const card = await screen.findByTestId("asleep-box");
    await waitFor(() => expect(card).toHaveTextContent("asleep"));
    fireEvent.click(within(card).getByTestId("wake-box"));
    await waitFor(() => expect(within(card).getByTestId("wake-sent")).toBeInTheDocument());
    fireEvent.click(card.querySelector(".stage-actions-gear") as HTMLElement);
    const cockpit = await screen.findByTestId("box-cockpit");
    await waitFor(() => expect(within(cockpit).getByTestId("wake-box")).toBeDisabled());
    expect(within(cockpit).getByTestId("wake-sent")).toBeInTheDocument();
    expect(wake).toHaveBeenCalledTimes(1);
  });

  it("an awake box keeps its telemetry and Restart", async () => {
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({ reachable: true });
    const spark = makeHost({ slug: "spark", display_name: "Spark" });
    const rt = makeRuntime({ id: "rt-s", slug: "rt-s", state: "ready" });
    wrap(
      <BoxCockpit open onClose={() => {}} members={[{ host: spark, role: "head" }]} activeHostId="spark" onSwitchActive={() => {}} runtime={rt} />,
    );
    const telemetry = await screen.findByTestId("cockpit-group-telemetry");
    await waitFor(() => expect(telemetry).toHaveTextContent("collecting"));
    expect(screen.getByTestId("cockpit-restart")).toBeInTheDocument();
    expect(screen.getByTestId("cockpit-stop")).toBeInTheDocument();
    expect(screen.queryByTestId("wake-box")).not.toBeInTheDocument();
  });
});
