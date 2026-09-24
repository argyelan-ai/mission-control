/**
 * /runtimes occupancy (docs/specs/head-launcher.md §8.3, build plan B8):
 * the model card stays quiet (no badge, no permanent notice); "In use" counts
 * agents + heads; clicking switch / stop while a head works shows the reason
 * inline and does nothing else; a refusal from the
 * box guard (409 head_on_box) is one sentence, not JSON; runs whose task was
 * deleted get a Stop.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Stage } from "../Stage";
import { ActionBar } from "../ActionBar";
import { HeadOrphanRuns } from "@/components/heads/HeadOccupancy";
import { HostRecipeSwitcher } from "@/components/shared/HostRecipeSwitcher";
import { api } from "@/lib/api";
import { mkPair, mkRun } from "@/lib/__tests__/headFixtures";
import { notify } from "@/lib/notify";
import userEvent from "@testing-library/user-event";
import type { HeadBusy } from "@/lib/heads";
import type { Agent, Host, HostRecipe, Runtime } from "@/lib/types";
import de from "../../../../../messages/de.json";
import en from "../../../../../messages/en.json";
import { IntlMessageFormat } from "intl-messageformat";

// Keep the real store module (notify needs useNotificationStore) and only
// pin the current user — a partial mock made notify.success throw unseen.
vi.mock("@/lib/store", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/store")>()),
  useAppStore: (selector: (s: unknown) => unknown) => selector({ currentUser: { id: "u1", email: "a@b.c", name: "A", role: "admin" } }),
}));

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const host = {
  id: "host-1", slug: "box", display_name: "Box", kind: "ssh",
  ssh_host: null, ssh_user: null, ssh_key_path: null, ssh_credential_id: null, role: null, fabric_ip: null, control_url: null,
  wol_mac_address: null, power_managed: false, notes: null, enabled: true, ui_order: 0, created_at: "", updated_at: "",
} as Host;

const runtime = {
  id: "rt-1", slug: "glm-local", display_name: "GLM local",
  runtime_type: "vllm_docker", provider: "vllm", endpoint: "http://engine.invalid:8000/v1", healthcheck_path: "/health",
  container_name: null, role_tags: [], supports_tools: true, supports_reasoning: false, supports_streaming: true,
  preferred_context_len: 8192, max_context_len: 32768, gpu_profile: "default", memory_notes: "", startup_notes: "",
  ui_order: 0, enabled: true, state: "ready", host: { id: "host-1", slug: "box", display_name: "Box" },
} as Runtime;

const head: HeadBusy = { run_id: "run-1", task_id: "task-7", title: "Fix flaky retry test", harness: "omp", runtime_slug: "glm-local", since: null };

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api.hosts, "pulse").mockResolvedValue({ points: [], now_tps: null, idle_seconds: null, available: false });
  vi.spyOn(api.hosts, "recipes").mockResolvedValue([]);
  vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({ runtime_slug: "glm-local", count: 0, agents: [] });
  vi.spyOn(api.agents, "list").mockResolvedValue([]);
});

const mkAgent = (id: string, name: string, mode: "active" | "paused") =>
  ({ id, name, operational_mode: mode, status: "idle" }) as unknown as Agent;

describe("Stage — model card while a head works", () => {
  it("no badge and no permanent notice — the card stays quiet", async () => {
    vi.spyOn(api.heads, "occupancy").mockResolvedValue({ boxes: { "host-1": head } });
    renderWithQuery(<Stage runtime={runtime} members={[{ host, role: "head" }]} onOpenCockpit={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("kpi-in-use")).toHaveTextContent("1"));
    expect(screen.queryByTestId("head-busy-badge")).not.toBeInTheDocument();
    expect(screen.queryByTestId("head-on-box-notice")).not.toBeInTheDocument();
    expect(screen.queryByTestId("head-in-use-notice")).not.toBeInTheDocument();
    expect(screen.queryByText(/Stop the head first/)).not.toBeInTheDocument();
    // the actions stay clickable — the reason comes on click
    expect(screen.getByTestId("stop-runtime")).toBeEnabled();
  });

  it("IN USE counts active agents + heads and says who in the tooltip", async () => {
    vi.spyOn(api.heads, "occupancy").mockResolvedValue({ boxes: { "host-1": head } });
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({
      runtime_slug: "glm-local", count: 2,
      agents: [{ id: "a1", name: "Rex", agent_runtime: "x" }, { id: "a2", name: "Nova", agent_runtime: "x" }],
    });
    vi.spyOn(api.agents, "list").mockResolvedValue([mkAgent("a1", "Rex", "active"), mkAgent("a2", "Nova", "active")]);
    renderWithQuery(<Stage runtime={runtime} members={[{ host, role: "head" }]} onOpenCockpit={() => {}} />);
    const tile = await screen.findByTestId("kpi-in-use");
    await waitFor(() => expect(tile).toHaveTextContent("3"));
    expect(tile).toHaveTextContent("In use");
    expect(screen.queryByText("Agents")).not.toBeInTheDocument();
    const title = tile.getAttribute("title") ?? "";
    // (the test mock of next-intl does not render ICU plurals — the counts
    // sentence itself is checked with the real formatter below)
    expect(title).toContain("{heads, plural");
    expect(title).toContain("Fix flaky retry test");
    expect(title).toContain("Rex, Nova");
  });

  it("paused agents do not count: 1 head + 3 paused agents → 1", async () => {
    vi.spyOn(api.heads, "occupancy").mockResolvedValue({ boxes: { "host-1": head } });
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({
      runtime_slug: "glm-local", count: 3,
      agents: [
        { id: "a1", name: "Hermes", agent_runtime: "x" },
        { id: "a2", name: "Rex", agent_runtime: "x" },
        { id: "a3", name: "Sparky", agent_runtime: "x" },
      ],
    });
    vi.spyOn(api.agents, "list").mockResolvedValue([
      mkAgent("a1", "Hermes", "paused"), mkAgent("a2", "Rex", "paused"), mkAgent("a3", "Sparky", "paused"),
    ]);
    renderWithQuery(<Stage runtime={runtime} members={[{ host, role: "head" }]} onOpenCockpit={() => {}} />);
    const tile = await screen.findByTestId("kpi-in-use");
    await waitFor(() => expect(tile.getAttribute("title") ?? "").toContain("Paused: Hermes, Rex, Sparky"));
    expect(tile).toHaveTextContent(/^1In use$/);
    const title = tile.getAttribute("title") ?? "";
    expect(title).toContain("{paused, plural"); // the "with paused" sentence
    expect(title).not.toContain("Agents:");
  });

  it("free box: IN USE is only the agents, nothing locked", async () => {
    vi.spyOn(api.heads, "occupancy").mockRejectedValue(new Error('API 404: {"detail":{"code":"heads_disabled"}}'));
    renderWithQuery(<Stage runtime={runtime} members={[{ host, role: "head" }]} onOpenCockpit={() => {}} />);
    await waitFor(() => expect(api.heads.occupancy).toHaveBeenCalled());
    const tile = await screen.findByTestId("kpi-in-use");
    expect(tile).toHaveTextContent("0");
    expect(tile.getAttribute("title")).not.toContain("Head:");
    expect(await screen.findByTestId("stop-runtime")).toBeEnabled();
  });
});

describe("Switch / stop under a working head", () => {
  it("clicking Switch model shows the reason and does not open the list or switch", async () => {
    vi.spyOn(api.hosts, "recipes").mockResolvedValue([{
      slug: "qwen", display_name: "Qwen", engine: "vllm_docker", topology: { nodes: 1 }, port: 8000,
      instance_runtime_id: null, running: false, startable: true, fit: "solo", reason: null,
      busy_hosts: [], candidate_workers: [],
    } as HostRecipe]);
    const start = vi.spyOn(api.hosts, "startRecipe");
    renderWithQuery(
      <ActionBar hostId="host-1" hostName="box" servingName="GLM local" runtimeId="rt-1" onOpenCockpit={() => {}} headOnBox={head} />,
    );
    const trigger = await screen.findByTestId("recipe-dropdown-trigger");
    expect(trigger).toBeEnabled();
    expect(screen.queryByTestId("head-in-use-notice")).not.toBeInTheDocument();
    await userEvent.click(trigger);
    const notice = await screen.findByTestId("head-in-use-notice");
    expect(notice).toHaveTextContent("A head is working on this box (“Fix flaky retry test”).");
    expect(notice).toHaveTextContent("Switching now would cut it off. Stop the head first.");
    expect(within(notice).getByRole("link", { name: "Open task" })).toHaveAttribute("href", "/tasks?task=task-7");
    expect(screen.queryByTestId("recipe-option-qwen")).not.toBeInTheDocument();
    expect(start).not.toHaveBeenCalled();
    // OK closes it and the bar is back
    await userEvent.click(within(notice).getByRole("button", { name: "OK" }));
    expect(screen.queryByTestId("head-in-use-notice")).not.toBeInTheDocument();
    expect(screen.getByTestId("recipe-dropdown-trigger")).toBeInTheDocument();
  });

  it("clicking Stop shows the reason and does not stop", async () => {
    const stop = vi.spyOn(api.runtimes, "stop");
    renderWithQuery(
      <ActionBar hostId="host-1" hostName="box" servingName="GLM local" runtimeId="rt-1" onOpenCockpit={() => {}} headOnBox={head} />,
    );
    await userEvent.click(await screen.findByTestId("stop-runtime"));
    const notice = await screen.findByTestId("head-in-use-notice");
    expect(notice).toHaveTextContent("Stopping now would cut it off. Stop the head first.");
    expect(stop).not.toHaveBeenCalled();
  });

  it("the counts sentence reads right in both languages (real ICU formatter)", () => {
    const fmt = (msg: string, v: Record<string, number>, loc: string) => new IntlMessageFormat(msg, loc).format(v);
    expect(fmt(en.runtimes.stage.inUseTooltip, { heads: 1, agents: 0 }, "en")).toBe("1 head · 0 active agents");
    expect(fmt(en.runtimes.stage.inUseTooltip, { heads: 2, agents: 1 }, "en")).toBe("2 heads · 1 active agent");
    expect(fmt(de.runtimes.stage.inUseTooltip, { heads: 1, agents: 2 }, "de")).toBe("1 Head · 2 aktive Agenten");
    expect(fmt(en.runtimes.stage.inUseTooltipWithPaused, { heads: 1, agents: 0, paused: 3 }, "en")).toBe("1 head · 0 active agents · 3 paused");
    expect(fmt(de.runtimes.stage.inUseTooltipWithPaused, { heads: 1, agents: 0, paused: 3 }, "de")).toBe("1 Head · 0 aktive Agenten · 3 pausiert");
  });

  it("the German texts are there", () => {
    expect(de.runtimes.stage.kpiInUse).toBe("In Nutzung");
    expect(de.heads.runtimes.onBoxBodyStop).toBeTruthy();
    expect(de.heads.runtimes.ok).toBeTruthy();
    expect(de.runtimes.stage.inUseTooltip).toContain("{heads");
  });

  it("a dead engine stays recoverable: trouble variant keeps Stop enabled", async () => {
    renderWithQuery(
      <ActionBar hostId="host-1" hostName="box" servingName="GLM local" runtimeId="rt-1" variant="trouble" onOpenCockpit={() => {}} headOnBox={head} />,
    );
    expect(await screen.findByTestId("stop-runtime")).toBeEnabled();
  });

  it("409 head_on_box from Stop shows the notice with the task, not JSON", async () => {
    vi.spyOn(api.runtimes, "stop").mockRejectedValue(
      new Error('API 409: {"detail":{"code":"head_on_box","run_id":"run-1","task_id":"task-7","title":"Fix flaky retry test"}}'),
    );
    renderWithQuery(<ActionBar hostId="host-1" hostName="box" servingName="GLM local" runtimeId="rt-1" onOpenCockpit={() => {}} />);
    await act(async () => { (await screen.findByTestId("stop-runtime")).click(); });
    const notice = await screen.findByTestId("head-on-box-notice");
    expect(notice).toHaveTextContent("Stop the head first.");
    expect(notice).not.toHaveTextContent("head_on_box");
  });

  it("the refusal notice goes away once the occupancy has seen the head end", async () => {
    vi.spyOn(api.runtimes, "stop").mockRejectedValue(
      new Error('API 409: {"detail":{"code":"head_on_box","run_id":"run-1","task_id":"task-7","title":"Fix flaky retry test"}}'),
    );
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const bar = (h: HeadBusy | null) => (
      <QueryClientProvider client={qc}>
        <ActionBar hostId="host-1" hostName="box" servingName="GLM local" runtimeId="rt-1" onOpenCockpit={() => {}} headOnBox={h} variant="trouble" />
      </QueryClientProvider>
    );
    const { rerender } = render(bar(null));
    await act(async () => { (await screen.findByTestId("stop-runtime")).click(); });
    expect(await screen.findByTestId("head-on-box-notice")).toBeInTheDocument();
    rerender(bar(head)); // the poll now sees the head
    expect(screen.getByTestId("head-on-box-notice")).toBeInTheDocument();
    rerender(bar(null)); // …and then the box free again
    await waitFor(() => expect(screen.queryByTestId("head-on-box-notice")).not.toBeInTheDocument());
  });

  it("409 head_on_box from a recipe start is one sentence in the switcher", async () => {
    const recipe = {
      slug: "qwen", display_name: "Qwen", engine: "vllm_docker", topology: { nodes: 1 }, port: 8000,
      instance_runtime_id: null, running: false, startable: true, fit: "solo", reason: null,
      busy_hosts: [], candidate_workers: [],
    } as HostRecipe;
    vi.spyOn(api.hosts, "recipes").mockResolvedValue([recipe]);
    vi.spyOn(api.hosts, "startRecipe").mockRejectedValue(
      new Error('API 409: {"detail":{"code":"head_on_box","run_id":"run-1","task_id":"task-7","title":"Fix flaky retry test"}}'),
    );
    renderWithQuery(<HostRecipeSwitcher hostId="host-1" hostName="box" compact />);
    await act(async () => { (await screen.findByTestId("recipe-dropdown-trigger")).click(); });
    await act(async () => { (await screen.findByTestId("recipe-option-qwen")).click(); });
    await act(async () => { screen.getByTestId("recipe-confirm-start").click(); });
    const err = await screen.findByTestId("recipe-start-error");
    expect(err).toHaveTextContent("A head is working on this box (“Fix flaky retry test”). Switching now would cut it off.");
    expect(err).not.toHaveTextContent("head_on_box");
  });
});

describe("Runs without a task", () => {
  it("lists active runs whose task was deleted, with Stop", async () => {
    vi.spyOn(api.heads, "list").mockResolvedValue({
      runs: [mkRun({ run_id: "orphan", task_deleted: true }), mkRun({ run_id: "normal", task_deleted: false })],
    });
    vi.spyOn(api.heads, "occupancy").mockResolvedValue({ boxes: {} });
    vi.spyOn(api.heads, "pairs").mockResolvedValue({ pairs: [mkPair()], default_pair: mkPair() });
    const stop = vi.spyOn(api.heads, "stop").mockResolvedValue({ run_id: "orphan", state: "stopping" });
    const success = vi.spyOn(notify, "success");
    const failure = vi.spyOn(notify, "error");
    renderWithQuery(<HeadOrphanRuns />);
    const section = await screen.findByTestId("head-orphan-runs");
    expect(within(section).getAllByRole("listitem")).toHaveLength(1);
    // display name, not the runtime slug
    await waitFor(() => expect(section).toHaveTextContent("omp · GLM local · task deleted"));
    await userEvent.click(within(section).getByRole("button", { name: "Stop" }));
    expect(stop).not.toHaveBeenCalled(); // asks first
    await userEvent.click(within(section).getByTestId("head-orphan-stop-orphan-confirm-yes"));
    await waitFor(() => expect(stop).toHaveBeenCalledWith("orphan"));
    // the success path really runs (it used to throw inside the mutation)
    await waitFor(() => expect(success).toHaveBeenCalledWith("Stop requested"));
    expect(failure).not.toHaveBeenCalled();
  });

  it("asks nothing while the launcher is off (404 heads_disabled)", async () => {
    vi.spyOn(api.heads, "occupancy").mockRejectedValue(new Error('API 404: {"detail":{"code":"heads_disabled"}}'));
    const list = vi.spyOn(api.heads, "list").mockResolvedValue({ runs: [] });
    renderWithQuery(<HeadOrphanRuns />);
    await waitFor(() => expect(api.heads.occupancy).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(list).not.toHaveBeenCalled();
  });
});
