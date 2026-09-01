import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DeviceControl, MODE_FACTS } from "../DeviceControl";
import { api } from "@/lib/api";
import type { Device, DeviceState } from "@/lib/types";

// Wie in HostsSection.test.tsx: der echte zustand-Store schreibt über die
// persist-Middleware in localStorage — im jsdom reicht ein Selektor-Mock,
// DeviceControl braucht nur currentUser.role.
const mockStore = vi.hoisted(() => ({
  state: { currentUser: null as { id: string; email: string; name: string; role: string } | null },
}));
vi.mock("@/lib/store", () => ({
  useAppStore: (selector: (s: typeof mockStore.state) => unknown) => selector(mockStore.state),
}));

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const makeState = (over: Partial<DeviceState> = {}): DeviceState => ({
  gpu_mode: "eco",
  gpu_clock_mhz: 1989,
  gpu_power_w: 33,
  gpu_temp_c: 63,
  min_free_kbytes: 5242880,
  oom_guard: "active",
  latency_tune: true,
  mtu: { iface: "enP7s7", value: 9000 },
  applied_at: "2026-09-01T00:12:00Z",
  last_error: null,
  ...over,
});

const makeDevice = (over: Partial<Device> = {}): Device => ({
  host_id: "host-1",
  slug: "gpu-box-1",
  display_name: "GPU Box 1",
  has_agent: true,
  desired_state: { gpu_mode: "eco" },
  device_state: makeState(),
  device_state_updated_at: "2026-09-01T00:12:00Z",
  agent_last_seen_at: "2026-09-01T00:12:00Z",
  status: "green",
  reason: "in_sync",
  diff: [],
  last_error: null,
  age_s: 4,
  ...over,
});


/** Die Balkenhöhe steckt im scaleY der Transformation — der Balken ist immer
 *  voll hoch und wird von der Grundlinie aus zusammengedrückt (kein Layout). */
function barPct(testId: string): number {
  const el = screen.getByTestId(testId) as HTMLElement;
  const m = /scaleY\(([-\d.]+)\)/.exec(el.style.transform);
  if (!m) throw new Error(`kein scaleY in transform: "${el.style.transform}"`);
  return parseFloat(m[1]) * 100;
}

