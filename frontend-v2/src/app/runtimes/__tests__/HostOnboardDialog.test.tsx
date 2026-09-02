import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HostOnboardDialog } from "../HostOnboardDialog";
import { api } from "@/lib/api";
import type { Credential, HostOnboardLog } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const makeCredential = (over: Partial<Credential> = {}): Credential => ({
  id: "cred-1",
  name: "box-c SSH key",
  credential_type: "ssh_key",
  data_masked: { username: "mcfleet", public_key: "ssh-ed25519 AAAA", private_key_pem: "[hidden]" },
  url: null,
  notes: null,
  created_at: "2026-08-30T00:00:00Z",
  updated_at: "2026-08-30T00:00:00Z",
  ...over,
});

describe("HostOnboardDialog", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("disables Start until address/username/password are filled", async () => {
    renderWithQuery(<HostOnboardDialog open onClose={vi.fn()} />);
    expect(screen.getByTestId("onboard-start")).toBeDisabled();

    await userEvent.type(screen.getByTestId("onboard-address"), "192.0.2.50");
    expect(screen.getByTestId("onboard-start")).toBeDisabled();

    await userEvent.type(screen.getByTestId("onboard-username"), "mcfleet");
    expect(screen.getByTestId("onboard-start")).toBeDisabled();

    await userEvent.type(screen.getByTestId("onboard-password"), "hunter2");
    expect(screen.getByTestId("onboard-start")).toBeEnabled();
  });

  it("switching auth method swaps the visible credential field", async () => {
    renderWithQuery(<HostOnboardDialog open onClose={vi.fn()} />);
    expect(screen.getByTestId("onboard-password")).toBeInTheDocument();

    await userEvent.click(screen.getByText("Private key"));
    expect(screen.queryByTestId("onboard-password")).toBeNull();
    expect(screen.getByTestId("onboard-private-key")).toBeInTheDocument();

    await userEvent.click(screen.getByText("Existing credential"));
    expect(screen.queryByTestId("onboard-private-key")).toBeNull();
  });

  it("starts a run, polls the log, and shows the done status", async () => {
    vi.spyOn(api.hosts, "onboard").mockResolvedValue({ job_id: "job-1" });
    const logSpy = vi.spyOn(api.hosts, "onboardLog");
    logSpy.mockResolvedValueOnce({
      job_id: "job-1", status: "running", phase: "connect", message: null,
      running: true, cursor: 1,
      lines: [{ ts: 1, level: "info", text: "Verbinde zu mcfleet@192.0.2.50 …" }],
    } as HostOnboardLog);
    logSpy.mockResolvedValueOnce({
      job_id: "job-1", status: "done", phase: "done", message: "Onboarding von 'box-c' abgeschlossen.",
      running: false, cursor: 2,
      lines: [{ ts: 2, level: "info", text: "Zugang im Vault gespeichert." }],
    } as HostOnboardLog);

    renderWithQuery(<HostOnboardDialog open onClose={vi.fn()} />);
    await userEvent.type(screen.getByTestId("onboard-address"), "192.0.2.50");
    await userEvent.type(screen.getByTestId("onboard-username"), "mcfleet");
    await userEvent.type(screen.getByTestId("onboard-password"), "hunter2");
    await userEvent.click(screen.getByTestId("onboard-start"));

    expect(api.hosts.onboard).toHaveBeenCalledWith({
      address: "192.0.2.50",
      username: "mcfleet",
      auth: { password: "hunter2" },
      display_name: null,
      bootstrap: true,
      install_agent: true,
      // P2: hosts.list nicht gemockt → Bestand unbekannt → kein stiller Vorschlag
      role: null,
    });

    expect(await screen.findByText("Verbinde zu mcfleet@192.0.2.50 …")).toBeInTheDocument();
    // The dialog polls every 2s (real timers) — the second, distinct mock
    // response only lands on that second tick, so the default 1s waitFor
    // window isn't enough here.
    await waitFor(
      () => expect(screen.getByTestId("onboard-status")).toHaveTextContent("Set up successfully"),
      { timeout: 4000 }
    );
    expect(screen.getByTestId("onboard-status")).toHaveTextContent("Onboarding von 'box-c' abgeschlossen.");
    expect(screen.getByTestId("onboard-done")).toBeInTheDocument();
  }, 10000);

  // ── P2: Geräterolle ────────────────────────────────────────────────────────

  it("P2: suggests 'worker' when a box exists and sends the clicked role", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([{ id: "host-a", slug: "box-a" } as never]);
    vi.spyOn(api.hosts, "onboard").mockResolvedValue({ job_id: "job-3" });
    vi.spyOn(api.hosts, "onboardLog").mockResolvedValue({
      job_id: "job-3", status: "running", phase: "connect", message: null, running: true, cursor: 0, lines: [],
    } as HostOnboardLog);

    renderWithQuery(<HostOnboardDialog open onClose={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByTestId("onboard-role-worker")).toHaveAttribute("aria-checked", "true"),
    );
    expect(screen.getByTestId("onboard-role-suggested")).toBeInTheDocument();

    await userEvent.type(screen.getByTestId("onboard-address"), "192.0.2.51");
    await userEvent.type(screen.getByTestId("onboard-username"), "operator");
    await userEvent.type(screen.getByTestId("onboard-password"), "secret");
    await userEvent.click(screen.getByTestId("onboard-start"));
    expect(api.hosts.onboard).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.hosts.onboard).mock.calls[0][0]).toMatchObject({ role: "worker" });
  });

  it("P2: one click flips the suggestion to 'head' — and the list must not overwrite it", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([{ id: "host-a", slug: "box-a" } as never]);
    vi.spyOn(api.hosts, "onboard").mockResolvedValue({ job_id: "job-4" });
    vi.spyOn(api.hosts, "onboardLog").mockResolvedValue({
      job_id: "job-4", status: "running", phase: "connect", message: null, running: true, cursor: 0, lines: [],
    } as HostOnboardLog);

    renderWithQuery(<HostOnboardDialog open onClose={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByTestId("onboard-role-worker")).toHaveAttribute("aria-checked", "true"),
    );
    await userEvent.click(screen.getByTestId("onboard-role-head"));
    expect(screen.queryByTestId("onboard-role-suggested")).toBeNull();

    await userEvent.type(screen.getByTestId("onboard-address"), "192.0.2.51");
    await userEvent.type(screen.getByTestId("onboard-username"), "operator");
    await userEvent.type(screen.getByTestId("onboard-password"), "secret");
    await userEvent.click(screen.getByTestId("onboard-start"));
    expect(vi.mocked(api.hosts.onboard).mock.calls[0][0]).toMatchObject({ role: "head" });
  });

  it("shows the actionable message on auth_failed", async () => {
    vi.spyOn(api.hosts, "onboard").mockResolvedValue({ job_id: "job-2" });
    vi.spyOn(api.hosts, "onboardLog").mockResolvedValue({
      job_id: "job-2", status: "auth_failed", phase: "auth_failed",
      message: "SSH-Login fehlgeschlagen (falsches Passwort/Key?): bad password",
      running: false, cursor: 1,
      lines: [{ ts: 1, level: "error", text: "SSH-Login fehlgeschlagen" }],
    } as HostOnboardLog);

    renderWithQuery(<HostOnboardDialog open onClose={vi.fn()} />);
    await userEvent.type(screen.getByTestId("onboard-address"), "192.0.2.50");
    await userEvent.type(screen.getByTestId("onboard-username"), "mcfleet");
    await userEvent.type(screen.getByTestId("onboard-password"), "wrong");
    await userEvent.click(screen.getByTestId("onboard-start"));

    await waitFor(() => expect(screen.getByTestId("onboard-status")).toHaveTextContent("Login failed"));
    expect(screen.getByTestId("onboard-status")).toHaveTextContent("bad password");
  }, 10000);

  it("lists only ssh_key credentials for the existing-credential option", async () => {
    vi.spyOn(api.credentials, "list").mockResolvedValue([
      makeCredential({ id: "cred-1", name: "box-c key" }),
      { ...makeCredential({ id: "cred-2", name: "Some login" }), credential_type: "login" },
    ]);

    renderWithQuery(<HostOnboardDialog open onClose={vi.fn()} />);
    await userEvent.click(screen.getByText("Existing credential"));

    expect(await screen.findByText("box-c key")).toBeInTheDocument();
    expect(screen.queryByText("Some login")).toBeNull();
  });
});
