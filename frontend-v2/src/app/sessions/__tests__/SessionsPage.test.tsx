import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { HeadRun } from "@/lib/heads";
import type { Agent } from "@/lib/types";

// Task #20 (pre-chat) / Task B6 (chat rebuild): the Sessions page restores
// the last-viewed agent from localStorage, with ?agent=<id> (from the
// Agents list "open session" button) taking precedence.
const nav = vi.hoisted(() => ({
  searchParamsString: "",
  replaced: [] as string[],
  pushed: [] as string[],
  backs: 0,
}));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(nav.searchParamsString),
  usePathname: () => "/sessions",
  // Like Next: replace()/push() change what useSearchParams reports.
  useRouter: () => ({
    replace: (url: string) => { nav.replaced.push(url); nav.searchParamsString = url.split("?")[1] ?? ""; },
    push: (url: string) => { nav.pushed.push(url); nav.searchParamsString = url.split("?")[1] ?? ""; },
    back: () => { nav.backs += 1; },
  }),
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

// DiffPanel owns its own TanStack Query fetch (api.chat.diff) — unrelated to
// "does the panel rail switch which panel is shown," which is what this
// suite tests. A thin stub keeps it hermetic; DiffPanel's own scope-switch/
// polling/empty-state behavior is covered by DiffPanel.test.tsx.
vi.mock("@/components/chat/DiffPanel", () => ({
  DiffPanel: ({ agentId }: { agentId: string }) => (
    <div data-testid="diff-panel-stub">Diff-Panel: {agentId}</div>
  ),
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
    head,
    hasTranscript,
    centerView,
    onCenterViewChange,
    onBack,
    contextLine,
  }: {
    agent: { name: string } | null;
    head?: { run_id: string; title: string | null } | null;
    hasTranscript: boolean;
    centerView: string;
    onCenterViewChange: (v: string) => void;
    onBack?: () => void;
    contextLine?: string | null;
  }) => (
    <div data-testid="chat-view-stub">
      {/* Heads in Chats (PR 2): the page's own `selectedHeadRun` resolution
          (recent list vs. the one-off fallback fetch) is observable here. */}
      <span data-testid="chat-view-head">{head ? `Head: ${head.run_id}` : ""}</span>
      <span>{agent ? `Chat: ${agent.name}` : "Chat: none"}</span>
      <span data-testid="chat-view-has-transcript">{String(hasTranscript)}</span>
      <span data-testid="chat-view-center">{centerView}</span>
      <span data-testid="chat-view-context-line">{contextLine ?? ""}</span>
      {/* The agent object the page hands down — the context line, the
          transcript gate and TerminalPanel all read from it, so whether it is
          the live row or a stale click-time snapshot is observable here. */}
      <span data-testid="chat-view-agent-task">{(agent as { current_task_id?: string | null })?.current_task_id ?? ""}</span>
      <span data-testid="chat-view-agent-status">{(agent as { status?: string })?.status ?? ""}</span>
      <button
        type="button"
        onClick={() => onCenterViewChange(centerView === "chat" ? "terminal" : "chat")}
      >
        Toggle Center View
      </button>
      {/* The real back chevron lives in ChatView's header (covered by
          ChatView.test.tsx). What the PAGE owes is a reaction to `onBack`:
          returning to the list screen. This stands in for that trigger. */}
      {onBack && (
        <button type="button" onClick={onBack}>
          Stub Back
        </button>
      )}
    </div>
  ),
  // Gleiche Form wie chatOptions.ts: Katalog-Schluessel, keine fertigen
  // Beschriftungen — die Seite liest ohnehin nur `.key`.
  DETAIL_LEVELS: [
    { key: "compact", labelKey: "detailCompact" },
    { key: "normal", labelKey: "detailNormal" },
    { key: "verbose", labelKey: "detailVerbose" },
  ],
  CENTER_VIEWS: [
    { key: "chat", labelKey: "chat" },
    { key: "terminal", labelKey: "terminal" },
  ],
}));

import SessionsPage from "../page";
import { C } from "@/lib/colors";

