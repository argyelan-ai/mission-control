import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useAppStore, type AuthUser } from "@/lib/store";
import type { Runtime, RuntimesLiveResponse } from "@/lib/types";

// Same shell mock as page.test.tsx — the empty views are page logic, not shell.
vi.mock("@/components/layout/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

import RuntimesPage from "../page";

function makeRuntime(over: Partial<Runtime>): Runtime {
  return {
    id: over.slug ?? "rt", slug: "rt", display_name: "RT",
    runtime_type: "cloud", provider: "anthropic",
    endpoint: "", healthcheck_path: "/health",
    container_name: null, role_tags: [], supports_tools: true,
    supports_reasoning: false, supports_streaming: true,
    preferred_context_len: 8192, max_context_len: 32768,
    gpu_profile: "default", memory_notes: "", startup_notes: "",
    ui_order: 0, enabled: true, state: "unknown",
    ...over,
  };
}

const emptyLive: RuntimesLiveResponse = { live: {}, watcher_enabled: true, interval: 30 };

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <RuntimesPage />
    </QueryClientProvider>
  );
}

describe("RuntimesPage — empty views (fresh install)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    useAppStore.setState({ currentUser: { id: "u1", role: "admin" } as AuthUser });
    vi.spyOn(api.runtimes, "liveStatus").mockResolvedValue(emptyLive);
    vi.spyOn(api.runtimes.db, "agents").mockResolvedValue({ runtime_slug: "rt", count: 0, agents: [] });
    vi.spyOn(api.hosts, "list").mockResolvedValue([]);
    vi.spyOn(api.lmstudio, "list").mockResolvedValue({ models: [], reachable: true });
    vi.spyOn(api.lmstudio, "downloads").mockResolvedValue({ downloads: [] });
  });

  it("0 runtimes: Fleet says what belongs there and its button opens the existing Add-device chooser", async () => {
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [] });
    renderPage();

    const fleet = await screen.findByTestId("runtimes-empty-fleet");
    expect(within(fleet).getByText(/computers that run your own models/i)).toBeInTheDocument();

    await act(async () => {
      within(fleet).getByRole("button", { name: "Add device" }).click();
    });
    // Same chooser the Infrastructure tab's "Add device" opens — no second path.
    await waitFor(() => expect(screen.getByTestId("page-tab-infra")).toHaveAttribute("aria-selected", "true"));
    expect(await screen.findByRole("heading", { name: "Add device" })).toBeInTheDocument();
  });

  it("0 runtimes: Fleet's secondary action opens the '+' add-runtime dialog", async () => {
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [] });
    renderPage();

    const fleet = await screen.findByTestId("runtimes-empty-fleet");
    await act(async () => {
      within(fleet).getByRole("button", { name: "Connect by address" }).click();
    });
    expect(await screen.findByText(/Paste a base URL/)).toBeInTheDocument();
  });

  it("0 runtimes: Cloud says what belongs there and leads to the provider catalog", async () => {
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [] });
    renderPage();

    await act(async () => {
      (await screen.findByTestId("page-tab-cloud")).click();
    });
    const cloud = await screen.findByTestId("runtimes-empty-cloud");
    expect(within(cloud).getByText(/hosted model APIs/i)).toBeInTheDocument();

    await act(async () => {
      within(cloud).getByRole("button", { name: "Pick a provider model" }).click();
    });
    await waitFor(() => expect(screen.getByTestId("page-tab-models")).toHaveAttribute("aria-selected", "true"));
    expect(await screen.findByTestId("models-tab-providers")).toHaveAttribute("aria-selected", "true");
  });

  it("fresh-install shape (cloud anchors only, no hosts): Fleet is not a blank area, Cloud shows no empty view", async () => {
    vi.spyOn(api.runtimes, "list").mockResolvedValue({
      runtimes: [makeRuntime({ slug: "opus", display_name: "Claude Opus" })],
    });
    renderPage();

    expect(await screen.findByTestId("runtimes-empty-fleet")).toBeInTheDocument();
    await act(async () => {
      screen.getByTestId("page-tab-cloud").click();
    });
    expect(screen.queryByTestId("runtimes-empty-cloud")).not.toBeInTheDocument();
  });

  it("a non-admin sees the explanation but no Add-device button (the chooser is admin-only)", async () => {
    useAppStore.setState({ currentUser: { id: "u2", role: "member" } as AuthUser });
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [] });
    renderPage();

    const fleet = await screen.findByTestId("runtimes-empty-fleet");
    expect(within(fleet).queryByRole("button", { name: "Add device" })).not.toBeInTheDocument();
  });
});
