import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ClaudeAccountTab } from "../ClaudeAccountTab";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import type { ClaudeAccountStatus, ConfigCheckItem } from "@/lib/types";

vi.mock("@/lib/notify", () => ({
  notify: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));

vi.setConfig({ testTimeout: 15_000 });

function renderTab() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ClaudeAccountTab />
    </QueryClientProvider>
  );
}

const minutesAgo = (n: number) => new Date(Date.now() - n * 60_000).toISOString();

const check = (over: Partial<ConfigCheckItem> = {}): ConfigCheckItem => ({
  kind: "secret",
  subject_id: "claude_code_oauth_token",
  label: "Claude Code OAuth Token",
  secret_key: "claude_code_oauth_token",
  configured: true,
  testable: true,
  cost: "quota",
  state: "ok",
  message: "Claude accepted the key (HTTP 200).",
  duration_ms: 420,
  checked_at: minutesAgo(3),
  age_seconds: 180,
  value_changed: false,
  reason: null,
  reason_params: {},
  ...over,
});

const status = (over: Partial<ClaudeAccountStatus> = {}): ClaudeAccountStatus => ({
  configured: true,
  token_updated_at: minutesAgo(60 * 24 * 2),
  check: check(),
  claude_agents: 2,
  host_agents: 0,
  stale_agents: [],
  login: { state: "idle", url: null, error: null },
  ...over,
});

const IDLE = { state: "idle", url: null, error: null } as const;

beforeEach(() => {
  vi.restoreAllMocks();
  vi.mocked(notify.success).mockReset();
  vi.mocked(notify.error).mockReset();
  vi.spyOn(api.configChecks, "run").mockResolvedValue({ item: check(), notRunnable: null });
});