describe("DeviceControl", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    mockStore.state.currentUser = { id: "u1", email: "a@b.c", name: "Admin", role: "admin" };
  });

  it("shows the device with its traffic light and live readings", async () => {
    vi.spyOn(api.nodes, "devices").mockResolvedValue([
      makeDevice({ status: "yellow", reason: "stale" }),
    ]);

    renderWithQuery(<DeviceControl />);

    // Der Name kommt direkt aus /nodes/devices — keine zweite Quelle
    expect(await screen.findByText("GPU Box 1")).toBeInTheDocument();
    expect(screen.getByTestId("health-dot-yellow")).toBeInTheDocument();
    expect(screen.getByText("stopped reporting")).toBeInTheDocument();
    // Live-Werte aus device_state, nicht aus der Referenzmessung
    expect(screen.getByText("1989 MHz")).toBeInTheDocument();
    expect(screen.getByText("33.0 W")).toBeInTheDocument();
    expect(screen.getByText("63 °C")).toBeInTheDocument();
  });

  it("renders all four traffic-light colours", async () => {
    vi.spyOn(api.nodes, "devices").mockResolvedValue([
      makeDevice({ host_id: "h1", slug: "a", status: "green", reason: "in_sync" }),
      makeDevice({ host_id: "h2", slug: "b", status: "yellow", reason: "pending", diff: ["mtu"] }),
      makeDevice({ host_id: "h3", slug: "c", status: "red", reason: "last_error", last_error: "boom" }),
      makeDevice({
        host_id: "h4",
        slug: "d",
        status: "grey",
        reason: "no_agent",
        has_agent: false,
        device_state: null,
      }),
    ]);

    renderWithQuery(<DeviceControl />);

    expect(await screen.findByTestId("health-dot-green")).toBeInTheDocument();
    expect(screen.getByTestId("health-dot-yellow")).toBeInTheDocument();
    expect(screen.getByTestId("health-dot-red")).toBeInTheDocument();
    expect(screen.getByTestId("health-dot-grey")).toBeInTheDocument();
    // Gerät ohne gemeldeten Zustand sagt das, statt Nullen zu zeigen
    expect(screen.getByTestId("device-no-report")).toBeInTheDocument();
  });

  // Eine frische Box hat weder Ist noch Soll. Ein hervorgehobener Reiter
  // würde eine Einstellung behaupten, die niemand gemacht hat.
  it("highlights nothing when neither a reading nor a target exists", async () => {
    vi.spyOn(api.nodes, "devices").mockResolvedValue([
      makeDevice({
        has_agent: false,
        status: "grey",
        reason: "no_agent",
        device_state: null,
        desired_state: null,
      }),
    ]);

    renderWithQuery(<DeviceControl />);
    await screen.findByTestId("device-card");

    expect(screen.queryByTestId("mode-indicator")).toBeNull();
    for (const m of ["eco+", "eco", "normal", "boost"]) {
      expect(screen.getByTestId(`mode-${m}`)).toHaveAttribute("data-active", "false");
    }
  });

  it("marks the reported mode as the checked one", async () => {
    vi.spyOn(api.nodes, "devices").mockResolvedValue([
      makeDevice({ device_state: makeState({ gpu_mode: "normal" }), desired_state: { gpu_mode: "normal" } }),
    ]);

    renderWithQuery(<DeviceControl />);

    await waitFor(() =>
      expect(screen.getByTestId("mode-normal")).toHaveAttribute("aria-checked", "true"),
    );
    expect(screen.getByTestId("mode-eco")).toHaveAttribute("aria-checked", "false");
    expect(screen.getByTestId("mode-boost")).toHaveAttribute("aria-checked", "false");
  });

  // Die eigentliche Aussage der Oberfläche: die Erzeugungs-Balken stehen über
  // alle vier Stufen gleich hoch, die Strom-Balken bilden eine Treppe. Ginge
  // das kaputt (z.B. andere Skala je Spalte), erzählte das Bild eine Lüge.
  it("draws generation flat across all modes and power as a staircase", async () => {
    vi.spyOn(api.nodes, "devices").mockResolvedValue([makeDevice()]);
    renderWithQuery(<DeviceControl />);
    await screen.findByTestId("device-card");

    const gen = ["eco+", "eco", "normal", "boost"].map((m) => barPct(`bar-generation-${m}`));
    expect(Math.max(...gen) - Math.min(...gen)).toBeLessThan(4);

    const power = ["eco+", "eco", "normal", "boost"].map((m) => barPct(`bar-power-${m}`));
    expect(power[0]).toBeLessThan(power[1]);
    expect(power[1]).toBeLessThan(power[2]);
    expect(power[2]).toBeLessThan(power[3]);
    expect(power[3] - power[0]).toBeGreaterThan(30);
  });

  // Der Betreiber will eine flüssige Animation. `width`/`height` zu animieren
  // erzwingt bei jedem Bild ein neues Layout — dieser Test hält fest, dass
  // Balken und Fortschritt ausschliesslich über transform laufen.
  it("animates through transform only, never through layout properties", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.nodes, "devices").mockResolvedValue([makeDevice()]);
    vi.spyOn(api.nodes, "setDesiredState").mockResolvedValue(makeDevice());

    renderWithQuery(<DeviceControl />);
    await screen.findByTestId("device-card");
    await user.click(screen.getByTestId("mode-boost"));
    const card = await screen.findByTestId("device-card");

    for (const el of Array.from(card.querySelectorAll<HTMLElement>("*"))) {
      const tr = el.style.transition;
      expect(tr).not.toMatch(/\b(width|height|padding|margin|top|left)\b/);
    }
    // Balken tragen ihre Höhe im transform, nicht im height
    expect(screen.getByTestId("bar-power-boost").style.height).toBe("100%");
    expect(screen.getByTestId("bar-power-boost").style.transform).toMatch(/scaleY/);
  });

  it("sends only the desired state and shows the hand-over while the box catches up", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.nodes, "devices").mockResolvedValue([makeDevice()]);
    const set = vi
      .spyOn(api.nodes, "setDesiredState")
      .mockResolvedValue(makeDevice({ desired_state: { gpu_mode: "eco+" }, status: "yellow", reason: "pending", diff: ["gpu_mode"] }));

    renderWithQuery(<DeviceControl />);
    await screen.findByTestId("device-card");

    await user.click(screen.getByTestId("mode-eco+"));

    // PUT ersetzt den Soll — die bestehenden Vorgaben müssen mitgeschickt werden
    expect(set).toHaveBeenCalledWith("host-1", { gpu_mode: "eco+" });
    const pendingBox = await screen.findByTestId("device-pending");
    expect(within(pendingBox).getByText(/switching to eco\+/)).toBeInTheDocument();
    // Ziel-Umrandung neben dem gefüllten Ist-Reiter — beides gleichzeitig
    expect(screen.getByTestId("mode-target-outline")).toBeInTheDocument();
    expect(screen.getByTestId("mode-indicator")).toBeInTheDocument();
    // Ist bleibt bis zur Bestätigung durch das Gerät bei eco
    expect(screen.getByTestId("mode-eco")).toHaveAttribute("aria-checked", "true");
  });

  // PUT ersetzt den Soll-Zustand vollständig. Schickte die Kachel nur
  // {gpu_mode}, löschte jeder Moduswechsel die Härtungs-Vorgaben — und der
  // Agent liesse sie ab dann in Ruhe, ohne dass es jemand merkt.
  it("keeps the other hardening settings when only the mode changes", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.nodes, "devices").mockResolvedValue([
      makeDevice({
        desired_state: { gpu_mode: "eco", oom_guard: true, latency_tune: true, mtu: 9000, min_free_kbytes: 5242880 },
      }),
    ]);
    const set = vi.spyOn(api.nodes, "setDesiredState").mockResolvedValue(makeDevice());

    renderWithQuery(<DeviceControl />);
    await screen.findByTestId("device-card");
    await user.click(screen.getByTestId("mode-normal"));

    expect(set).toHaveBeenCalledWith("host-1", {
      gpu_mode: "normal",
      oom_guard: true,
      latency_tune: true,
      mtu: 9000,
      min_free_kbytes: 5242880,
    });
  });

  it("shows the hand-over for a desired state set elsewhere (no click here)", async () => {
    vi.spyOn(api.nodes, "devices").mockResolvedValue([
      makeDevice({
        device_state: makeState({ gpu_mode: "boost" }),
        desired_state: { gpu_mode: "eco" },
        status: "yellow",
        reason: "pending",
        diff: ["gpu_mode"],
      }),
    ]);

    renderWithQuery(<DeviceControl />);

    expect(await screen.findByTestId("device-pending")).toBeInTheDocument();
    expect(screen.getByTestId("mode-target-outline")).toBeInTheDocument();
  });

  it("keeps quiet about the hand-over when desired and reported agree", async () => {
    vi.spyOn(api.nodes, "devices").mockResolvedValue([makeDevice()]);
    renderWithQuery(<DeviceControl />);
    await screen.findByTestId("device-card");

    expect(screen.queryByTestId("device-pending")).toBeNull();
    expect(screen.queryByTestId("mode-target-outline")).toBeNull();
  });

  it("stays quiet about boost while a gentler step is chosen", async () => {
    vi.spyOn(api.nodes, "devices").mockResolvedValue([makeDevice()]);
    renderWithQuery(<DeviceControl />);
    await screen.findByTestId("device-card");

    expect(screen.queryByTestId("device-boost-warning")).toBeNull();
  });

  it("warns about the risk once boost is the chosen step", async () => {
    vi.spyOn(api.nodes, "devices").mockResolvedValue([
      makeDevice({ device_state: makeState({ gpu_mode: "boost" }), desired_state: { gpu_mode: "boost" } }),
    ]);
    renderWithQuery(<DeviceControl />);

    expect(await screen.findByTestId("device-boost-warning")).toBeInTheDocument();
  });

  it("passes the box's own error through instead of swallowing it", async () => {
    vi.spyOn(api.nodes, "devices").mockResolvedValue([
      makeDevice({ status: "red", reason: "last_error", last_error: "nvidia-smi: permission denied" }),
    ]);

    renderWithQuery(<DeviceControl />);

    expect(await screen.findByTestId("device-last-error")).toHaveTextContent(
      "The box reports: nvidia-smi: permission denied",
    );
  });

  it("drops the target again when saving fails, so nobody waits for nothing", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.nodes, "devices").mockResolvedValue([makeDevice()]);
    vi.spyOn(api.nodes, "setDesiredState").mockRejectedValue(new Error("API 500"));

    renderWithQuery(<DeviceControl />);
    await screen.findByTestId("device-card");

    await user.click(screen.getByTestId("mode-boost"));

    expect(await screen.findByTestId("device-apply-failed")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId("device-pending")).toBeNull());
  });

  it("is read-only for non-admins", async () => {
    mockStore.state.currentUser = { id: "u2", email: "x@y.z", name: "Ops", role: "user" };
    vi.spyOn(api.nodes, "devices").mockResolvedValue([makeDevice()]);
    const set = vi.spyOn(api.nodes, "setDesiredState");

    renderWithQuery(<DeviceControl />);
    await screen.findByTestId("device-card");

    expect(screen.getByTestId("mode-boost")).toBeDisabled();
    expect(screen.getByText("Only administrators can change the mode.")).toBeInTheDocument();
    expect(set).not.toHaveBeenCalled();
  });

  it("shows an empty state when no box is paired", async () => {
    vi.spyOn(api.nodes, "devices").mockResolvedValue([]);
    renderWithQuery(<DeviceControl />);
    expect(
      await screen.findByText("No devices paired — a box running the node agent shows up here by itself."),
    ).toBeInTheDocument();
  });

  it("keeps the measured reference numbers exactly as the contract states them", () => {
    expect(MODE_FACTS.boost).toMatchObject({ clockMhz: null, tokensPerSec: 20.3, watt: 59.5, tempC: 87 });
    expect(MODE_FACTS.normal).toMatchObject({ clockMhz: 2200, tokensPerSec: 19.6, watt: 39.9, tempC: 81 });
    expect(MODE_FACTS.eco).toMatchObject({ clockMhz: 2000, tokensPerSec: 20.4, watt: 32.5, tempC: 74 });
    expect(MODE_FACTS["eco+"]).toMatchObject({ clockMhz: 1800, tokensPerSec: 19.8, watt: 27.1, tempC: 69 });
  });
});
