import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Agent } from "@/lib/types";

// Task #20 (pre-chat) / Task B6 (chat rebuild): the Sessions page restores
// the last-viewed agent from localStorage, with ?agent=<id> (from the
// Agents list "open session" button) taking precedence.
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
vi.mock("@/components/shared/BrowserLiveView", () => ({
  BrowserLiveView: () => <div data-testid="browser-live-view-stub">Browser-Panel</div>,
}));

// TerminalPanel pulls in the real xterm/WebSocket/scaling machinery, all
// irrelevant to "does the panel rail switch which panel is shown" — a thin
// stub keeps this suite hermetic (that machinery is exercised elsewhere via
// TerminalPanel's own move-not-rewrite from the pre-chat page).
vi.mock("@/components/chat/TerminalPanel", async () => {
  const actual = await vi.importActual<typeof import("@/components/chat/TerminalPanel")>(
    "@/components/chat/TerminalPanel"
  );
  return {
    ...actual,
    TerminalPanel: ({ agent }: { agent: { name: string } }) => (
      <div data-testid="terminal-panel-stub">Terminal-Panel: {agent.name}</div>
    ),
  };
});

// ChatView owns its own transcript fetch/SSE plumbing (useChatStream) —
// unrelated to sidebar restore/persist behavior and would otherwise fire
// unmocked network calls (api.chat.history) plus reach for EventSource,
// which jsdom doesn't implement. A thin stub keeps this suite hermetic; the
// chat rendering itself (incl. the Chat/Terminal header toggle swapping
// content and no-transcript agents forcing terminal mode) is covered by
// ChatView.test.tsx. This stub surfaces just enough of `hasTranscript` and
// `centerView`/`onCenterViewChange` for the page-level tests below, which
// only care that sessions/page.tsx computes and wires those props/persists
// them correctly — not how ChatView renders once it has them.
vi.mock("@/components/chat/ChatView", () => ({
  ChatView: ({
    agent,
    hasTranscript,
    centerView,
    onCenterViewChange,
  }: {
    agent: { name: string } | null;
    hasTranscript: boolean;
    centerView: string;
    onCenterViewChange: (v: string) => void;
  }) => (
    <div data-testid="chat-view-stub">
      <span>{agent ? `Chat: ${agent.name}` : "Chat: none"}</span>
      <span data-testid="chat-view-has-transcript">{String(hasTranscript)}</span>
      <span data-testid="chat-view-center">{centerView}</span>
      <button
        type="button"
        onClick={() => onCenterViewChange(centerView === "chat" ? "terminal" : "chat")}
      >
        Toggle Center View
      </button>
    </div>
  ),
  DETAIL_LEVELS: [
    { key: "compact", label: "Kompakt" },
    { key: "normal", label: "Normal" },
    { key: "verbose", label: "Ausführlich" },
  ],
  CENTER_VIEWS: [
    { key: "chat", label: "Chat" },
    { key: "terminal", label: "Terminal" },
  ],
}));

import SessionsPage from "../page";

function mkAgent(
  overrides: Partial<Agent> & {
    container_state?: string;
    session_name?: string;
    session_running?: boolean;
    slug?: string | null;
  } = {}
): Agent & {
  container_state: string;
  session_name: string;
  session_running: boolean;
  slug?: string | null;
} {
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
    session_name: "agent-one-host",
    session_running: false,
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

// The agent's name can legitimately appear twice (the desktop sidebar's
// list item AND the mobile sheet's collapsed-toggle label, both mounted
// regardless of viewport in jsdom) — this resolves to the actual
// `role="option"` row, the one carrying `aria-selected`.
function findOptionRow(name: string): HTMLElement {
  const matches = screen.getAllByText(name);
  const row = matches
    .map((el) => el.closest('[role="option"]'))
    .find((el): el is HTMLElement => el !== null);
  if (!row) throw new Error(`No [role="option"] row found for "${name}"`);
  return row;
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

    await screen.findAllByText("Agent Two");
    const selectedRow = findOptionRow("Agent Two");
    expect(selectedRow).toHaveAttribute("aria-selected", "true");
    expect(findOptionRow("Agent One")).toHaveAttribute("aria-selected", "false");
  });

  it("prefers ?agent=<id> over the stored value", async () => {
    // agent-1 is both the stored value AND the plain fallback (agents[0]) —
    // using it here wouldn't prove the query param path fired at all.
    // agent-2 has neither in its favor, so only the param can select it.
    localStorage.setItem("mc-sessions-last-agent", "agent-1");
    nav.searchParamsString = "agent=agent-2";
    renderPage();

    await screen.findAllByText("Agent Two");
    expect(findOptionRow("Agent Two")).toHaveAttribute("aria-selected", "true");
    expect(findOptionRow("Agent One")).toHaveAttribute("aria-selected", "false");
  });

  it("persists the selection to localStorage after picking an agent from the list", async () => {
    renderPage();
    await screen.findAllByText("Agent Two");
    const row = findOptionRow("Agent Two");
    fireEvent.click(row);

    await waitFor(() => expect(localStorage.getItem("mc-sessions-last-agent")).toBe("agent-2"));
  });

  it("shows the mocked chat view for the selected agent", async () => {
    localStorage.setItem("mc-sessions-last-agent", "agent-2");
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("chat-view-stub")).toHaveTextContent("Chat: Agent Two")
    );
  });
});