// Tokens are CSS variables (ADR-087): jsdom keeps the specified inline value,
// so assertions compare against the token from lib/colors.ts directly.

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
    role_canonical: null,
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

// Returns the QueryClient alongside the render result so a test can force a
// refetch deterministically instead of waiting out `refetchInterval`.
function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return {
    ...render(
      <QueryClientProvider client={qc}>
        <SessionsPage />
      </QueryClientProvider>
    ),
    qc,
  };
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
    expect(screen.queryByTestId("diff-panel-stub")).not.toBeInTheDocument();
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
    await screen.findByTestId("diff-panel-stub");

    await user.click(screen.getByRole("button", { name: "Browser" }));
    expect(await screen.findByTestId("browser-live-view-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("diff-panel-stub")).not.toBeInTheDocument();
  });

  it("clicking the already-active panel icon collapses the panel", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByText("Agent One");

    await user.click(screen.getByRole("button", { name: "Diff" }));
    await screen.findByTestId("diff-panel-stub");

    await user.click(screen.getByRole("button", { name: "Diff" }));
    expect(screen.queryByTestId("diff-panel-stub")).not.toBeInTheDocument();
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

  // Ein Agent mit `headless_chat` faehrt ueber ACP und schreibt in dieselbe
  // omp-Transkript-Ablage (Spec docs/specs/chat-over-acp.md) — der Chat ist
  // dort die EINZIGE Oberflaeche. Faengt ihn die Host-Regel als
  // „kein Transkript" ab, steht er stumm im Terminal, das nichts faehrt.
  it("passes hasTranscript=true for a headless (ACP) host agent", async () => {
    vi.spyOn(api.agents, "listDockerSessions").mockResolvedValue([]);
    vi.spyOn(api.agents, "listHostSessions").mockResolvedValue([
      mkAgent({ id: "agent-1", name: "acp-one", agent_runtime: "host", slug: "acp-one", headless_chat: true }),
    ]);
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("chat-view-has-transcript")).toHaveTextContent("true")
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

describe("SessionsPage — sidebar collapse (mc.chat.sidebar)", () => {
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

  it("defaults to open, collapses on chevron click, persists to mc.chat.sidebar, and restores on expand", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByText("Agent One");
    // Scoped to the desktop rail instance — the mobile sheet is a separate,
    // always-mounted SessionSidebar (jsdom ignores its `md:hidden` class)
    // and legitimately renders "Agent One" too, in its own toggle label.
    const desktopSidebar = screen.getByTestId("sidebar-desktop");

    // Open by default: the row's name is visible in the desktop rail.
    expect(within(desktopSidebar).getByRole("option", { name: "Agent One" })).toBeInTheDocument();

    await user.click(within(desktopSidebar).getByRole("button", { name: "Seitenleiste einklappen" }));

    // Collapsed: name text is gone, row is icon-only but still an
    // accessible option (title attribute carries the name).
    expect(within(desktopSidebar).queryByText("Agent One")).not.toBeInTheDocument();
    expect(within(desktopSidebar).getByRole("option", { name: "Agent One" })).toBeInTheDocument();
    await waitFor(() => expect(localStorage.getItem("mc.chat.sidebar")).toBe("collapsed"));

    await user.click(within(desktopSidebar).getByRole("button", { name: "Seitenleiste ausklappen" }));
    expect(await within(desktopSidebar).findByText("Agent One")).toBeInTheDocument();
    await waitFor(() => expect(localStorage.getItem("mc.chat.sidebar")).toBe("open"));
  });

  it("restores a persisted mc.chat.sidebar=collapsed on load", async () => {
    localStorage.setItem("mc.chat.sidebar", "collapsed");
    renderPage();

    const desktopSidebar = screen.getByTestId("sidebar-desktop");
    await waitFor(() =>
      expect(within(desktopSidebar).getByRole("option", { name: "Agent One" })).toBeInTheDocument()
    );
    expect(within(desktopSidebar).queryByText("Agent One")).not.toBeInTheDocument();
  });

  it("the mobile stack list is unaffected by the desktop collapse state", async () => {
    localStorage.setItem("mc.chat.sidebar", "collapsed");
    renderPage();

    // Desktop rail is collapsed (icon-only, no text). The remaining text node
    // reading "Agent One" belongs to the mobile stack's own list screen — a
    // separate SessionSidebar instance (variant="list") that the rail-only
    // `collapsed` prop never reaches.
    expect(await screen.findByText("Agent One")).toBeInTheDocument();
  });
});

// jsdom in this environment has no working localStorage (see the note on the
// first describe). Installed per describe so no block depends on another
// having run first.
function installLocalStorageShim() {
  let store: Record<string, string> = {};
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
}

// ── Live agent snapshot ─────────────────────────────────────────────────────
// `selected` is captured when the row is clicked and never changes, while the
// agent queries refetch every 5–10s. Everything downstream (header context
// line, transcript gate, TerminalPanel) must read the LIVE row instead, or it
// keeps describing the agent as it was at click time — a task it has since
// finished, a status it has since left.
describe("SessionsPage — the selected agent follows the live data, not the click-time snapshot", () => {
  beforeEach(() => {
    nav.searchParamsString = "";
    installLocalStorageShim();
    vi.spyOn(api.agents, "listHostSessions").mockResolvedValue([]);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("picks up a changed current task on the next refetch", async () => {
    vi.spyOn(api.agents, "listDockerSessions")
      .mockResolvedValueOnce([mkAgent({ id: "agent-1", name: "Agent One", current_task_id: "task-1" })])
      .mockResolvedValue([mkAgent({ id: "agent-1", name: "Agent One", current_task_id: "task-2" })]);

    const { qc } = renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("chat-view-agent-task")).toHaveTextContent("task-1")
    );

    await qc.invalidateQueries({ queryKey: ["agents", "docker-sessions"] });

    await waitFor(() =>
      expect(screen.getByTestId("chat-view-agent-task")).toHaveTextContent("task-2")
    );
  });

  it("picks up a changed status on the next refetch", async () => {
    vi.spyOn(api.agents, "listDockerSessions")
      .mockResolvedValueOnce([mkAgent({ id: "agent-1", name: "Agent One", status: "idle" })])
      .mockResolvedValue([mkAgent({ id: "agent-1", name: "Agent One", status: "busy" })]);

    const { qc } = renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("chat-view-agent-status")).toHaveTextContent("idle")
    );

    await qc.invalidateQueries({ queryKey: ["agents", "docker-sessions"] });

    await waitFor(() =>
      expect(screen.getByTestId("chat-view-agent-status")).toHaveTextContent("busy")
    );
  });

  it("falls back to the snapshot when the agent disappears from the list", async () => {
    vi.spyOn(api.agents, "listDockerSessions")
      .mockResolvedValueOnce([mkAgent({ id: "agent-1", name: "Agent One", current_task_id: "task-1" })])
      .mockResolvedValue([]);

    const { qc } = renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("chat-view-agent-task")).toHaveTextContent("task-1")
    );

    await qc.invalidateQueries({ queryKey: ["agents", "docker-sessions"] });

    // Deleted or filtered out: keep showing what we last knew rather than
    // blanking the chat out from under the operator.
    await waitFor(() => expect(screen.getByText("Chat: Agent One")).toBeInTheDocument());
    expect(screen.getByTestId("chat-view-agent-task")).toHaveTextContent("task-1");
  });
});

