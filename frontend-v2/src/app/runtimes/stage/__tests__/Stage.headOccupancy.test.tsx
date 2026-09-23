/**
 * /runtimes occupancy (docs/specs/head-launcher.md §8.3, build plan B8):
 * busy badge on the stage card → links to the task; while the engine serves,
 * switch / stop are disabled with the head's task title; a refusal from the
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
import { mkRun } from "@/lib/__tests__/headFixtures";
import type { HeadBusy } from "@/lib/heads";
import type { Host, HostRecipe, Runtime } from "@/lib/types";

vi.mock("@/lib/store", () => ({
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
});

describe("Stage — head busy badge", () => {
  it("shows the badge linking to the head's task when a head holds a member box", async () => {
    vi.spyOn(api.heads, "occupancy").mockResolvedValue({ boxes: { "host-1": head } });
    renderWithQuery(<Stage runtime={runtime} members={[{ host, role: "head" }]} onOpenCockpit={() => {}} />);
    const badge = await screen.findByTestId("head-busy-badge");
    expect(badge).toHaveTextContent("Head working: “Fix flaky retry test”");
    expect(badge).toHaveAttribute("href", "/tasks?task=task-7");
    // …and the actions below are locked with the task title.
    expect(screen.getByTestId("head-on-box-notice")).toHaveTextContent("A head is working on this box (“Fix flaky retry test”).");
    expect(screen.getByTestId("stop-runtime")).toBeDisabled();
    expect(within(screen.getByTestId("head-on-box-notice")).getByRole("link", { name: "Open task" })).toHaveAttribute("href", "/tasks?task=task-7");
  });

  it("no badge and nothing locked when the box is free (or heads are off)", async () => {
    vi.spyOn(api.heads, "occupancy").mockRejectedValue(new Error('API 404: {"detail":{"code":"heads_disabled"}}'));
    renderWithQuery(<Stage runtime={runtime} members={[{ host, role: "head" }]} onOpenCockpit={() => {}} />);
    await waitFor(() => expect(api.heads.occupancy).toHaveBeenCalled());
    expect(screen.queryByTestId("head-busy-badge")).not.toBeInTheDocument();
    expect(await screen.findByTestId("stop-runtime")).toBeEnabled();
  });
});

describe("Switch / stop under a working head", () => {
  it("the switch trigger is disabled and names the head's task", async () => {
    vi.spyOn(api.hosts, "recipes").mockResolvedValue([{ slug: "qwen", display_name: "Qwen", running: false } as unknown as HostRecipe]);
    renderWithQuery(
      <ActionBar hostId="host-1" hostName="box" servingName="GLM local" runtimeId="rt-1" onOpenCockpit={() => {}} headOnBox={head} />,
    );
    const trigger = await screen.findByTestId("recipe-dropdown-trigger");
    expect(trigger).toBeDisabled();
    expect(trigger).toHaveAttribute("title", expect.stringContaining("Fix flaky retry test"));
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
    const stop = vi.spyOn(api.heads, "stop").mockResolvedValue({ run_id: "orphan", state: "stopping" });
    renderWithQuery(<HeadOrphanRuns />);
    const section = await screen.findByTestId("head-orphan-runs");
    expect(within(section).getAllByRole("listitem")).toHaveLength(1);
    expect(section).toHaveTextContent("omp · glm-local · task deleted");
    await act(async () => { within(section).getByRole("button", { name: "Stop" }).click(); });
    await waitFor(() => expect(stop).toHaveBeenCalledWith("orphan"));
  });
});