describe("ClaudeAccountTab — status", () => {
  it("not connected: says so and offers Connect", async () => {
    vi.spyOn(api.claudeAccount, "status").mockResolvedValue(
      status({ configured: false, token_updated_at: null, check: null })
    );
    renderTab();
    expect(await screen.findByTestId("claude-status-headline")).toHaveTextContent("Not connected");
    expect(screen.getByRole("button", { name: "Connect Claude" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disconnect" })).toBeNull();
  });

  it("working: shows when it was last tested", async () => {
    vi.spyOn(api.claudeAccount, "status").mockResolvedValue(status());
    renderTab();
    const headline = await screen.findByTestId("claude-status-headline");
    expect(headline).toHaveTextContent("Connected");
    expect(headline).toHaveTextContent("tested 3 minutes ago");
  });

  it("reading the card never runs the real test (E8a: only on save or click)", async () => {
    vi.spyOn(api.claudeAccount, "status").mockResolvedValue(status({ check: check({ state: "never_tested", checked_at: null, age_seconds: null, message: null }) }));
    renderTab();
    expect(await screen.findByTestId("claude-status-headline")).toHaveTextContent("Not tested yet");
    expect(api.configChecks.run).not.toHaveBeenCalled();
  });

  it("broken token: shows Claude's answer and makes Reconnect the main action", async () => {
    vi.spyOn(api.claudeAccount, "status").mockResolvedValue(
      status({ check: check({ state: "fail", message: "Claude rejected the key (HTTP 401) — expired, revoked or mistyped?" }) })
    );
    renderTab();
    expect(await screen.findByTestId("claude-status-headline")).toHaveTextContent("Claude refused the token");
    expect(screen.getByTestId("claude-check-message")).toHaveTextContent("HTTP 401");
    expect(screen.getByRole("button", { name: "Reconnect" })).toBeInTheDocument();
  });

  it("Test now runs the registry check for the token", async () => {
    vi.spyOn(api.claudeAccount, "status").mockResolvedValue(status());
    renderTab();
    await userEvent.click(await screen.findByRole("button", { name: "Test now" }));
    expect(api.configChecks.run).toHaveBeenCalledWith("secret", "claude_code_oauth_token");
  });
});

describe("ClaudeAccountTab — agents with the old token", () => {
  it("names the agents and restarts the idle ones through the agent restart", async () => {
    vi.spyOn(api.claudeAccount, "status").mockResolvedValue(
      status({
        stale_agents: [
          { id: "a1", name: "Builder", busy: false, started_at: minutesAgo(300) },
          { id: "a2", name: "Reviewer", busy: true, started_at: minutesAgo(300) },
        ],
      })
    );
    const restart = vi
      .spyOn(api.agents, "restartContainer")
      .mockResolvedValue({ ok: true, container: "mc-agent-builder", state: "running" });
    renderTab();

    expect(await screen.findByTestId("claude-stale")).toHaveTextContent("2 agents still use the old token");
    expect(screen.getByText("Builder")).toBeInTheDocument();
    expect(screen.getByText("Reviewer")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Restart 1 agent" }));
    await userEvent.click(await screen.findByRole("button", { name: "Restart" }));

    await waitFor(() => expect(restart).toHaveBeenCalledTimes(1));
    expect(restart).toHaveBeenCalledWith("a1");
  });

  it("says host agents read the token from .env", async () => {
    vi.spyOn(api.claudeAccount, "status").mockResolvedValue(status({ host_agents: 1 }));
    renderTab();
    expect(await screen.findByTestId("claude-host-note")).toHaveTextContent(".env");
  });
});

describe("ClaudeAccountTab — reconnect via claude setup-token", () => {
  it("shows the sign-in link, sends the pasted code and reports success", async () => {
    vi.spyOn(api.claudeAccount, "status").mockResolvedValue(status());
    vi.spyOn(api.claudeAccount, "startLogin").mockResolvedValue({ state: "starting", url: null, error: null });
    vi.spyOn(api.claudeAccount, "loginStatus").mockResolvedValue({
      state: "waiting_for_code",
      url: "https://claude.ai/oauth/authorize?state=xyz",
      error: null,
    });
    const submit = vi
      .spyOn(api.claudeAccount, "submitCode")
      .mockResolvedValue({ state: "connected", url: null, error: null });
    vi.spyOn(api.claudeAccount, "cancelLogin").mockResolvedValue(IDLE);
    renderTab();

    await userEvent.click(await screen.findByRole("button", { name: "Reconnect" }));
    const link = await screen.findByRole("link", { name: /Open the Claude sign-in page/ }, { timeout: 5000 });
    expect(link).toHaveAttribute("href", "https://claude.ai/oauth/authorize?state=xyz");
    expect(link).toHaveAttribute("target", "_blank");

    await userEvent.type(screen.getByLabelText("Code from Claude"), "abc123#xyz");
    await userEvent.click(screen.getByRole("button", { name: "Finish" }));

    await waitFor(() => expect(submit).toHaveBeenCalledWith("abc123#xyz"));
    await waitFor(() => expect(notify.success).toHaveBeenCalled());
  });

  it("a refused code shows the reason and offers to start again", async () => {
    vi.spyOn(api.claudeAccount, "status").mockResolvedValue(status());
    vi.spyOn(api.claudeAccount, "startLogin").mockResolvedValue({
      state: "waiting_for_code",
      url: "https://claude.ai/oauth/authorize?state=xyz",
      error: null,
    });
    vi.spyOn(api.claudeAccount, "loginStatus").mockResolvedValue({
      state: "waiting_for_code",
      url: "https://claude.ai/oauth/authorize?state=xyz",
      error: null,
    });
    vi.spyOn(api.claudeAccount, "submitCode").mockResolvedValue({
      state: "failed",
      url: null,
      error: "Claude did not accept the code. Start again and paste the whole code.",
    });
    renderTab();

    await userEvent.click(await screen.findByRole("button", { name: "Reconnect" }));
    await userEvent.type(await screen.findByLabelText("Code from Claude"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Finish" }));

    expect(await screen.findByTestId("claude-login-error")).toHaveTextContent("did not accept the code");
    expect(screen.getByRole("button", { name: "Start again" })).toBeInTheDocument();
  });
});

describe("ClaudeAccountTab — paste a token, disconnect", () => {
  it("saving a pasted token goes through the vault and then runs the test once", async () => {
    vi.spyOn(api.claudeAccount, "status").mockResolvedValue(status());
    const update = vi.spyOn(api.secrets, "update").mockResolvedValue({
      id: "s1", key: "claude_code_oauth_token", value_masked: "****wxyz",
      provider: "anthropic-claude-code", label: null, description: null, created_at: null, updated_at: null,
    });
    renderTab();

    await userEvent.click(await screen.findByRole("button", { name: "Paste a token instead" }));
    await userEvent.type(screen.getByLabelText("Token from claude setup-token"), "sk-ant-oat01-new");
    await userEvent.click(screen.getByRole("button", { name: "Save and test" }));

    await waitFor(() => expect(update).toHaveBeenCalledWith("claude_code_oauth_token", { value: "sk-ant-oat01-new" }));
    await waitFor(() => expect(api.configChecks.run).toHaveBeenCalledTimes(1));
  });

  it("a first token is created with the Claude template", async () => {
    vi.spyOn(api.claudeAccount, "status").mockResolvedValue(
      status({ configured: false, token_updated_at: null, check: null })
    );
    const create = vi.spyOn(api.secrets, "create").mockResolvedValue({
      id: "s1", key: "claude_code_oauth_token", value_masked: "****wxyz",
      provider: "anthropic-claude-code", label: null, description: null, created_at: null, updated_at: null,
    });
    renderTab();

    await userEvent.click(await screen.findByRole("button", { name: "Paste a token instead" }));
    await userEvent.type(screen.getByLabelText("Token from claude setup-token"), "sk-ant-oat01-first");
    await userEvent.click(screen.getByRole("button", { name: "Save and test" }));

    await waitFor(() =>
      expect(create).toHaveBeenCalledWith(
        expect.objectContaining({ key: "claude_code_oauth_token", value: "sk-ant-oat01-first", provider: "anthropic-claude-code" })
      )
    );
  });

  it("disconnect asks first, then deletes the vault token", async () => {
    vi.spyOn(api.claudeAccount, "status").mockResolvedValue(status());
    const del = vi.spyOn(api.secrets, "delete").mockResolvedValue(undefined);
    renderTab();

    await userEvent.click(await screen.findByRole("button", { name: "Disconnect" }));
    expect(del).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("claude_code_oauth_token"));
  });
});

