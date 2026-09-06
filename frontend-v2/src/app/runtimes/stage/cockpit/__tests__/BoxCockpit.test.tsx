/**
 * BoxCockpit — die Schublade je Box (Spec §4). Deckt Öffnen/Schliessen/Esc,
 * den Box-Umschalter bei Duo, den Stop-409-Fluss und den No-Runtime-Fall ab.
 */
import { useState } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BoxCockpit, type BoxCockpitMember } from "../BoxCockpit";
import { api } from "@/lib/api";
import type { Device, DeviceState, Host, Runtime } from "@/lib/types";

const mockStore = vi.hoisted(() => ({
  state: { currentUser: { id: "u1", email: "a@b.c", name: "Admin", role: "admin" } as { id: string; email: string; name: string; role: string } | null },
}));
vi.mock("@/lib/store", () => ({
  useAppStore: (selector: (s: typeof mockStore.state) => unknown) => selector(mockStore.state),
}));

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function makeHost(over: Partial<Host> = {}): Host {
  return {
    id: over.slug ?? "spark", slug: "spark", display_name: "DGX Spark", kind: "ssh",
    ssh_host: "192.0.2.10", ssh_user: null, ssh_key_path: null, ssh_credential_id: null,
    role: "head", fabric_ip: "10.0.0.1", control_url: null,
    wol_mac_address: null, power_managed: false, notes: null, enabled: true,
    ui_order: 0, created_at: "", updated_at: "",
    ...over,
  };
}

function makeState(over: Partial<DeviceState> = {}): DeviceState {
  return {
    gpu_mode: "eco", gpu_clock_mhz: 2000, gpu_power_w: 33, gpu_temp_c: 62,
    min_free_kbytes: 5242880, oom_guard: "active", latency_tune: true,
    mtu: { iface: "enP7s7", value: 9000 }, applied_at: "2026-09-06T00:00:00Z", last_error: null,
    ...over,
  };
}

function makeDevice(over: Partial<Device> = {}): Device {
  return {
    host_id: "spark", slug: "spark", display_name: "DGX Spark", has_agent: true,
    desired_state: { gpu_mode: "eco" }, device_state: makeState(),
    device_state_updated_at: "2026-09-06T00:00:00Z", agent_last_seen_at: "2026-09-06T00:00:00Z",
    status: "green", reason: "in_sync", diff: [], last_error: null, age_s: 4,
    ...over,
  };
}

function makeRuntime(over: Partial<Runtime> = {}): Runtime {
  return {
    id: "rt-1", slug: "qwen38-flash-next", display_name: "Qwen3.8 Flash Next",
    runtime_type: "vllm_docker", provider: "vllm",
    endpoint: "http://192.0.2.10:8000/v1", healthcheck_path: "/health",
    container_name: null, role_tags: [], supports_tools: true,
    supports_reasoning: false, supports_streaming: true,
    preferred_context_len: 8192, max_context_len: 262144,
    gpu_profile: "default", memory_notes: "", startup_notes: "",
    ui_order: 0, enabled: true, state: "ready",
    ...over,
  };
}

const headMember: BoxCockpitMember = { host: makeHost(), role: "head", device: makeDevice() };
const workerMember: BoxCockpitMember = {
  host: makeHost({ id: "gx10", slug: "gx10", display_name: "GX10", role: "worker", fabric_ip: "10.0.0.2" }),
  role: "worker",
  device: makeDevice({ host_id: "gx10", slug: "gx10" }),
};

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api.hosts, "pulse").mockResolvedValue({ points: [], now_tps: null, idle_seconds: null, available: false });
  vi.spyOn(api.hosts, "metricsHistory").mockResolvedValue({ points: [], window: 3600, sample_seconds: 5 });
  vi.spyOn(api.hosts, "autostart").mockResolvedValue({
    host_id: "spark", enabled: true, recipe_slug: "qwen38-flash-next", recipe_display_name: "Qwen3.8 Flash Next",
    role: "head", via_head: null, last_attempt_at: null, last_result: null,
  });
  vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({ runtime_slug: "qwen38-flash-next", count: 0, agents: [] });
  vi.spyOn(api.localRegistry, "list").mockResolvedValue({ recipes: [], total: 0, sources: [] });
});

