import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DeviceModeStrip, MODE_FACTS } from "../DeviceControl";
import { api } from "@/lib/api";
import type { Device, DeviceState } from "@/lib/types";

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

/** Länge der Stromtreppe im kompakten Schalter (scaleX). */
function compactBarPct(mode: string): number {
  const el = screen.getByTestId(`compact-bar-${mode}`) as HTMLElement;
  const m = /scaleX\(([-\d.]+)\)/.exec(el.style.transform);
  if (!m) throw new Error(`kein scaleX in transform: "${el.style.transform}"`);
  return parseFloat(m[1]) * 100;
}

describe("DeviceModeStrip", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("shows the four steps compactly, with what each one costs", async () => {
    renderWithQuery(<DeviceModeStrip device={makeDevice()} canControl />);

    for (const m of ["eco+", "eco", "normal", "boost"]) {
      expect(screen.getByTestId(`compact-mode-${m}`)).toBeInTheDocument();
    }
    // Der Stromwert je Stufe steht dran — die Zahl trägt auf dieser Fläche
    // mehr als jede Grafik.
    expect(screen.getByTestId("compact-mode-eco+")).toHaveTextContent("≈27 W");
    expect(screen.getByTestId("compact-mode-boost")).toHaveTextContent("≈60 W");
    expect(screen.getByTestId("compact-mode-eco")).toHaveAttribute("aria-checked", "true");
  });

  // Die Kernaussage muss auch eingeklappt sichtbar sein — ohne sie versteht
  // niemand, warum man freiwillig die sparsamste Stufe wählt.
  it("states the core message without expanding anything", () => {
    renderWithQuery(<DeviceModeStrip device={makeDevice()} canControl />);

    expect(screen.queryByTestId("device-detail")).toBeNull();
    // „Measured:" davor — die Zahl darf nicht als Live-Wert dieser Box
    // gelesen werden (HONESTY RULE der Slot-Kachel).
    expect(
      screen.getByText(/^Measured: 20\.4 tok\/s on every step — what you save is power and heat\.$/),
    ).toBeInTheDocument();
  });

  it("draws the power staircase in the compact switch", () => {
    renderWithQuery(<DeviceModeStrip device={makeDevice()} canControl />);

    const power = ["eco+", "eco", "normal", "boost"].map(compactBarPct);
    expect(power[0]).toBeLessThan(power[1]);
    expect(power[1]).toBeLessThan(power[2]);
    expect(power[2]).toBeLessThan(power[3]);
    expect(power[3] - power[0]).toBeGreaterThan(30);
  });

  it("opens the full measurement on demand", async () => {
    const user = userEvent.setup();
    renderWithQuery(<DeviceModeStrip device={makeDevice()} canControl />);

    await user.click(screen.getByTestId("device-toggle-detail"));

    const detail = await screen.findByTestId("device-detail");
    // Das volle Diagramm samt Zahlenblock der gewählten Stufe
    expect(within(detail).getByTestId("bar-generation-eco")).toBeInTheDocument();
    expect(within(detail).getByText("32.5 W")).toBeInTheDocument();
    expect(within(detail).getByText("74 °C")).toBeInTheDocument();
    expect(within(detail).getByText(/Measured 16 Aug 2026/)).toBeInTheDocument();
  });

  // Die eigentliche Aussage des Diagramms: die Erzeugungs-Balken stehen über
  // alle vier Stufen gleich hoch, die Strom-Balken bilden eine Treppe. Ginge
  // das kaputt (z.B. andere Skala je Spalte), erzählte das Bild eine Lüge.
  it("draws generation flat across all modes and power as a staircase", async () => {
    const user = userEvent.setup();
    renderWithQuery(<DeviceModeStrip device={makeDevice()} canControl />);
    await user.click(screen.getByTestId("device-toggle-detail"));
    await screen.findByTestId("device-detail");

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
  // Balken, Treppe und Fortschritt ausschliesslich über transform laufen.
  it("animates through transform only, never through layout properties", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.nodes, "setDesiredState").mockResolvedValue(makeDevice());
    renderWithQuery(<DeviceModeStrip device={makeDevice()} canControl />);

    await user.click(screen.getByTestId("compact-mode-boost"));
    await user.click(screen.getByTestId("device-toggle-detail"));
    const block = await screen.findByTestId("device-control");

    for (const el of Array.from(block.querySelectorAll<HTMLElement>("*"))) {
      expect(el.style.transition).not.toMatch(/\b(width|height|padding|margin|top|left)\b/);
    }
    expect(screen.getByTestId("bar-power-boost").style.height).toBe("100%");
    expect(screen.getByTestId("bar-power-boost").style.transform).toMatch(/scaleY/);
    expect(screen.getByTestId("compact-bar-boost").style.transform).toMatch(/scaleX/);
  });

  it("sends the desired state and shows the hand-over while the box catches up", async () => {
    const user = userEvent.setup();
    const set = vi.spyOn(api.nodes, "setDesiredState").mockResolvedValue(makeDevice());
    renderWithQuery(<DeviceModeStrip device={makeDevice()} canControl />);

    await user.click(screen.getByTestId("compact-mode-eco+"));

    expect(set).toHaveBeenCalledWith("host-1", { gpu_mode: "eco+" });
    const pendingBox = await screen.findByTestId("device-pending");
    expect(within(pendingBox).getByText(/switching to eco\+/)).toBeInTheDocument();
    // Ziel-Umrandung neben dem gefüllten Ist-Reiter — beides gleichzeitig
    expect(screen.getByTestId("compact-target-outline")).toBeInTheDocument();
    expect(screen.getByTestId("compact-indicator")).toBeInTheDocument();
    // Ist bleibt bis zur Bestätigung durch das Gerät bei eco
    expect(screen.getByTestId("compact-mode-eco")).toHaveAttribute("aria-checked", "true");
  });

  // PUT ersetzt den Soll-Zustand vollständig. Schickte die Kachel nur
  // {gpu_mode}, löschte jeder Moduswechsel die Härtungs-Vorgaben — und der
  // Agent liesse sie ab dann in Ruhe, ohne dass es jemand merkt.
  it("keeps the other hardening settings when only the mode changes", async () => {
    const user = userEvent.setup();
    const set = vi.spyOn(api.nodes, "setDesiredState").mockResolvedValue(makeDevice());
    renderWithQuery(
      <DeviceModeStrip
        device={makeDevice({
          desired_state: { gpu_mode: "eco", oom_guard: true, latency_tune: true, mtu: 9000, min_free_kbytes: 5242880 },
        })}
        canControl
      />,
    );

    await user.click(screen.getByTestId("compact-mode-normal"));

    expect(set).toHaveBeenCalledWith("host-1", {
      gpu_mode: "normal",
      oom_guard: true,
      latency_tune: true,
      mtu: 9000,
      min_free_kbytes: 5242880,
    });
  });

  it("shows the hand-over for a desired state set elsewhere (no click here)", async () => {
    renderWithQuery(
      <DeviceModeStrip
        device={makeDevice({
          device_state: makeState({ gpu_mode: "boost" }),
          desired_state: { gpu_mode: "eco" },
          status: "yellow",
          reason: "pending",
          diff: ["gpu_mode"],
        })}
        canControl
      />,
    );

    expect(await screen.findByTestId("device-pending")).toBeInTheDocument();
    expect(screen.getByTestId("compact-target-outline")).toBeInTheDocument();
  });

  it("keeps quiet about the hand-over when desired and reported agree", () => {
    renderWithQuery(<DeviceModeStrip device={makeDevice()} canControl />);

    expect(screen.queryByTestId("device-pending")).toBeNull();
    expect(screen.queryByTestId("compact-target-outline")).toBeNull();
  });

  it("stays quiet about boost while a gentler step is chosen", () => {
    renderWithQuery(<DeviceModeStrip device={makeDevice()} canControl />);
    expect(screen.queryByTestId("device-boost-warning")).toBeNull();
  });

  it("warns about the risk once boost is the chosen step", () => {
    renderWithQuery(
      <DeviceModeStrip
        device={makeDevice({
          device_state: makeState({ gpu_mode: "boost" }),
          desired_state: { gpu_mode: "boost" },
        })}
        canControl
      />,
    );
    expect(screen.getByTestId("device-boost-warning")).toBeInTheDocument();
  });

  it("passes the box's own error through instead of swallowing it", () => {
    renderWithQuery(
      <DeviceModeStrip
        device={makeDevice({
          status: "red",
          reason: "last_error",
          last_error: "nvidia-smi: permission denied",
        })}
        canControl
      />,
    );

    expect(screen.getByTestId("device-last-error")).toHaveTextContent(
      "The box reports: nvidia-smi: permission denied",
    );
  });

  it("drops the target again when saving fails, so nobody waits for nothing", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.nodes, "setDesiredState").mockRejectedValue(new Error("API 500"));
    renderWithQuery(<DeviceModeStrip device={makeDevice()} canControl />);

    await user.click(screen.getByTestId("compact-mode-boost"));

    expect(await screen.findByTestId("device-apply-failed")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId("device-pending")).toBeNull());
  });

  // Eine frische Box hat weder Ist noch Soll. Ein hervorgehobener Reiter
  // würde eine Einstellung behaupten, die niemand gemacht hat.
  it("highlights nothing when neither a reading nor a target exists", () => {
    renderWithQuery(
      <DeviceModeStrip
        device={makeDevice({
          has_agent: false,
          status: "grey",
          reason: "no_agent",
          device_state: null,
          desired_state: null,
        })}
        canControl
      />,
    );

    expect(screen.queryByTestId("compact-indicator")).toBeNull();
    for (const m of ["eco+", "eco", "normal", "boost"]) {
      expect(screen.getByTestId(`compact-mode-${m}`)).toHaveAttribute("data-active", "false");
    }
    expect(screen.getByTestId("device-no-report")).toBeInTheDocument();
  });

  it("is read-only for non-admins", async () => {
    const set = vi.spyOn(api.nodes, "setDesiredState");
    renderWithQuery(<DeviceModeStrip device={makeDevice()} canControl={false} />);

    expect(screen.getByTestId("compact-mode-boost")).toBeDisabled();
    expect(screen.getByText("Only administrators can change the mode.")).toBeInTheDocument();
    expect(set).not.toHaveBeenCalled();
  });

  it("keeps the measured reference numbers exactly as the contract states them", () => {
    expect(MODE_FACTS.boost).toMatchObject({ clockMhz: null, tokensPerSec: 20.3, watt: 59.5, tempC: 87 });
    expect(MODE_FACTS.normal).toMatchObject({ clockMhz: 2200, tokensPerSec: 19.6, watt: 39.9, tempC: 81 });
    expect(MODE_FACTS.eco).toMatchObject({ clockMhz: 2000, tokensPerSec: 20.4, watt: 32.5, tempC: 74 });
    expect(MODE_FACTS["eco+"]).toMatchObject({ clockMhz: 1800, tokensPerSec: 19.8, watt: 27.1, tempC: 69 });
  });
});