describe("SessionsPage — panel rail switches the panel slot's content (Diff/Browser only)", () => {
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
    ]);
    vi.spyOn(api.agents, "listHostSessions").mockResolvedValue([]);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("no panel is open by default, and Terminal is no longer one of the rail's options", async () => {
    renderPage();
    await screen.findAllByText("Agent One");

    expect(screen.queryByRole("button", { name: "Terminal" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("browser-live-view-stub")).not.toBeInTheDocument();
    expect(screen.queryByText("Diff-Ansicht kommt in Teil 3.")).not.toBeInTheDocument();
  });

  it("selecting Browser opens the browser panel", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByText("Agent One");

    await user.click(screen.getByRole("button", { name: "Browser" }));
    expect(await screen.findByTestId("browser-live-view-stub")).toBeInTheDocument();
  });

  it("switching from Diff to Browser replaces the panel content (not both)", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByText("Agent One");

    await user.click(screen.getByRole("button", { name: "Diff" }));
    await screen.findByText("Diff-Ansicht kommt in Teil 3.");

    await user.click(screen.getByRole("button", { name: "Browser" }));
    expect(await screen.findByTestId("browser-live-view-stub")).toBeInTheDocument();
    expect(screen.queryByText("Diff-Ansicht kommt in Teil 3.")).not.toBeInTheDocument();
  });

  it("clicking the already-active panel icon collapses the panel", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByText("Agent One");

    await user.click(screen.getByRole("button", { name: "Diff" }));
    await screen.findByText("Diff-Ansicht kommt in Teil 3.");

    await user.click(screen.getByRole("button", { name: "Diff" }));
    expect(screen.queryByText("Diff-Ansicht kommt in Teil 3.")).not.toBeInTheDocument();
  });
});

describe("SessionsPage — center view (Chat/Terminal) wiring and hasTranscript derivation", () => {
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
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("defaults centerView to 'chat' and persists a change to mc.chat.view", async () => {
    vi.spyOn(api.agents, "listDockerSessions").mockResolvedValue([
      mkAgent({ id: "agent-1", name: "Agent One" }),
    ]);
    vi.spyOn(api.agents, "listHostSessions").mockResolvedValue([]);
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByTestId("chat-view-center")).toHaveTextContent("chat");

    await user.click(screen.getByRole("button", { name: "Toggle Center View" }));
    expect(screen.getByTestId("chat-view-center")).toHaveTextContent("terminal");
    await waitFor(() => expect(localStorage.getItem("mc.chat.view")).toBe("terminal"));
  });

  it("restores a persisted mc.chat.view=terminal on load", async () => {
    vi.spyOn(api.agents, "listDockerSessions").mockResolvedValue([
      mkAgent({ id: "agent-1", name: "Agent One" }),
    ]);
    vi.spyOn(api.agents, "listHostSessions").mockResolvedValue([]);
    localStorage.setItem("mc.chat.view", "terminal");
    renderPage();

    expect(await screen.findByTestId("chat-view-center")).toHaveTextContent("terminal");
  });

  it("passes hasTranscript=true for a cli-bridge agent", async () => {
    vi.spyOn(api.agents, "listDockerSessions").mockResolvedValue([
      mkAgent({ id: "agent-1", name: "Agent One", agent_runtime: "cli-bridge" }),
    ]);
    vi.spyOn(api.agents, "listHostSessions").mockResolvedValue([]);
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("chat-view-has-transcript")).toHaveTextContent("true")
    );
  });

  it("passes hasTranscript=false for a non-Boss host agent (Hermes/Jarvis) — ChatView forces terminal mode from this", async () => {
    vi.spyOn(api.agents, "listDockerSessions").mockResolvedValue([]);
    vi.spyOn(api.agents, "listHostSessions").mockResolvedValue([
      mkAgent({ id: "agent-1", name: "Hermes", agent_runtime: "host", slug: "hermes" }),
    ]);
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("chat-view-has-transcript")).toHaveTextContent("false")
    );
  });

  it("passes hasTranscript=true for the Boss host agent", async () => {
    vi.spyOn(api.agents, "listDockerSessions").mockResolvedValue([]);
    vi.spyOn(api.agents, "listHostSessions").mockResolvedValue([
      mkAgent({ id: "agent-1", name: "Boss", agent_runtime: "host", slug: "boss" }),
    ]);
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("chat-view-has-transcript")).toHaveTextContent("true")
    );
  });
});
