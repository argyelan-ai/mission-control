/**
 * HostAutostartRow — Marks Ein/Aus-Schalter je Box (Rezept-Umschalter P3).
 *
 * Abgedeckt:
 *   1. „an" zeigt das gemerkte Rezept im Untertitel
 *   2. Klick → PUT mit dem umgekehrten Wert + dem gemerkten Rezept, danach
 *      zeigt der Schalter den Zustand AUS DER ANTWORT (kein Optimistic-Update)
 *   3. Fehler beim Schalten bleibt als Satz stehen, der Zustand kippt nicht
 *   4. Worker-Box (via_head) bekommt einen Chip statt eines Schalters
 *   5. nicht lesbar → „unbekannt", Schalter gesperrt, kein PUT
 *   6. letzter Versuch wird als Satz gezeigt
 *   7. Nicht-Admins dürfen nicht schalten
 *
 * Sabotage-Probe (manuell gefahren): `disabled={unknown || busy || !isAdmin}`
 * auf `disabled={false}` gesetzt → Tests 5 und 7 fallen; `setQueryData` nach
 * dem PUT entfernt → Test 2 fällt.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HostAutostartRow } from "../HostAutostartRow";
import { api } from "@/lib/api";
import type { HostAutostartStatus } from "@/lib/types";

const mockStore = vi.hoisted(() => ({
  state: {
    currentUser: { id: "u1", email: "a@b.c", name: "Admin", role: "admin" } as
      | { id: string; email: string; name: string; role: string }
      | null,
  },
}));
vi.mock("@/lib/store", () => ({
  useAppStore: (selector: (s: typeof mockStore.state) => unknown) => selector(mockStore.state),
}));

function makeStatus(over: Partial<HostAutostartStatus> = {}): HostAutostartStatus {
  return {
    host_id: "id-box-a",
    enabled: false,
    recipe_slug: null,
    recipe_display_name: null,
    role: "head",
    via_head: null,
    last_attempt_at: null,
    last_result: null,
    ...over,
  };
}

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("HostAutostartRow", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    mockStore.state.currentUser = { id: "u1", email: "a@b.c", name: "Admin", role: "admin" };
  });

  it("switched on it names the recipe it will bring back", async () => {
    vi.spyOn(api.hosts, "autostart").mockResolvedValue(
      makeStatus({ enabled: true, recipe_slug: "recipe-x", recipe_display_name: "Recipe X" }),
    );
    renderWithQuery(<HostAutostartRow hostId="id-box-a" />);

    const sw = await screen.findByTestId("host-autostart-switch");
    await waitFor(() => expect(sw).toHaveAttribute("data-state", "on"));
    expect(sw).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("host-autostart-subtitle")).toHaveTextContent(
      "starts Recipe X after a failure or reboot",
    );
  });

  it("a click sends the PUT and then shows the state the server answered with", async () => {
    vi.spyOn(api.hosts, "autostart").mockResolvedValue(
      makeStatus({ enabled: false, recipe_slug: "recipe-x", recipe_display_name: "Recipe X" }),
    );
    const put = vi.spyOn(api.hosts, "setAutostart").mockResolvedValue(
      makeStatus({ enabled: true, recipe_slug: "recipe-x", recipe_display_name: "Recipe X" }),
    );
    renderWithQuery(<HostAutostartRow hostId="id-box-a" />);

    const sw = await screen.findByTestId("host-autostart-switch");
    await waitFor(() => expect(sw).toHaveAttribute("data-state", "off"));
    expect(screen.getByTestId("host-autostart-subtitle")).toHaveTextContent(
      "off — MC starts nothing on its own",
    );

    await userEvent.click(sw);
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("id-box-a", { enabled: true, recipe_slug: "recipe-x" }),
    );
    await waitFor(() => expect(sw).toHaveAttribute("data-state", "on"));
    expect(screen.getByTestId("host-autostart-subtitle")).toHaveTextContent(
      "starts Recipe X after a failure or reboot",
    );
  });

  it("a failed switch stays visible as a sentence and the state does not flip", async () => {
    vi.spyOn(api.hosts, "autostart").mockResolvedValue(makeStatus({ enabled: false }));
    vi.spyOn(api.hosts, "setAutostart").mockRejectedValue(
      new Error('API 422: {"detail":"Recipe recipe-x is not in the catalog"}'),
    );
    renderWithQuery(<HostAutostartRow hostId="id-box-a" />);

    const sw = await screen.findByTestId("host-autostart-switch");
    await waitFor(() => expect(sw).toHaveAttribute("data-state", "off"));
    await userEvent.click(sw);

    const err = await screen.findByTestId("host-autostart-error");
    expect(err).toHaveTextContent("Could not switch: Recipe recipe-x is not in the catalog");
    expect(err).not.toHaveTextContent("detail");
    expect(sw).toHaveAttribute("data-state", "off");
  });

  it("a worker box shows a chip pointing at its head instead of a switch", async () => {
    vi.spyOn(api.hosts, "autostart").mockResolvedValue(
      makeStatus({ role: "worker", via_head: { host_id: "id-box-a", slug: "box-a" } }),
    );
    const put = vi.spyOn(api.hosts, "setAutostart");
    renderWithQuery(<HostAutostartRow hostId="id-box-b" />);

    expect(await screen.findByTestId("host-autostart-via-head")).toHaveTextContent(
      "Autostart via head box-a",
    );
    expect(screen.queryByTestId("host-autostart-switch")).not.toBeInTheDocument();
    expect(put).not.toHaveBeenCalled();
  });

  it("unreadable state says unknown and cannot be switched", async () => {
    vi.spyOn(api.hosts, "autostart").mockRejectedValue(new Error("API 500: boom"));
    const put = vi.spyOn(api.hosts, "setAutostart");
    renderWithQuery(<HostAutostartRow hostId="id-box-a" />);

    const sw = await screen.findByTestId("host-autostart-switch");
    await waitFor(() => expect(sw).toHaveAttribute("data-state", "unknown"));
    expect(sw).toBeDisabled();
    expect(screen.getByTestId("host-autostart-subtitle")).toHaveTextContent(
      "state not readable",
    );
    await userEvent.click(sw, { pointerEventsCheck: 0 });
    expect(put).not.toHaveBeenCalled();
  });

  it("shows what happened on the last attempt", async () => {
    vi.spyOn(api.hosts, "autostart").mockResolvedValue(
      makeStatus({
        enabled: true,
        recipe_slug: "recipe-x",
        recipe_display_name: "Recipe X",
        last_attempt_at: "2026-09-04T16:02:00Z",
        last_result: "Failed: box not reachable",
      }),
    );
    renderWithQuery(<HostAutostartRow hostId="id-box-a" />);

    expect(await screen.findByTestId("host-autostart-last-attempt")).toHaveTextContent(
      "Failed: box not reachable",
    );
  });

  it("a non-admin sees the state but cannot switch it", async () => {
    mockStore.state.currentUser = { id: "u2", email: "v@b.c", name: "Viewer", role: "viewer" };
    vi.spyOn(api.hosts, "autostart").mockResolvedValue(makeStatus({ enabled: true }));
    const put = vi.spyOn(api.hosts, "setAutostart");
    renderWithQuery(<HostAutostartRow hostId="id-box-a" />);

    const sw = await screen.findByTestId("host-autostart-switch");
    await waitFor(() => expect(sw).toHaveAttribute("data-state", "on"));
    expect(sw).toBeDisabled();
    expect(sw).toHaveAttribute("title", "Only admins may switch autostart.");
    await userEvent.click(sw, { pointerEventsCheck: 0 });
    expect(put).not.toHaveBeenCalled();
  });
});
