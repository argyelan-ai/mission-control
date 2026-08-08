import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Agent } from "@/lib/types";

// Task #20: the Sessions page restores the last-viewed agent from
// localStorage, with ?agent=<id> (from the Agents list "open session"
// button) taking precedence.
const nav = vi.hoisted(() => ({ searchParamsString: "" }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(nav.searchParamsString),
}));

// AppShell (auth guard, Sidebar, TopBar, CommandPalette, VoiceProvider, …)
// is unrelated to the restore/persist behavior under test — mocking it out
// keeps this test focused on SessionsPage's own logic instead of AppShell's
// auth/localStorage/SSE dependency chain.
vi.mock("@/components/layout/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

// useTerminalRemountSignal opens a real EventSource per selected agent —
// irrelevant here, and jsdom has no EventSource implementation.
vi.mock("@/hooks/useTerminalRemountSignal", () => ({
  useTerminalRemountSignal: () => {},
}));

vi.mock("@xterm/xterm", () => ({ Terminal: class {} }));
vi.mock("@/components/shared/BrowserLiveView", () => ({ BrowserLiveView: () => null }));

import SessionsPage from "../page";

function mkAgent(overrides: Partial<Agent> & { container_state?: string } = {}): Agent & { container_state: string } {
  return {
    id: "agent-1",
    board_id: null,
    name: "Agent One",
    role: null,
    emoji: null,
    status: "idle",
    model: null,
    secret_id: null,
    is_board_lead: false,
    heartbeat_config: { interval: "5m", target: "boss" },
    skills: [],
    skill_filter: null,
    cli_plugins: null,
    cli_skills: null,
    mcp_servers: null,
    scopes: [],
    identity_md: null,
    soul_md: null,
    tools_md: null,
    heartbeat_md: null,
    rules_md: null,
    memory_md: null,
    last_seen_at: null,
    last_task_activity_at: null,
    current_task_id: null,
    context_tokens: 0,
    context_max: 200000,
    session_message_count: 0,
    total_tasks_completed: 0,
    total_compactions: 0,
    template_id: null,
    workspace_path: null,
    provision_status: "local",
    provisioned_at: null,
    archived_at: null,
    discord_channel_id: null,
    discord_channel_name: null,
    last_trigger_at: null,
    last_dispatch_error: null,
    run_state: "idle",
    operational_mode: "active",
    agent_runtime: "cli-bridge",
    runtime_id: null,
    pending_runtime_sync: false,
    harness: null,
    runtime_switchable: false,
    runtime_switch_blocked_reason: null,
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    container_state: "exited",
    ...overrides,
  };
}

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <SessionsPage />
    </QueryClientProvider>
  );
}

describe("SessionsPage — last-selected-agent restore", () => {
  // jsdom in this environment has no working localStorage (every other
  // *.test.tsx in the repo hits the same gap and stubs its own — see
  // TasksPage.test.tsx) — a plain in-memory Storage shim, reset per test.
  let store: Record<string, string>;

  beforeEach(() => {
    nav.searchParamsString = "";

    store = {};
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: {
        getItem: (k: string) => store[k] ?? null,
        setItem: (k: string, v: string) => { store[k] = v; },
        removeItem: (k: string) => { delete store[k]; },
        clear: () => { store = {}; },
        length: 0,
        key: () => null,
      },
    });

    vi.spyOn(api.agents, "listDockerSessions").mockResolvedValue([
      mkAgent({ id: "agent-1", name: "Agent One" }),
      mkAgent({ id: "agent-2", name: "Agent Two" }),
    ]);
    vi.spyOn(api.agents, "listHostSessions").mockResolvedValue([]);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("restores the agent stored under mc-sessions-last-agent when no query param is present", async () => {
    localStorage.setItem("mc-sessions-last-agent", "agent-2");
    renderPage();

    // Selected row's name renders in the primary text color; unselected rows
    // use the muted secondary color (see AgentList row styling in page.tsx).
    const selectedName = await screen.findByText("Agent Two");
    expect(selectedName).toHaveStyle({ color: "var(--color-text-primary)" });
    const otherName = screen.getByText("Agent One");
    expect(otherName).not.toHaveStyle({ color: "var(--color-text-primary)" });
  });

  it("prefers ?agent=<id> over the stored value", async () => {
    // agent-1 is both the stored value AND the plain fallback (agents[0]) —
    // using it here wouldn't prove the query param path fired at all.
    // agent-2 has neither in its favor, so only the param can select it.
    localStorage.setItem("mc-sessions-last-agent", "agent-1");
    nav.searchParamsString = "agent=agent-2";
    renderPage();

    const selectedName = await screen.findByText("Agent Two");
    expect(selectedName).toHaveStyle({ color: "var(--color-text-primary)" });
    const otherName = screen.getByText("Agent One");
    expect(otherName).not.toHaveStyle({ color: "var(--color-text-primary)" });
  });

  it("persists the selection to localStorage after picking an agent from the list", async () => {
    renderPage();
    const row = await screen.findByText("Agent Two");
    fireEvent.click(row);

    await waitFor(() => expect(localStorage.getItem("mc-sessions-last-agent")).toBe("agent-2"));
  });
});
