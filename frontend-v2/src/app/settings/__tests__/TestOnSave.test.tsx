import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ProviderTemplate, SecretEntry } from "@/lib/types";

// Test on save + age across the settings sections (Welle 2, ADR-093 §2).
// Each test deep-links into its section (same convention as GithubSection.test.tsx).
// The section lives in the URL. router.replace updates a small store that
// useSearchParams subscribes to — like Next's router, a section jump is a URL
// change the page re-renders from.
const url = vi.hoisted(() => {
  let search = "section=apikeys";
  const listeners = new Set<() => void>();
  return {
    get: () => search,
    set: (next: string) => {
      search = next;
      listeners.forEach((l) => l());
    },
    subscribe: (l: () => void) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
  };
});
vi.mock("next/navigation", async () => {
  const { useSyncExternalStore } = await import("react");
  return {
    useRouter: () => ({
      replace: (href: string) => url.set(href.split("?")[1] ?? ""),
      push: vi.fn(),
      back: vi.fn(),
      refresh: vi.fn(),
    }),
    usePathname: () => "/settings",
    useSearchParams: () => new URLSearchParams(useSyncExternalStore(url.subscribe, url.get, url.get)),
  };
});

const mockAppState = vi.hoisted(() => ({
  state: {
    activeBoardId: null as string | null,
    sidebarCollapsed: false,
    commandPaletteOpen: false,
    boards: [] as unknown[],
    currentUser: { id: "u1", email: "a@b.com", name: "Admin", role: "admin" } as {
      id: string; email: string; name: string; role: string;
    } | null,
    setActiveBoardId: (id: string | null) => { mockAppState.state.activeBoardId = id; },
    toggleSidebar: () => {},
    setCommandPaletteOpen: (open: boolean) => { mockAppState.state.commandPaletteOpen = open; },
    setBoards: (boards: unknown[]) => { mockAppState.state.boards = boards; },
    setCurrentUser: (user: typeof mockAppState.state.currentUser) => { mockAppState.state.currentUser = user; },
  },
}));
vi.mock("@/lib/store", () => ({
  // ToastRenderer (AppShell) and notify() read this store too.
  useNotificationStore: Object.assign(
    (selector?: (s: { notifications: never[] }) => unknown) =>
      selector ? selector({ notifications: [] }) : { notifications: [] },
    { getState: () => ({ addNotification: vi.fn() }) }
  ),
  useAppStore: Object.assign(
    (selector?: (s: typeof mockAppState.state) => unknown) =>
      selector ? selector(mockAppState.state) : mockAppState.state,
    { setState: (partial: Partial<typeof mockAppState.state>) => Object.assign(mockAppState.state, partial) }
  ),
}));

import SettingsPage from "../page";

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <SettingsPage />
    </QueryClientProvider>
  );
}

// ── fixtures ──────────────────────────────────────────────────────────────

import type { ConfigCheckItem, GithubStatus, GithubConfigStatus, SlackConnectionResult, TelegramConnectionResult } from "@/lib/types";
import { waitFor, within } from "@testing-library/react";

function check(over: Partial<ConfigCheckItem>): ConfigCheckItem {
  return {
    kind: "secret",
    subject_id: "openai_api_key",
    label: "OpenAI",
    secret_key: "openai_api_key",
    configured: true,
    testable: true,
    cost: "free",
    state: "never_tested",
    message: null,
    duration_ms: null,
    checked_at: null,
    age_seconds: null,
    value_changed: false,
    ...over,
  };
}

const ago = (s: number) => ({ age_seconds: s, checked_at: new Date(Date.now() - s * 1000).toISOString() });

function listChecks(items: ConfigCheckItem[]) {
  return vi.spyOn(api.configChecks, "list").mockResolvedValue({ kinds: [], cost_hints: {} as never, items });
}

const OPENAI: ProviderTemplate = {
  provider: "openai",
  key: "openai_api_key",
  label: "OpenAI API Key",
  description: "For GPT models",
  placeholder: "sk-...",
};

const OPENAI_SECRET: SecretEntry = {
  id: "s2",
  key: "openai_api_key",
  value_masked: "****wxyz",
  provider: "openai",
  label: "OpenAI API Key",
  description: null,
  created_at: null,
  updated_at: null,
};

