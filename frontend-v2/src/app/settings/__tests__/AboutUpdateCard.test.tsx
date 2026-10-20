/**
 * Settings → About carries the update card only for admins and only while
 * the backend's feature switch is on (update plan PR 5, sabotage probe (b):
 * not an admin → no card, and the update API is not even asked).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), back: vi.fn() }),
  usePathname: () => "/settings",
  useSearchParams: () => new URLSearchParams("section=about"),
}));
vi.mock("@/components/layout/AppShell", () => ({
  __esModule: true,
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

const state = vi.hoisted(() => ({
  currentUser: { id: "u1", email: "a@b.com", name: "Operator", role: "member" } as {
    id: string; email: string; name: string; role: string;
  },
}));
vi.mock("@/lib/store", () => ({
  useAppStore: (selector?: (s: typeof state) => unknown) => (selector ? selector(state) : state),
}));

const m = vi.hoisted(() => ({ status: vi.fn(), version: vi.fn() }));
vi.mock("@/lib/api", async (orig) => {
  const real = (await orig()) as { api: Record<string, unknown> };
  const system = real.api.system as Record<string, unknown>;
  return {
    ...real,
    api: {
      ...real.api,
      system: {
        ...system,
        version: m.version,
        update: {
          ...(system.update as object),
          status: m.status,
          history: vi.fn().mockResolvedValue({ runs: [] }),
        },
      },
      auth: {
        ...(real.api.auth as object),
        stepUpAvailability: vi.fn().mockResolvedValue({ available: true, reason: null, ttl_seconds: 300 }),
      },
    },
  };
});

import SettingsPage from "../page";

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SettingsPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  m.status.mockReset();
  m.version.mockResolvedValue({
    current: "0.9.2", latest: "v0.9.3", release_url: "https://example.invalid/r", update_available: true,
  });
});

describe("About → update card", () => {
  it("a member sees no card and the update API is never asked", async () => {
    state.currentUser = { ...state.currentUser, role: "member" };
    renderPage();
    await waitFor(() => expect(screen.getByText("Update available: v0.9.3")).toBeInTheDocument());
    expect(screen.queryByTestId("update-card")).toBeNull();
    expect(m.status).not.toHaveBeenCalled();
  });

  it("an admin with the switch off sees today's About — no card", async () => {
    state.currentUser = { ...state.currentUser, role: "admin" };
    m.status.mockRejectedValue(new Error('API 404: {"detail":{"code":"update_disabled"}}'));
    renderPage();
    await waitFor(() => expect(m.status).toHaveBeenCalled());
    expect(await screen.findByText("Update available: v0.9.3")).toBeInTheDocument();
    expect(screen.queryByTestId("update-card")).toBeNull();
  });

  it("an admin with the switch on gets the card, and the old line goes (said once)", async () => {
    state.currentUser = { ...state.currentUser, role: "admin" };
    m.status.mockResolvedValue({
      drain: { active: false, run_id: null, expires_at: null },
      running_work: { tasks: 0, group_rounds: 0 },
      bridge_reachable: true, stale: false, run: null,
    });
    renderPage();
    expect(await screen.findByTestId("update-card")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Update available: v0.9.3")).toBeNull());
  });
});
