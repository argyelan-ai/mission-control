import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { SecretEntry, SlackConnectionResult } from "@/lib/types";

// Deep-link straight into ?section=slack so only SlackTab mounts.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/settings",
  useSearchParams: () => new URLSearchParams("section=slack"),
}));

const mockAppState = vi.hoisted(() => ({
  state: {
    activeBoardId: null as string | null,
    sidebarCollapsed: false,
    commandPaletteOpen: false,
    boards: [] as unknown[],
    boardGroups: [] as unknown[],
    currentUser: { id: "u1", email: "a@b.com", name: "Admin", role: "admin" } as {
      id: string; email: string; name: string; role: string;
    } | null,
    setActiveBoardId: (id: string | null) => { mockAppState.state.activeBoardId = id; },
    toggleSidebar: () => {},
    setCommandPaletteOpen: (open: boolean) => { mockAppState.state.commandPaletteOpen = open; },
    setBoards: (boards: unknown[]) => { mockAppState.state.boards = boards; },
    setBoardGroups: (boardGroups: unknown[]) => { mockAppState.state.boardGroups = boardGroups; },
    setCurrentUser: (user: typeof mockAppState.state.currentUser) => { mockAppState.state.currentUser = user; },
  },
}));
vi.mock("@/lib/store", () => ({
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

const CONNECTED: SlackConnectionResult = {
  connected: true,
  team: "Acme HQ",
  bot_user: "mission-control",
  bot_token_set: true,
  app_token_set: true,
  socket_mode_ready: true,
  error: null,
  app_token_error: null,
};

const SECRETS: SecretEntry[] = [
  {
    id: "s1",
    key: "slack_bot_token",
    value_masked: "****abcd",
    provider: "slack",
    label: "Slack Bot Token",
    description: null,
    created_at: null,
    updated_at: null,
  },
];

describe("SlackTab (Settings)", () => {
  beforeEach(() => {
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
  });

  it("shows the connection status with workspace and bot, and both token fields", async () => {
    vi.spyOn(api.secrets, "list").mockResolvedValue(SECRETS);
    const testSpy = vi.spyOn(api.slack, "testConnection").mockResolvedValue(CONNECTED);

    renderPage();

    expect(await screen.findByRole("heading", { name: "Slack" })).toBeInTheDocument();
    expect(await screen.findByText("Connected to Acme HQ")).toBeInTheDocument();
    expect(screen.getByText("mission-control")).toBeInTheDocument();
    expect(screen.getByText("ready")).toBeInTheDocument();
    // Fixed fields, not a free-text key/value form.
    expect(screen.getByLabelText("Bot User OAuth Token")).toBeInTheDocument();
    expect(screen.getByLabelText("App-Level Token")).toBeInTheDocument();
    // Status is loaded on open, without a click.
    await waitFor(() => expect(testSpy).toHaveBeenCalled());
  });

  it("the test button calls the endpoint again", async () => {
    vi.spyOn(api.secrets, "list").mockResolvedValue(SECRETS);
    const testSpy = vi.spyOn(api.slack, "testConnection").mockResolvedValue(CONNECTED);

    renderPage();
    await screen.findByText("Connected to Acme HQ");
    await waitFor(() => expect(testSpy).toHaveBeenCalledTimes(1));

    await userEvent.click(screen.getByRole("button", { name: "Test connection" }));

    await waitFor(() => expect(testSpy).toHaveBeenCalledTimes(2));
  });

  it("renders Slack's own failure message instead of a generic error", async () => {
    vi.spyOn(api.secrets, "list").mockResolvedValue(SECRETS);
    vi.spyOn(api.slack, "testConnection").mockResolvedValue({
      ...CONNECTED,
      connected: false,
      team: null,
      bot_user: null,
      error: "Slack rejected the bot token (invalid_auth).",
    });

    renderPage();

    expect(await screen.findByText("Not connected")).toBeInTheDocument();
    expect(
      await screen.findByText("Slack rejected the bot token (invalid_auth).")
    ).toBeInTheDocument();
  });

  it("reports a missing app-level token as its own defect, not as a bot-token failure", async () => {
    vi.spyOn(api.secrets, "list").mockResolvedValue(SECRETS);
    vi.spyOn(api.slack, "testConnection").mockResolvedValue({
      ...CONNECTED,
      app_token_set: false,
      socket_mode_ready: false,
      app_token_error: "No app-level token set. Socket Mode needs one.",
    });

    renderPage();

    expect(await screen.findByText(/Socket Mode not ready/)).toBeInTheDocument();
    expect(await screen.findByTestId("slack-app-token-error")).toHaveTextContent(
      "No app-level token set."
    );
    expect(screen.queryByTestId("slack-error")).not.toBeInTheDocument();
  });

  it("copies the full scope list to the clipboard with one click", async () => {
    vi.spyOn(api.secrets, "list").mockResolvedValue(SECRETS);
    vi.spyOn(api.slack, "testConnection").mockResolvedValue(CONNECTED);

    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });

    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: /Set up the Slack app/ }));

    const list = await screen.findByTestId("slack-scope-list");
    expect(list).toHaveTextContent("chat:write.customize");

    await userEvent.click(screen.getByRole("button", { name: "Copy scope list" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const copied = writeText.mock.calls[0][0] as string;
    for (const scope of [
      "chat:write",
      "chat:write.customize",
      "channels:read",
      "channels:manage",
      "channels:history",
      "app_mentions:read",
      "im:history",
      "im:write",
      "users:read",
      "reactions:write",
      "files:write",
    ]) {
      expect(copied).toContain(scope);
    }
  });

  it("saves a pasted bot token through the secrets API", async () => {
    vi.spyOn(api.secrets, "list").mockResolvedValue([]);
    vi.spyOn(api.slack, "testConnection").mockResolvedValue({
      ...CONNECTED,
      connected: false,
      bot_token_set: false,
      app_token_set: false,
      socket_mode_ready: false,
      team: null,
      bot_user: null,
      error: "No bot token set.",
      app_token_error: "No app-level token set.",
    });
    const createSpy = vi.spyOn(api.secrets, "create").mockResolvedValue(SECRETS[0]);

    renderPage();

    const field = await screen.findByLabelText("Bot User OAuth Token");
    await userEvent.type(field, "xoxb-TEST-dummy");

    const card = field.closest("div.mc-card") as HTMLElement;
    await userEvent.click(within(card).getAllByRole("button", { name: "Save" })[0]);

    await waitFor(() =>
      expect(createSpy).toHaveBeenCalledWith({
        key: "slack_bot_token",
        value: "xoxb-TEST-dummy",
        provider: "slack",
      })
    );
  });
});