function setup(section: string) {
  url.set(`section=${section}`);
  vi.restoreAllMocks();
  mockAppState.state.currentUser = { id: "u1", email: "a@b.com", name: "Admin", role: "admin" };
  const store: Record<string, string> = { mc_auth_token: "tok" };
  Object.defineProperty(globalThis, "localStorage", {
    value: {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v; },
      removeItem: (k: string) => { delete store[k]; },
      clear: () => undefined,
    },
    configurable: true,
    writable: true,
  });
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
  );
}

// ── API keys ──────────────────────────────────────────────────────────────

describe("API keys: test on save + age", () => {
  beforeEach(() => setup("apikeys"));

  it("shows the stored result and its age next to a set key", async () => {
    vi.spyOn(api.secrets, "providers").mockResolvedValue([OPENAI]);
    vi.spyOn(api.secrets, "list").mockResolvedValue([OPENAI_SECRET]);
    listChecks([check({ state: "ok", ...ago(180) })]);

    renderPage();

    const card = (await screen.findByText("OpenAI API Key")).closest(".mc-card") as HTMLElement;
    await waitFor(() =>
      expect(within(card).getByTestId("check-status")).toHaveTextContent("Works · tested 3 min ago"),
    );
  });

  it("saving a new key runs its registry check and shows a rejection with the reason", async () => {
    vi.spyOn(api.secrets, "providers").mockResolvedValue([OPENAI]);
    const secretsList = vi.spyOn(api.secrets, "list").mockResolvedValue([]);
    listChecks([]);
    const create = vi.spyOn(api.secrets, "create").mockImplementation(async () => {
      secretsList.mockResolvedValue([OPENAI_SECRET]);
      return OPENAI_SECRET;
    });
    const run = vi.spyOn(api.configChecks, "run").mockResolvedValue({
      item: check({ state: "fail", message: "OpenAI rejected the key (HTTP 401)", ...ago(0) }),
      notRunnable: null,
    });

    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Add" }));
    await userEvent.type(screen.getByPlaceholderText("sk-..."), "sk-wrong");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(create).toHaveBeenCalled());
    await waitFor(() => expect(run).toHaveBeenCalledWith("secret", "openai_api_key"));
    const card = (await screen.findByText("OpenAI API Key")).closest(".mc-card") as HTMLElement;
    await waitFor(() => expect(within(card).getByTestId("check-status")).toHaveTextContent("Failed · tested just now"));
    expect(within(card).getByTestId("check-message")).toHaveTextContent("HTTP 401");
  });

  it("changing a key also runs the check, and Test re-runs it on demand", async () => {
    vi.spyOn(api.secrets, "providers").mockResolvedValue([OPENAI]);
    vi.spyOn(api.secrets, "list").mockResolvedValue([OPENAI_SECRET]);
    listChecks([check({ state: "ok", ...ago(600) })]);
    const update = vi.spyOn(api.secrets, "update").mockResolvedValue(OPENAI_SECRET);
    const run = vi.spyOn(api.configChecks, "run").mockResolvedValue({
      item: check({ state: "ok", ...ago(0) }),
      notRunnable: null,
    });

    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Change" }));
    await userEvent.type(screen.getByPlaceholderText("New value..."), "sk-new");
    await userEvent.click(screen.getByRole("button", { name: "Update" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    await waitFor(() => expect(run).toHaveBeenCalledTimes(1));

    const card = (await screen.findByText("OpenAI API Key")).closest(".mc-card") as HTMLElement;
    await waitFor(() => expect(within(card).getByTestId("check-status")).toHaveTextContent("tested just now"));
    await userEvent.click(within(card).getByRole("button", { name: "Test" }));
    await waitFor(() => expect(run).toHaveBeenCalledTimes(2));
  });
});

// ── GitHub ────────────────────────────────────────────────────────────────

const GH: GithubStatus = {
  owner: "acme",
  owner_source: "vault",
  token_set: true,
  token_source: "vault",
  configured: true,
  connected: null,
  login: null,
  owner_type: null,
  rate_limit_remaining: null,
  rate_limit_total: null,
  error: null,
};

describe("GitHub: test on save, age, removable token", () => {
  beforeEach(() => setup("github"));

  it("without a live probe, the status card shows the last stored result and its age", async () => {
    vi.spyOn(api.repos, "githubStatus").mockResolvedValue(GH);
    listChecks([check({ kind: "github", subject_id: "connection", secret_key: "github_token", cost: "rate_limited", state: "ok", ...ago(7200) })]);

    renderPage();

    expect(await screen.findByText("tested 2 hours ago")).toBeInTheDocument();
  });

  it("saving a token runs the GitHub check right away", async () => {
    const status = vi.spyOn(api.repos, "githubStatus").mockImplementation(async (probe?: boolean) =>
      probe ? { ...GH, connected: false, error: "Bad credentials" } : GH,
    );
    vi.spyOn(api.repos, "setGithubConfig").mockResolvedValue(GH as GithubConfigStatus);
    listChecks([]);

    renderPage();

    await userEvent.type(await screen.findByLabelText("GitHub token"), "ghp_bad");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(status).toHaveBeenCalledWith(true));
    expect(await screen.findByText("Connection failed")).toBeInTheDocument();
    expect(screen.getByText("Bad credentials")).toBeInTheDocument();
  });

  it("a token saved in the app can be removed (confirm first)", async () => {
    vi.spyOn(api.repos, "githubStatus").mockResolvedValue(GH);
    listChecks([]);
    const save = vi.spyOn(api.repos, "setGithubConfig").mockResolvedValue({ ...GH, token_set: false, token_source: null, configured: false } as GithubConfigStatus);

    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Remove token" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Agents lose GitHub access");
    await userEvent.click(within(dialog).getByRole("button", { name: "Remove token" }));

    await waitFor(() => expect(save).toHaveBeenCalledWith({ token: "" }));
  });

  it("a token that only lives in .env offers no remove button (nothing in the app to delete)", async () => {
    vi.spyOn(api.repos, "githubStatus").mockResolvedValue({ ...GH, token_source: "env" });
    listChecks([]);

    renderPage();

    await screen.findByRole("heading", { name: "GitHub" });
    await screen.findByText("Set ••••");
    expect(screen.queryByRole("button", { name: "Remove token" })).not.toBeInTheDocument();
  });
});

