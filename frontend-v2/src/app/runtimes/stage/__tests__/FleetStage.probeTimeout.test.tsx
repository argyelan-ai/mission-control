/**
 * FleetStage — a box whose state probe timed out (GET /runtimes reports its
 * recipe runtimes as unknown / container_status "probe_timeout"). Nothing is
 * known about it, so it must not be painted as a free box ("no model ·
 * ready") nor fold into the "nothing is running" empty state.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Host, RuntimeLiveStatus } from "@/lib/types";
import type { HostGroup } from "../../grouping";
import { FleetStage } from "../FleetStage";

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

const alpha = makeHost({ slug: "alpha", display_name: "Box Alpha", ui_order: 0 });
const recipe = {
  id: "rt-1", slug: "rt-1", display_name: "Recipe One",
  runtime_type: "ssh_process", provider: "vllm",
  endpoint: "http://192.0.2.10:8000/v1", healthcheck_path: "/health",
  container_name: null, role_tags: [], supports_tools: true,
  supports_reasoning: false, supports_streaming: true,
  preferred_context_len: 8192, max_context_len: 32768,
  gpu_profile: "default", memory_notes: "", startup_notes: "",
  ui_order: 1, enabled: true,
  host: { id: "alpha", slug: "alpha", display_name: "Box Alpha" },
} as HostGroup["runtimes"][number];

function renderStage(groups: HostGroup[], live?: Record<string, RuntimeLiveStatus>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <FleetStage stageGroups={groups} sleepingGroups={[]} live={live} onOpen={() => {}} />
    </QueryClientProvider>
  );
}

describe("FleetStage — probe timed out", () => {
  beforeEach(() => {
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({
      reachable: true, gpu_util_pct: 10, vram_used_mb: 1024, vram_total_mb: 8192, gpu_temp_c: 40,
    });
    vi.spyOn(api.hosts, "pulse").mockResolvedValue({ points: [], now_tps: null, idle_seconds: null, available: false });
    vi.spyOn(api.hosts, "recipes").mockResolvedValue([]);
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({ runtime_slug: "rt-1", count: 0, agents: [] });
  });

  it("shows the box with the reason instead of 'free' or the empty state", () => {
    renderStage([{ host: alpha, runtimes: [{ ...recipe, state: "unknown", container_status: "probe_timeout" }] }]);
    expect(screen.queryByTestId("empty-stage")).not.toBeInTheDocument();
    const card = screen.getByTestId("free-box");
    expect(card).toHaveTextContent("Unknown — probe timed out");
    expect(card).not.toHaveTextContent("no model · ready");
  });

  it("a truly free box keeps its free wording", () => {
    renderStage([
      { host: alpha, runtimes: [{ ...recipe, state: "stopped", container_status: "no_process" }] },
    ]);
    // Only one box and nothing running → the empty state, no timeout wording.
    expect(screen.getByTestId("empty-stage")).toBeInTheDocument();
    expect(screen.queryByText(/probe timed out/)).not.toBeInTheDocument();
  });

  // Round-2 user test (t4 S1): the page now paints from the unprobed rows
  // (container_status "not_probed") while the live probes run. Such a box is
  // "being checked" — never "free · ready", never the empty state.
  it("a box whose rows are not probed yet says it is being checked", async () => {
    renderStage([{ host: alpha, runtimes: [{ ...recipe, state: "unknown", container_status: "not_probed" }] }]);
    expect(screen.queryByTestId("empty-stage")).not.toBeInTheDocument();
    const card = screen.getByTestId("free-box");
    // The box itself answers (metrics in: 7 GB free) — it is the runtime
    // probe that is still out, and that alone keeps it "being checked".
    await waitFor(() => expect(card).toHaveTextContent("7GB"));
    expect(card).toHaveTextContent("Checking…");
    expect(card).not.toHaveTextContent("no model · ready");
    // PR #960 review SHOULD 1: no armed "Start model" and no "· idle" claim
    // on an unchecked box — on the first paint it may be half of a running duo.
    expect(card).not.toHaveTextContent("Start model");
    expect(card).not.toHaveTextContent("Endpoint · idle");
    expect(card).toHaveTextContent("Endpoint");
  });

  // PR #960 review (NICE): failed metrics are no proof of "ready".
  it("a free box whose metrics fail says the readings are missing, not 'ready'", async () => {
    vi.spyOn(api.hosts, "metrics").mockRejectedValue(new Error("boom"));
    const other = makeHost({ slug: "beta", display_name: "Box Beta", ui_order: 1 });
    const serving = { ...recipe, id: "rt-2", slug: "rt-2", state: "ready" as const, host: { id: "beta", slug: "beta", display_name: "Box Beta" } };
    renderStage([
      { host: alpha, runtimes: [{ ...recipe, state: "stopped" }] },
      { host: other, runtimes: [serving] },
    ]);
    const card = await screen.findByTestId("free-box");
    await waitFor(() => expect(card).toHaveTextContent("no model · no readings"));
    expect(card).not.toHaveTextContent("no model · ready");
    expect(card).toHaveTextContent("Start model");
  });

  // t4 S2: a sleeping box was painted "free · no model · ready" although its
  // own metrics said reachable:false / asleep. "ready" needs reachable:true.
  it("a box that does not answer is shown asleep/unreachable, never ready", async () => {
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({
      kind: "flask_wol", reachable: false, awake: false, status: "asleep",
    } as never);
    const sleeper = makeHost({ slug: "sleeper", display_name: "Sleeper", kind: "flask_wol", power_managed: true });
    const other = makeHost({ slug: "beta", display_name: "Box Beta", ui_order: 1 });
    const serving = { ...recipe, id: "rt-2", slug: "rt-2", state: "ready" as const, host: { id: "beta", slug: "beta", display_name: "Box Beta" } };
    renderStage([
      { host: sleeper, runtimes: [{ ...recipe, host: { id: "sleeper", slug: "sleeper", display_name: "Sleeper" }, state: "stopped" }] },
      { host: other, runtimes: [serving] },
    ]);
    const card = await screen.findByTestId("free-box");
    await waitFor(() => expect(screen.getByTestId("free-box-line")).toHaveTextContent("no model · no readings"));
    expect(card).not.toHaveTextContent("no model · ready");
    // Round-3 (r2 N1): the state is said ONCE — "ASLEEP" in the corner and
    // "asleep · not reachable" right below it was the same word twice.
    expect(card.textContent?.match(/asleep/gi) ?? []).toHaveLength(1);
    // …and no "· idle" claim about an engine that does not answer.
    expect(card).not.toHaveTextContent("Endpoint · idle");
    expect(card).toHaveTextContent("Endpoint");
  });

  // Round-4 (t3 S1, r2 S1): the watcher's "3837 failed probes" is about the
  // MODEL port. An awake box without a model read SCHLÄFT → PRÜFT → FREI in
  // 2 s (once "asleep" next to "Start model"). Before the box's own check
  // answers the card checks; a dark model port alone is never "asleep".
  it("a dark model port alone is not 'asleep' — the card checks until the box answers", async () => {
    let answer: (v: unknown) => void = () => {};
    vi.spyOn(api.hosts, "metrics").mockImplementation(
      () => new Promise((resolve) => { answer = resolve as (v: unknown) => void; }) as never,
    );
    const awakeBox = makeHost({ slug: "sleeper", display_name: "Sleeper", kind: "flask_wol", power_managed: true });
    const other = makeHost({ slug: "beta", display_name: "Box Beta", ui_order: 1 });
    const serving = { ...recipe, id: "rt-2", slug: "rt-2", state: "ready" as const, host: { id: "beta", slug: "beta", display_name: "Box Beta" } };
    const darkRow = { ...recipe, id: "rt-sleep", slug: "rt-sleep", host: { id: "sleeper", slug: "sleeper", display_name: "Sleeper" }, state: "stopped" as const, container_status: "no_process" };
    renderStage(
      [{ host: awakeBox, runtimes: [darkRow] }, { host: other, runtimes: [serving] }],
      { "rt-sleep": { reachable: false, served_model: null, latency_ms: null, last_probe_at: "", consecutive_failures: 3837, drift: false } },
    );
    const card = await screen.findByTestId("free-box");
    expect(card).toHaveTextContent("checking");
    expect(card).not.toHaveTextContent(/asleep/i);
    expect(card).not.toHaveTextContent("unreachable");
    // Nor "Endpoint · idle" while the box's own check is still out.
    expect(card).not.toHaveTextContent("Endpoint · idle");

    answer({ kind: "flask_wol", reachable: true, awake: true, status: "awake" });
    await waitFor(() => expect(card).toHaveTextContent("no model · ready"));
    expect(card).toHaveTextContent("free");
    expect(card).not.toHaveTextContent(/asleep/i);
  });

  // The same box asleep for real: its own check says so, and then it is.
  it("a box whose own check fails reads asleep, after checking", async () => {
    let answer: (v: unknown) => void = () => {};
    vi.spyOn(api.hosts, "metrics").mockImplementation(
      () => new Promise((resolve) => { answer = resolve as (v: unknown) => void; }) as never,
    );
    const sleeper = makeHost({ slug: "sleeper", display_name: "Sleeper", kind: "flask_wol", power_managed: true });
    const other = makeHost({ slug: "beta", display_name: "Box Beta", ui_order: 1 });
    const serving = { ...recipe, id: "rt-2", slug: "rt-2", state: "ready" as const, host: { id: "beta", slug: "beta", display_name: "Box Beta" } };
    const darkRow = { ...recipe, id: "rt-sleep", slug: "rt-sleep", host: { id: "sleeper", slug: "sleeper", display_name: "Sleeper" }, state: "stopped" as const };
    renderStage([{ host: sleeper, runtimes: [darkRow] }, { host: other, runtimes: [serving] }]);
    const card = await screen.findByTestId("free-box");
    expect(card).not.toHaveTextContent(/asleep/i);

    answer({ kind: "flask_wol", reachable: false, awake: false, status: "asleep" });
    await waitFor(() => expect(card).toHaveTextContent("asleep"));
  });

  // The "sleeping" card (power-managed box, its power-managed runtime idle)
  // made the same claim without asking the box: an awake one read "asleep".
  it("a power-managed box in the sleeping row is checked first and shown free when awake", async () => {
    let answer: (v: unknown) => void = () => {};
    vi.spyOn(api.hosts, "metrics").mockImplementation(
      () => new Promise((resolve) => { answer = resolve as (v: unknown) => void; }) as never,
    );
    const box = makeHost({ slug: "wol", display_name: "Wake Box", kind: "flask_wol", power_managed: true });
    const pmRow = { ...recipe, id: "rt-pm", slug: "rt-pm", power_managed: true, state: "stopped" as const, host: { id: "wol", slug: "wol", display_name: "Wake Box" } };
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <FleetStage stageGroups={[]} sleepingGroups={[{ host: box, runtimes: [pmRow] }]} onOpen={() => {}} />
      </QueryClientProvider>,
    );
    const card = await screen.findByTestId("asleep-box");
    expect(card).toHaveTextContent("checking");
    expect(card).not.toHaveTextContent(/asleep/i);

    answer({ kind: "flask_wol", reachable: true, awake: true, status: "awake" });
    const free = await screen.findByTestId("free-box");
    await waitFor(() => expect(free).toHaveTextContent("no model · ready"));
    expect(screen.queryByTestId("asleep-box")).not.toBeInTheDocument();
  });

  it("a power-managed box whose own check fails keeps the sleeping card", async () => {
    vi.spyOn(api.hosts, "metrics").mockResolvedValue({ kind: "flask_wol", reachable: false, awake: false, status: "asleep" } as never);
    const box = makeHost({ slug: "wol", display_name: "Wake Box", kind: "flask_wol", power_managed: true });
    const pmRow = { ...recipe, id: "rt-pm", slug: "rt-pm", power_managed: true, state: "stopped" as const, host: { id: "wol", slug: "wol", display_name: "Wake Box" } };
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <FleetStage stageGroups={[]} sleepingGroups={[{ host: box, runtimes: [pmRow] }]} onOpen={() => {}} />
      </QueryClientProvider>,
    );
    const card = await screen.findByTestId("asleep-box");
    await waitFor(() => expect(card).toHaveTextContent("asleep"));
    expect(screen.getByTestId("wake-box")).toBeInTheDocument();
  });

  it("one or two failed probes are a blip — the card still checks", async () => {
    vi.spyOn(api.hosts, "metrics").mockImplementation(() => new Promise(() => {}));
    const sleeper = makeHost({ slug: "sleeper", display_name: "Sleeper", kind: "flask_wol", power_managed: true });
    const other = makeHost({ slug: "beta", display_name: "Box Beta", ui_order: 1 });
    const serving = { ...recipe, id: "rt-2", slug: "rt-2", state: "ready" as const, host: { id: "beta", slug: "beta", display_name: "Box Beta" } };
    const sleeperRow = { ...recipe, id: "rt-sleep", slug: "rt-sleep", host: { id: "sleeper", slug: "sleeper", display_name: "Sleeper" }, state: "unknown" as const, container_status: "not_probed" };
    renderStage(
      [{ host: sleeper, runtimes: [sleeperRow] }, { host: other, runtimes: [serving] }],
      { "rt-sleep": { reachable: false, served_model: null, latency_ms: null, last_probe_at: "", consecutive_failures: 1, drift: false } },
    );
    const card = await screen.findByTestId("free-box");
    expect(card).toHaveTextContent("Checking…");
    expect(card).not.toHaveTextContent("asleep");
  });

  it("a box that answers keeps 'no model · ready'", async () => {
    const other = makeHost({ slug: "beta", display_name: "Box Beta", ui_order: 1 });
    const serving = { ...recipe, id: "rt-2", slug: "rt-2", state: "ready" as const, host: { id: "beta", slug: "beta", display_name: "Box Beta" } };
    renderStage([
      { host: alpha, runtimes: [{ ...recipe, state: "stopped" }] },
      { host: other, runtimes: [serving] },
    ]);
    const card = await screen.findByTestId("free-box");
    await waitFor(() => expect(card).toHaveTextContent("no model · ready"));
  });
});