// ── Island tones ────────────────────────────────────────────────────────────
// Operator-directed (taste round 2, reversing the earlier one-surface pass):
// reading a long transcript on near-black was tiring, so the page ground stays
// dark and every island sits a tonal step above it. The mobile list and chat
// screens must carry the SAME tone, or switching between them flashes.
describe("SessionsPage — islands sit a step above the page ground", () => {
  beforeEach(() => {
    nav.searchParamsString = "";
    installLocalStorageShim();
    vi.spyOn(api.agents, "listDockerSessions").mockResolvedValue([
      mkAgent({ id: "agent-1", name: "Agent One" }),
    ]);
    vi.spyOn(api.agents, "listHostSessions").mockResolvedValue([]);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  const BG_SURFACE = C.bgSurface;

  it("lifts the chat island above a ground it does NOT paint itself", async () => {
    renderPage();
    await screen.findAllByText("Agent One");

    const chat = screen.getByTestId("chat-column");
    // jsdom keeps a var() only in the shorthand it was written in.
    expect(chat.style.background).toBe(BG_SURFACE);

    // Operator-Befund 18.08.2026: der Seiten-Grund darf keine eigene deckende
    // Flaeche malen. Tat er es (bgDeep), uebermalte er den App-Hintergrund samt
    // AmbientBackground-Verlauf und stand als hartes Schwarz rund um die Inseln.
    // Der Grund bleibt transparent, die Insel behaelt ihren Tonschritt.
    const ground = chat.parentElement?.parentElement as HTMLElement;
    expect(ground.style.background).toBe("");
    expect(ground.style.backgroundColor).toBe("");
    expect(ground.className).not.toMatch(/bg-(deep|dark|base|black)/);
  });

  it("gives the mobile list and chat screens the same tone (no jump on switch)", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByText("Agent One");

    const listGround = screen.getByTestId("session-list-mobile").firstElementChild as HTMLElement;
    expect(listGround.style.background).toBe(BG_SURFACE);

    await user.click(
      within(screen.getByTestId("session-list-mobile")).getByRole("option", { name: /Agent One/ })
    );
    expect(screen.getByTestId("chat-column").style.background).toBe(BG_SURFACE);
  });
});

// ── Mobile stack visibility ─────────────────────────────────────────────────
// Both stack screens stay MOUNTED (the chat must keep its SSE subscription and
// scroll position while the list is up), so the inactive one has to be
// `display: none` — not merely clipped. Clipped-but-in-flow was a real defect:
// the shell column measured scrollHeight 2794 against clientHeight 852, the
// dead screen was reachable by momentum scroll and keyboard focus, and a
// programmatic scroll landed on a fully black viewport.
//
// jsdom computes no layout, so the enforceable invariant here is the class
// contract that produces `display: none`. Exactly one screen may be visible.
describe("SessionsPage — mobile stack keeps only one screen in flow", () => {
  beforeEach(() => {
    nav.searchParamsString = "";
    installLocalStorageShim();
    vi.spyOn(api.agents, "listDockerSessions").mockResolvedValue([
      mkAgent({ id: "agent-1", name: "Agent One" }),
      mkAgent({ id: "agent-2", name: "Agent Two" }),
    ]);
    vi.spyOn(api.agents, "listHostSessions").mockResolvedValue([]);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  const list = () => screen.getByTestId("session-list-mobile");
  const chat = () => screen.getByTestId("chat-column");
  const isHidden = (el: HTMLElement) => el.className.split(/\s+/).includes("hidden");

  it("starts on the list screen with the chat screen display:none", async () => {
    renderPage();
    await screen.findAllByText("Agent One");

    expect(isHidden(list())).toBe(false);
    expect(isHidden(chat())).toBe(true);
  });

  it("hides the list screen once a session is opened", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByText("Agent One");

    await user.click(within(list()).getByRole("option", { name: /Agent One/ }));

    expect(isHidden(chat())).toBe(false);
    expect(isHidden(list())).toBe(true);
  });

  it("returns to the list screen on back, hiding the chat again", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByText("Agent One");

    await user.click(within(list()).getByRole("option", { name: /Agent One/ }));
    await user.click(screen.getByRole("button", { name: "Stub Back" }));

    expect(isHidden(list())).toBe(false);
    expect(isHidden(chat())).toBe(true);
  });

  it("never leaves both screens in flow at once", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByText("Agent One");

    const bothVisible = () => !isHidden(list()) && !isHidden(chat());
    expect(bothVisible()).toBe(false);

    await user.click(within(list()).getByRole("option", { name: /Agent Two/ }));
    expect(bothVisible()).toBe(false);

    await user.click(screen.getByRole("button", { name: "Stub Back" }));
    expect(bothVisible()).toBe(false);
  });

  it("keeps both screens mounted so the chat's stream and scroll survive the switch", async () => {
    renderPage();
    await screen.findAllByText("Agent One");

    // Hidden, but present — unmounting would drop the SSE subscription and the
    // scroll position every time the operator glances at the list.
    expect(chat()).toBeInTheDocument();
    expect(list()).toBeInTheDocument();
  });

  it("a ?agent= deep link opens the chat screen directly", async () => {
    nav.searchParamsString = "agent=agent-2";
    renderPage();
    await screen.findAllByText("Agent Two");

    await waitFor(() => expect(isHidden(chat())).toBe(false));
    expect(isHidden(list())).toBe(true);
  });

  // Mobile nav V2: a chip in the ⊕ sheet (or the Chats tab) changes the URL
  // while the page is already open — the screen must follow, once.
  it("a ?agent= link arriving while the page is open opens that chat", async () => {
    const view = renderPage();
    await screen.findAllByText("Agent One");
    expect(isHidden(chat())).toBe(true);

    nav.searchParamsString = "agent=agent-2";
    view.rerender(
      <QueryClientProvider client={view.qc}>
        <SessionsPage />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(isHidden(chat())).toBe(false));
    expect(screen.getByText("Chat: Agent Two")).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem("mc-recent-chats") ?? "[]")[0]).toEqual({ kind: "agent", id: "agent-2" });
  });

  it("back to the list stays on the list although the URL still names the chat", async () => {
    const user = userEvent.setup();
    nav.searchParamsString = "agent=agent-2";
    renderPage();
    await waitFor(() => expect(isHidden(chat())).toBe(false));

    await user.click(screen.getByRole("button", { name: "Stub Back" }));
    expect(isHidden(list())).toBe(false);
    expect(isHidden(chat())).toBe(true);
  });

  // Live 02.10.2026: chat → back → ⊕ chip to the SAME chat stayed on the
  // list. Back now clears the URL; once the URL names no chat, the next link
  // to that same chat must open it again.
  it("back clears the chat from the URL, and a later link to the same chat opens it again", async () => {
    const user = userEvent.setup();
    nav.replaced = [];
    nav.searchParamsString = "agent=agent-2";
    const view = renderPage();
    await waitFor(() => expect(isHidden(chat())).toBe(false));

    await user.click(screen.getByRole("button", { name: "Stub Back" }));
    expect(isHidden(chat())).toBe(true);
    // Through Next's router (a native history.replaceState left Next's own
    // search params on the old chat — the live re-test failure).
    expect(nav.replaced).toEqual(["/sessions"]);

    const rerender = () =>
      view.rerender(
        <QueryClientProvider client={view.qc}>
          <SessionsPage />
        </QueryClientProvider>,
      );
    rerender();                              // Next now reports no chat
    nav.searchParamsString = "agent=agent-2"; // the chip to the same chat
    rerender();
    await waitFor(() => expect(isHidden(chat())).toBe(false));
  });

  it("opening a chat from the list remembers it for the Chats tab", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByText("Agent One");
    await user.click(within(list()).getByRole("option", { name: /Agent Two/ }));
    expect(JSON.parse(localStorage.getItem("mc-recent-chats") ?? "[]")[0]).toEqual({ kind: "agent", id: "agent-2" });
  });

  it("a merely remembered selection still opens on the list screen", async () => {
    localStorage.setItem("mc-sessions-last-agent", "agent-2");
    renderPage();
    await screen.findAllByText("Agent Two");

    expect(isHidden(list())).toBe(false);
    expect(isHidden(chat())).toBe(true);
  });
});