// ── Slack + Telegram ──────────────────────────────────────────────────────

const SLACK_OK: SlackConnectionResult = {
  connected: true,
  team: "Acme HQ",
  bot_user: "mission-control",
  bot_token_set: true,
  app_token_set: true,
  socket_mode_ready: true,
  error: null,
  app_token_error: null,
};

describe("Slack + Telegram: the age of the stored result", () => {
  it("Slack: the status card says when it was tested, and re-reads it after a test", async () => {
    setup("slack");
    vi.spyOn(api.secrets, "list").mockResolvedValue([
      { ...OPENAI_SECRET, key: "slack_bot_token", provider: "slack" },
    ]);
    vi.spyOn(api.slack, "testConnection").mockResolvedValue(SLACK_OK);
    const list = listChecks([check({ kind: "slack", subject_id: "workspace", secret_key: "slack_bot_token", state: "ok", ...ago(0) })]);

    renderPage();

    expect(await screen.findByText("tested just now")).toBeInTheDocument();
    // The Slack button stores through the registry — after a test the age is re-read.
    await waitFor(() => expect(screen.getByRole("button", { name: "Test connection" })).toBeEnabled());
    const before = list.mock.calls.length;
    await userEvent.click(screen.getByRole("button", { name: "Test connection" }));
    await waitFor(() => expect(list.mock.calls.length).toBeGreaterThan(before));
  });

  it("Telegram: each bot line carries its own age", async () => {
    setup("telegram");
    vi.spyOn(api.secrets, "list").mockResolvedValue([]);
    vi.spyOn(api.channels, "getSettings").mockResolvedValue({ values: {}, overridden: [] } as never);
    const tg: TelegramConnectionResult = {
      command_bot: { token_set: true, chat_id_set: true, connected: true, bot_username: "mc_bot", error: null },
      reports_bot: { token_set: false, chat_id_set: false, connected: false, bot_username: null, error: null },
    };
    vi.spyOn(api.channels, "telegramTestConnection").mockResolvedValue(tg);
    listChecks([
      check({ kind: "telegram", subject_id: "command_bot", secret_key: "telegram_bot_token", state: "ok", ...ago(120) }),
      check({ kind: "telegram", subject_id: "reports_bot", secret_key: "telegram_reports_bot_token", configured: false }),
    ]);

    renderPage();

    expect(await screen.findByText("tested 2 min ago")).toBeInTheDocument();
  });
});