describe("BoxCockpit", () => {
  it("renders nothing when closed", () => {
    renderWithQuery(
      <BoxCockpit open={false} onClose={() => {}} members={[headMember]} activeHostId="spark" onSwitchActive={() => {}} runtime={makeRuntime()} />
    );
    expect(screen.queryByTestId("box-cockpit")).not.toBeInTheDocument();
  });

  it("open: shows the box name, mono facts and all five groups", async () => {
    renderWithQuery(
      <BoxCockpit open onClose={() => {}} members={[headMember]} activeHostId="spark" onSwitchActive={() => {}} runtime={makeRuntime()} />
    );
    expect(await screen.findByTestId("box-cockpit")).toBeInTheDocument();
    expect(screen.getByText("DGX Spark")).toBeInTheDocument();
    expect(screen.getByText("Head · 192.0.2.10 · fabric 10.0.0.1")).toBeInTheDocument();
    expect(screen.getByTestId("cockpit-group-telemetry")).toBeInTheDocument();
    expect(screen.getByTestId("cockpit-group-mode")).toBeInTheDocument();
    expect(screen.getByTestId("cockpit-group-autostart")).toBeInTheDocument();
    expect(screen.getByTestId("cockpit-group-connection")).toBeInTheDocument();
    expect(screen.getByTestId("cockpit-group-recipe")).toBeInTheDocument();
  });

  it("close button calls onClose", async () => {
    const onClose = vi.fn();
    renderWithQuery(
      <BoxCockpit open onClose={onClose} members={[headMember]} activeHostId="spark" onSwitchActive={() => {}} runtime={makeRuntime()} />
    );
    const closeBtn = await screen.findByTestId("cockpit-close");
    await act(async () => { closeBtn.click(); });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("Escape closes the cockpit (SlideOverPanel's own handler)", async () => {
    const onClose = vi.fn();
    renderWithQuery(
      <BoxCockpit open onClose={onClose} members={[headMember]} activeHostId="spark" onSwitchActive={() => {}} runtime={makeRuntime()} />
    );
    await screen.findByTestId("box-cockpit");
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("focus trap: the close button receives focus on open", async () => {
    renderWithQuery(
      <BoxCockpit open onClose={() => {}} members={[headMember]} activeHostId="spark" onSwitchActive={() => {}} runtime={makeRuntime()} />
    );
    const closeBtn = await screen.findByTestId("cockpit-close");
    await waitFor(() => expect(document.activeElement).toBe(closeBtn));
  });

  it("solo box: no box switcher is shown", async () => {
    renderWithQuery(
      <BoxCockpit open onClose={() => {}} members={[headMember]} activeHostId="spark" onSwitchActive={() => {}} runtime={makeRuntime()} />
    );
    await screen.findByTestId("box-cockpit");
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
  });

  it("duo: box switcher shows both boxes, clicking the other calls onSwitchActive", async () => {
    const onSwitchActive = vi.fn();
    renderWithQuery(
      <BoxCockpit
        open
        onClose={() => {}}
        members={[headMember, workerMember]}
        activeHostId="spark"
        onSwitchActive={onSwitchActive}
        runtime={makeRuntime({ member_hosts: [{ host_id: "gx10", slug: "gx10", display_name: "GX10", role: "worker", node_rank: 1 }] })}
      />
    );
    const switcher = await screen.findByTestId("cockpit-box-switch-gx10");
    await act(async () => { switcher.click(); });
    expect(onSwitchActive).toHaveBeenCalledWith("gx10");
  });

  it("duo: switching the active box re-renders the header for the new box", async () => {
    function Wrapper() {
      const [active, setActive] = useState("spark");
      return (
        <BoxCockpit
          open
          onClose={() => {}}
          members={[headMember, workerMember]}
          activeHostId={active}
          onSwitchActive={setActive}
          runtime={makeRuntime({ member_hosts: [{ host_id: "gx10", slug: "gx10", display_name: "GX10", role: "worker", node_rank: 1 }] })}
        />
      );
    }
    renderWithQuery(<Wrapper />);
    const switcher = await screen.findByTestId("cockpit-box-switch-gx10");
    await act(async () => { switcher.click(); });
    await waitFor(() => expect(screen.getByText(/10\.0\.0\.2|fabric 10\.0\.0\.2/)).toBeInTheDocument());
  });

  it("no runtime (free box): Connection group shows the no-model hint, footer actions are disabled", async () => {
    renderWithQuery(
      <BoxCockpit open onClose={() => {}} members={[headMember]} activeHostId="spark" onSwitchActive={() => {}} runtime={null} />
    );
    await screen.findByTestId("box-cockpit");
    expect(screen.getByText(/no model is running/i)).toBeInTheDocument();
    expect(screen.getByTestId("cockpit-stop")).toBeDisabled();
    expect(screen.getByTestId("cockpit-restart")).toBeDisabled();
    expect(screen.getByTestId("cockpit-reprobe")).toBeDisabled();
  });

  it("no device: Mode group shows the no-device-agent hint instead of the radio list", async () => {
    renderWithQuery(
      <BoxCockpit
        open
        onClose={() => {}}
        members={[{ host: makeHost(), role: "head", device: undefined }]}
        activeHostId="spark"
        onSwitchActive={() => {}}
        runtime={makeRuntime()}
      />
    );
    expect(await screen.findByTestId("mode-list-no-device")).toBeInTheDocument();
  });

  it("Stop → 409 agent_busy shows an inline confirmation, clicking 'Stop anyway' retries with force:true", async () => {
    const stopSpy = vi
      .spyOn(api.runtimes, "stop")
      .mockImplementationOnce(() =>
        Promise.reject(new Error('API 409: {"detail":{"code":"agent_busy","agents":[{"name":"Alpha","slug":"alpha","task_id":"t-1"}]}}'))
      )
      .mockImplementationOnce(() => Promise.resolve({ ok: true, message: "stopped", autostart_disabled: true }));

    renderWithQuery(
      <BoxCockpit open onClose={() => {}} members={[headMember]} activeHostId="spark" onSwitchActive={() => {}} runtime={makeRuntime()} />
    );
    const stopBtn = await screen.findByTestId("cockpit-stop");
    await act(async () => { stopBtn.click(); });
    expect(await screen.findByTestId("cockpit-stop-conflict")).toBeInTheDocument();
    expect(screen.getByText(/Alpha/)).toBeInTheDocument();

    const anyway = screen.getByTestId("cockpit-stop-anyway");
    await act(async () => { anyway.click(); });
    await waitFor(() => expect(stopSpy).toHaveBeenCalledTimes(2));
    expect(stopSpy.mock.calls[0]).toEqual(["rt-1", { force: false }]);
    expect(stopSpy.mock.calls[1]).toEqual(["rt-1", { force: true }]);
    expect(await screen.findByTestId("cockpit-message")).toHaveTextContent(/autostart/i);
  });

  it("Connection URL shows the box's OWN slot-runtime endpoint (ADR-078), not the serving runtime's endpoint (Team-Lead-Fund 06.09.2026)", async () => {
    const serving = makeRuntime({ endpoint: "http://198.51.100.20:8000/v1" });
    const slot = makeRuntime({
      id: "slot-1", slug: "spark-slot", is_slot: true, endpoint: "http://192.0.2.10:8000/v1",
    });
    renderWithQuery(
      <BoxCockpit
        open
        onClose={() => {}}
        members={[{ ...headMember, slot }]}
        activeHostId="spark"
        onSwitchActive={() => {}}
        runtime={serving}
      />
    );
    await screen.findByTestId("box-cockpit");
    const copyBtn = await screen.findByTestId("connection-copy");
    expect(copyBtn.textContent).toContain("192.0.2.10");
    expect(copyBtn.textContent).not.toContain("198.51.100.20");
  });

  it("Connection URL falls back to the serving runtime's endpoint when the box has no slot runtime yet", async () => {
    renderWithQuery(
      <BoxCockpit open onClose={() => {}} members={[headMember]} activeHostId="spark" onSwitchActive={() => {}} runtime={makeRuntime({ endpoint: "http://192.0.2.20:8000/v1" })} />
    );
    const copyBtn = await screen.findByTestId("connection-copy");
    expect(copyBtn.textContent).toContain("192.0.2.20");
  });

  it("Connection group loads bound agents from the SLOT runtime (ADR-078), not the recipe runtime — three agents on the slot show as three links (Team-Lead-Fund 06.09.2026)", async () => {
    const serving = makeRuntime({ slug: "qwen38-flash-next" });
    const slot = makeRuntime({ id: "slot-1", slug: "dgx-spark-slot", is_slot: true, endpoint: "http://192.0.2.10:8000/v1" });

    const agentsSpy = vi.spyOn(api.runtimes.db, "agents").mockImplementation((slug: string) =>
      Promise.resolve(
        slug === "dgx-spark-slot"
          ? { runtime_slug: slug, count: 3, agents: [
              { id: "a1", name: "Alpha", agent_runtime: "cli-bridge" },
              { id: "a2", name: "Beta", agent_runtime: "cli-bridge" },
              { id: "a3", name: "Gamma", agent_runtime: "cli-bridge" },
            ] }
          : { runtime_slug: slug, count: 0, agents: [] }
      )
    );

    renderWithQuery(
      <BoxCockpit
        open
        onClose={() => {}}
        members={[{ ...headMember, slot }]}
        activeHostId="spark"
        onSwitchActive={() => {}}
        runtime={serving}
      />
    );

    const links = await screen.findAllByTestId("connection-agent-link");
    expect(links).toHaveLength(3);
    expect(links.map((l) => l.textContent)).toEqual(["Alpha", "Beta", "Gamma"]);
    expect(agentsSpy).toHaveBeenCalledWith("dgx-spark-slot");
    expect(agentsSpy).not.toHaveBeenCalledWith("qwen38-flash-next");
  });

  it("touch targets: close button and footer actions are >= 44px (Review #440 Fund 3)", async () => {
    renderWithQuery(
      <BoxCockpit open onClose={() => {}} members={[headMember]} activeHostId="spark" onSwitchActive={() => {}} runtime={makeRuntime()} />
    );
    const closeBtn = await screen.findByTestId("cockpit-close");
    expect(closeBtn.className).toMatch(/\bmin-h-touch\b/);
    expect(closeBtn.className).toMatch(/\bmin-w-touch\b/);
    // The fixed 36px inline size the review flagged must be gone — height
    // now comes from the min-h-touch utility class, not an inline style.
    expect(closeBtn.style.width).toBe("");
    expect(closeBtn.style.height).toBe("");

    for (const testId of ["cockpit-reprobe", "cockpit-restart", "cockpit-stop"]) {
      const btn = await screen.findByTestId(testId);
      expect(btn.className).toMatch(/\bmin-h-touch\b/);
    }
  });

  it("Re-probe and Restart call the runtime actions", async () => {
    const probeSpy = vi.spyOn(api.runtimes, "probeModel").mockResolvedValue({
      slug: "qwen38-flash-next", old_model_identifier: "a", new_model_identifier: "a", changed: false,
    });
    const restartSpy = vi.spyOn(api.runtimes, "restart").mockResolvedValue({ ok: true, message: "restarted" });

    renderWithQuery(
      <BoxCockpit open onClose={() => {}} members={[headMember]} activeHostId="spark" onSwitchActive={() => {}} runtime={makeRuntime()} />
    );
    const reprobe = await screen.findByTestId("cockpit-reprobe");
    await act(async () => { reprobe.click(); });
    await waitFor(() => expect(probeSpy).toHaveBeenCalledWith("rt-1"));

    const restart = screen.getByTestId("cockpit-restart");
    await act(async () => { restart.click(); });
    await waitFor(() => expect(restartSpy).toHaveBeenCalledWith("rt-1"));
  });
});