// ── Heads in Chats (heads-sichtbar PR 2) ────────────────────────────────────

describe("SessionsPage — heads deep link (?head=)", () => {
  beforeEach(() => {
    nav.searchParamsString = "";
    nav.replaced = [];
    vi.spyOn(api.agents, "listDockerSessions").mockResolvedValue([]);
    vi.spyOn(api.agents, "listHostSessions").mockResolvedValue([]);
  });

  afterEach(() => vi.restoreAllMocks());

  it("opens the named run even when it is outside the recent-runs window (the one-off fallback fetch)", async () => {
    vi.spyOn(api.heads, "occupancy").mockResolvedValue({ boxes: {} });
    vi.spyOn(api.heads, "list").mockResolvedValue({ runs: [], archived_count: 1 });
    vi.spyOn(api.heads, "get").mockResolvedValue({
      run_id: "archived-1", task_id: null, title: "Old fixed head", harness: "omp", runtime_slug: "x",
      model: "glm", repo_full_name: null, branch: "b", mode: "fresh", restarted_from: null, box_keys: [],
      created_at: "2026-09-01T00:00:00Z", started_at: null, exited_at: "2026-09-02T00:00:00Z",
      state: "passed", reason: null, silent_s: null, heartbeat_stale: false, step: null, question: null,
      pr_url: null, tmux: null, run_record: false, task_deleted: false,
    });

    nav.searchParamsString = "head=archived-1";
    renderPage();
    await waitFor(() => expect(screen.getByTestId("chat-view-head")).toHaveTextContent("archived-1"));
    expect(api.heads.get).toHaveBeenCalledWith("archived-1");
  });

  it("selecting a head from the sidebar's Heads section opens it without a fallback fetch (already in the recent list)", async () => {
    vi.spyOn(api.heads, "occupancy").mockResolvedValue({ boxes: {} });
    vi.spyOn(api.heads, "list").mockResolvedValue({
      runs: [{
        run_id: "run-1", task_id: null, title: "Fix flaky retry test", harness: "omp", runtime_slug: "x",
        model: "glm", repo_full_name: null, branch: "b", mode: "fresh", restarted_from: null, box_keys: [],
        created_at: "2026-10-04T09:00:00Z", started_at: "2026-10-04T09:00:00Z", exited_at: null,
        state: "running", reason: null, silent_s: 5, heartbeat_stale: false, step: null, question: null,
        pr_url: null, tmux: null, run_record: false, task_deleted: false,
      }],
      archived_count: 0,
    });
    const getSpy = vi.spyOn(api.heads, "get");

    const user = userEvent.setup();
    renderPage();
    // Mobile list AND desktop rail are both mounted in jsdom regardless of
    // viewport (same reason `findOptionRow` exists above) — the mobile
    // list's own row is the one guaranteed clickable one.
    const row = (await within(screen.getByTestId("session-list-mobile")).findAllByTestId("head-chat-row"))[0];
    await user.click(row);
    await waitFor(() => expect(screen.getByTestId("chat-view-head")).toHaveTextContent("run-1"));
    expect(getSpy).not.toHaveBeenCalled();
  });

  // Review finding on PR #756: `selectedHeadId` is set before `selectedHeadRun`
  // resolves in this exact gap — `ChatView` (stubbed above) used to mount
  // with `head={null}` here, which the REAL component's "pick a session"
  // empty state (no header, no back chevron — see `ChatView.head.test.tsx`'s
  // own hooks-order regression test for the crash this caused) covered up in
  // THIS file because `ChatView` is a stub. `HeadChatPlaceholder` is not
  // mocked, so these two assert against the real component.
  it("while the fallback fetch for an off-window run is in flight, shows the loading placeholder with a back chevron — never the chat stub with head=null", async () => {
    vi.spyOn(api.heads, "occupancy").mockResolvedValue({ boxes: {} });
    vi.spyOn(api.heads, "list").mockResolvedValue({ runs: [], archived_count: 1 });
    let resolveGet!: (run: HeadRun) => void;
    vi.spyOn(api.heads, "get").mockReturnValue(new Promise<HeadRun>((resolve) => { resolveGet = resolve; }));

    nav.searchParamsString = "head=archived-1";
    renderPage();

    await waitFor(() => expect(screen.getByTestId("head-chat-placeholder")).toHaveAttribute("data-variant", "loading"));
    expect(screen.getByTestId("head-chat-back")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-view-stub")).not.toBeInTheDocument();

    resolveGet({
      run_id: "archived-1", task_id: null, title: "Old fixed head", harness: "omp", runtime_slug: "x",
      model: "glm", repo_full_name: null, branch: "b", mode: "fresh", restarted_from: null, box_keys: [],
      created_at: "2026-09-01T00:00:00Z", started_at: null, exited_at: "2026-09-02T00:00:00Z",
      state: "passed", reason: null, silent_s: null, heartbeat_stale: false, step: null, question: null,
      pr_url: null, tmux: null, run_record: false, task_deleted: false,
    });
    await waitFor(() => expect(screen.getByTestId("chat-view-head")).toHaveTextContent("archived-1"));
  });

  it("a `?head=` id that no longer exists (deleted/cleaned up) shows the not-found placeholder with a back chevron, never the chromeless agent empty-state", async () => {
    vi.spyOn(api.heads, "occupancy").mockResolvedValue({ boxes: {} });
    vi.spyOn(api.heads, "list").mockResolvedValue({ runs: [], archived_count: 0 });
    vi.spyOn(api.heads, "get").mockRejectedValue(new Error("API 404: run_not_found"));

    nav.searchParamsString = "head=gone-1";
    renderPage();

    await waitFor(() => expect(screen.getByTestId("head-chat-placeholder")).toHaveAttribute("data-variant", "not_found"));
    expect(screen.getByTestId("head-chat-not-found")).toBeInTheDocument();
    expect(screen.getByTestId("head-chat-back")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-view-stub")).not.toBeInTheDocument();
  });

  it("tapping an Archive-sheet row (archived run, never in the recent list) opens it through the same resolving → chat path", async () => {
    vi.spyOn(api.heads, "occupancy").mockResolvedValue({ boxes: {} });
    const ARCHIVED_ROW: HeadRun = {
      run_id: "archived-row-1", task_id: null, title: "Rotate the API key", harness: "claude", runtime_slug: "x",
      model: "claude-opus", repo_full_name: null, branch: "b", mode: "fresh", restarted_from: null, box_keys: [],
      created_at: "2026-09-01T00:00:00Z", started_at: null, exited_at: "2026-09-02T00:00:00Z",
      state: "passed", reason: null, silent_s: null, heartbeat_stale: false, step: null, question: null,
      pr_url: null, tmux: null, run_record: false, task_deleted: false,
    };
    vi.spyOn(api.heads, "list").mockImplementation(async (opts) =>
      opts?.archived ? { runs: [ARCHIVED_ROW] } : { runs: [], archived_count: 1 },
    );
    let resolveGet!: (run: HeadRun) => void;
    vi.spyOn(api.heads, "get").mockReturnValue(new Promise<HeadRun>((resolve) => { resolveGet = resolve; }));

    const user = userEvent.setup();
    renderPage();
    // Mobile list AND desktop rail both mount in jsdom regardless of
    // viewport — same reasoning as the recent-list click test above.
    const mobileList = await screen.findByTestId("session-list-mobile");
    await user.click(await within(mobileList).findByTestId("heads-archive-button"));
    const row = await within(await screen.findByTestId("head-archive-sheet")).findByTestId("head-chat-row");
    await user.click(row);

    // Sheet closes, placeholder shows while `api.heads.get` (the row's run
    // is not in the recent list) is still in flight.
    await waitFor(() => expect(screen.queryByTestId("head-archive-sheet")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId("head-chat-placeholder")).toHaveAttribute("data-variant", "loading"));
    expect(api.heads.get).toHaveBeenCalledWith("archived-row-1");

    resolveGet(ARCHIVED_ROW);
    await waitFor(() => expect(screen.getByTestId("chat-view-head")).toHaveTextContent("archived-row-1"));
  });
});

// ── Phone history: back from a chat lands on the chats list ─────────────────
// The edge swipe is the browser's back, so on the phone the list must be the
// history entry directly behind every open chat (lib/chatHistory.ts). These
// tests pin the router calls; the history itself is proven in a real browser
// (playwright/chat-back.spec.ts) — a mocked router cannot show what back does.

describe("SessionsPage — phone history puts the list behind every chat", () => {
  const list = () => screen.getByTestId("session-list-mobile");
  const chat = () => screen.getByTestId("chat-column");
  const isHidden = (el: HTMLElement) => el.className.split(/\s+/).includes("hidden");
  let phone = true;

  beforeEach(() => {
    phone = true;
    nav.searchParamsString = "";
    nav.replaced = [];
    nav.pushed = [];
    nav.backs = 0;
    installLocalStorageShim();
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: (query: string) => ({ matches: phone && query.includes("max-width"), media: query }),
    });
    vi.spyOn(api.agents, "listDockerSessions").mockResolvedValue([
      mkAgent({ id: "agent-1", name: "Agent One" }),
      mkAgent({ id: "agent-2", name: "Agent Two" }),
    ]);
    vi.spyOn(api.agents, "listHostSessions").mockResolvedValue([]);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    delete (window as { matchMedia?: unknown }).matchMedia;
  });

  // The mocked router does not re-render on its own; Next does.
  const rerenderWith = (view: ReturnType<typeof renderPage>) =>
    view.rerender(
      <QueryClientProvider client={view.qc}>
        <SessionsPage />
      </QueryClientProvider>,
    );

  it("a link from elsewhere (enter=1) turns its one entry into list + chat", async () => {
    nav.searchParamsString = "agent=agent-2&enter=1";
    const view = renderPage();
    await waitFor(() => expect(nav.replaced).toEqual(["/sessions"]));
    expect(nav.pushed).toEqual([]);
    // The chat stays on screen while the list entry goes in underneath.
    expect(isHidden(chat())).toBe(false);

    rerenderWith(view); // Next now reports the list URL
    await waitFor(() => expect(nav.pushed).toEqual(["/sessions?agent=agent-2"]));
    expect(isHidden(chat())).toBe(false);

    rerenderWith(view); // … and then the chat's own URL
    expect(nav.replaced).toEqual(["/sessions"]);
    expect(nav.pushed).toEqual(["/sessions?agent=agent-2"]);
    expect(screen.getByText("Chat: Agent Two")).toBeInTheDocument();
  });

  it("a chat the browser returns to (no marker) is left alone — no doubled list", async () => {
    nav.searchParamsString = "agent=agent-2";
    renderPage();
    await waitFor(() => expect(isHidden(chat())).toBe(false));
    expect(nav.replaced).toEqual([]);
    expect(nav.pushed).toEqual([]);
  });

  it("a link arriving while the list is open only drops the marker (the list is already behind)", async () => {
    const view = renderPage();
    await screen.findAllByText("Agent One");
    nav.searchParamsString = "agent=agent-2&enter=1";
    rerenderWith(view);
    await waitFor(() => expect(isHidden(chat())).toBe(false));
    expect(nav.replaced).toEqual(["/sessions?agent=agent-2"]);
    expect(nav.pushed).toEqual([]);
  });

  it("opening a chat from the list pushes it on top of the list", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByText("Agent One");
    await user.click(within(list()).getByRole("option", { name: /Agent Two/ }));
    expect(nav.pushed).toEqual(["/sessions?agent=agent-2"]);
    expect(isHidden(chat())).toBe(false);
  });

  it("the header's back chevron goes back in history, like the swipe", async () => {
    const user = userEvent.setup();
    nav.searchParamsString = "agent=agent-2";
    renderPage();
    await waitFor(() => expect(isHidden(chat())).toBe(false));
    await user.click(screen.getByRole("button", { name: "Stub Back" }));
    expect(nav.backs).toBe(1);
    expect(nav.replaced).toEqual([]);
  });

  it("when back takes the chat out of the URL, the list shows", async () => {
    nav.searchParamsString = "agent=agent-2";
    const view = renderPage();
    await waitFor(() => expect(isHidden(chat())).toBe(false));

    nav.searchParamsString = ""; // the browser went back to the list entry
    rerenderWith(view);
    await waitFor(() => expect(isHidden(list())).toBe(false));
    expect(isHidden(chat())).toBe(true);
  });

  it("desktop keeps one entry per link: the marker goes, nothing is pushed", async () => {
    phone = false;
    nav.searchParamsString = "agent=agent-2&enter=1";
    renderPage();
    await waitFor(() => expect(nav.replaced).toEqual(["/sessions?agent=agent-2"]));
    expect(nav.pushed).toEqual([]);
  });

  it("desktop: picking a chat from the list adds no history entry", async () => {
    phone = false;
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByText("Agent One");
    await user.click(within(screen.getByTestId("sidebar-desktop")).getByRole("option", { name: /Agent Two/ }));
    expect(nav.pushed).toEqual([]);
  });
});
