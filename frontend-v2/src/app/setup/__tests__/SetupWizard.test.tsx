import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api } from "@/lib/api";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn() }),
  usePathname: () => "/setup",
}));

import SetupWizardPage from "../page";

function mockAuthed() {
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
  return store;
}

const PROVIDERS = [
  {
    provider: "anthropic-claude-code",
    key: "claude_code_oauth_token",
    label: "Claude Code OAuth Token",
    description: "For cli-bridge agents",
    placeholder: "sk-ant-oat01-...",
  },
];

function checkItem(
  state: "ok" | "fail" | "never_tested",
  kind = "secret",
  over: Record<string, unknown> = {},
) {
  return {
    kind,
    subject_id: kind === "github" ? "connection" : "claude_code_oauth_token",
    label: "x",
    secret_key: null,
    configured: true,
    testable: true,
    cost: "quota" as const,
    state,
    reason: state === "fail" ? "rejected" : state === "ok" ? "ok" : null,
    reason_params: (state === "fail" ? { status: 401 } : {}) as Record<string, number>,
    message: state === "fail" ? "Claude rejected the key (HTTP 401) — expired, revoked or mistyped?" : null,
    duration_ms: 120,
    checked_at: state === "never_tested" ? null : new Date().toISOString(),
    age_seconds: state === "never_tested" ? null : 0,
    value_changed: false,
    ...over,
  };
}

describe("First-Run-Wizard", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    replace.mockReset();
  });

  it("redirects to /login without a token", async () => {
    const store = mockAuthed();
    delete store.mc_auth_token;
    vi.spyOn(api.secrets, "providers").mockResolvedValue(PROVIDERS as never);
    render(<SetupWizardPage />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
  });

  it("saves the provider key and advances to the Connect GitHub step", async () => {
    mockAuthed();
    vi.spyOn(api.secrets, "providers").mockResolvedValue(PROVIDERS as never);
    const create = vi.spyOn(api.secrets, "create").mockResolvedValue({} as never);
    const run = vi.spyOn(api.configChecks, "run").mockResolvedValue({ item: checkItem("ok"), notRunnable: null });

    render(<SetupWizardPage />);
    await screen.findByText("Connect an LLM provider");

    await userEvent.type(screen.getByLabelText("Key"), "sk-ant-oat01-xyz");
    await userEvent.click(screen.getByRole("button", { name: /Save & continue/ }));

    await screen.findByRole("heading", { name: "Connect GitHub" });
    // Test on save (ADR-093 §2): the key is checked before the wizard moves on.
    expect(run).toHaveBeenCalledWith("secret", "claude_code_oauth_token");
    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({
        key: "claude_code_oauth_token",
        value: "sk-ant-oat01-xyz",
        provider: "anthropic-claude-code",
      }),
    );
  });

  it("skip provider key, skip GitHub, then demo seed creates board + crew + 8 tasks", async () => {
    mockAuthed();
    vi.spyOn(api.secrets, "providers").mockResolvedValue(PROVIDERS as never);
    const board = vi
      .spyOn(api.boards, "create")
      .mockResolvedValue({ id: "b1" } as never);
    const agent = vi
      .spyOn(api.agents, "create")
      .mockResolvedValue({ id: "a1" } as never);
    const task = vi.spyOn(api.tasks, "create").mockResolvedValue({} as never);
    const done = vi
      .spyOn(api.auth, "completeFirstRun")
      .mockResolvedValue({ wizard_pending: false });

    render(<SetupWizardPage />);
    await screen.findByText("Connect an LLM provider");
    await userEvent.click(screen.getByRole("button", { name: "Skip" }));

    await screen.findByRole("heading", { name: "Connect GitHub" });
    // Not done yet: the operator is still inside the configuration steps.
    expect(done).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Skip for now" }));

    await screen.findByText("Ready to get started");
    // Last step reached: the app stops sending the operator back to /setup.
    await waitFor(() => expect(done).toHaveBeenCalled());
    await userEvent.click(
      screen.getByRole("button", { name: /Create demo board/ }),
    );
    await screen.findByText("Demo board created");
    expect(board).toHaveBeenCalledTimes(1);
    expect(agent).toHaveBeenCalledTimes(4);
    expect(task).toHaveBeenCalledTimes(8);
    // Assigned tasks carry the crew's agent id; inbox tasks stay unassigned.
    const bodies = task.mock.calls.map((c) => c[1] as Record<string, unknown>);
    expect(bodies.filter((b) => b.assigned_agent_id === "a1")).toHaveLength(6);

    await userEvent.click(screen.getByRole("button", { name: /Go to command center/ }));
    expect(replace).toHaveBeenCalledWith("/");
  });

  it("Connect GitHub step saves owner + token via PUT github-config and advances", async () => {
    mockAuthed();
    vi.spyOn(api.secrets, "providers").mockResolvedValue(PROVIDERS as never);
    const setGithub = vi.spyOn(api.repos, "setGithubConfig").mockResolvedValue({} as never);
    vi.spyOn(api.configChecks, "run").mockResolvedValue({ item: checkItem("ok", "github"), notRunnable: null });

    render(<SetupWizardPage />);
    await screen.findByText("Connect an LLM provider");
    await userEvent.click(screen.getByRole("button", { name: "Skip" }));

    await screen.findByRole("heading", { name: "Connect GitHub" });
    await userEvent.type(screen.getByLabelText("Owner"), "acme");
    await userEvent.type(screen.getByLabelText("Personal access token"), "ghp_xyz123");
    await userEvent.click(screen.getByRole("button", { name: /Save & continue/ }));

    await screen.findByText("Ready to get started");
    expect(setGithub).toHaveBeenCalledWith({ owner: "acme", token: "ghp_xyz123" });
  });

  it("an invalid key: no 'continue', a clear reason — then a corrected key passes", async () => {
    mockAuthed();
    vi.spyOn(api.secrets, "providers").mockResolvedValue(PROVIDERS as never);
    const create = vi.spyOn(api.secrets, "create").mockResolvedValue({} as never);
    const update = vi.spyOn(api.secrets, "update").mockResolvedValue({} as never);
    const run = vi
      .spyOn(api.configChecks, "run")
      .mockResolvedValueOnce({ item: checkItem("fail"), notRunnable: null })
      .mockResolvedValueOnce({ item: checkItem("ok"), notRunnable: null });

    render(<SetupWizardPage />);
    await screen.findByText("Connect an LLM provider");

    await userEvent.type(screen.getByLabelText("Key"), "sk-ant-oat01-wrong");
    await userEvent.click(screen.getByRole("button", { name: /Save & continue/ }));

    const keyError = await screen.findByTestId("setup-key-error");
    expect(keyError).toHaveTextContent("The key did not work.");
    // Worded from the reason code, not the server's sentence (finding 1).
    expect(keyError).toHaveTextContent("Rejected (HTTP 401) — the key is expired, revoked or mistyped.");
    expect(keyError).not.toHaveTextContent("Claude rejected the key");
    // Still on the key step, and the same key cannot be sent on again.
    expect(screen.getByRole("heading", { name: "Connect an LLM provider" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Save & continue/ })).toBeDisabled();
    // Skipping stays possible.
    expect(screen.getByRole("button", { name: "Skip" })).toBeEnabled();

    // A corrected key: the stored entry is replaced (not created twice) and tested again.
    await userEvent.clear(screen.getByLabelText("Key"));
    await userEvent.type(screen.getByLabelText("Key"), "sk-ant-oat01-right");
    await userEvent.click(screen.getByRole("button", { name: /Save & continue/ }));

    await screen.findByRole("heading", { name: "Connect GitHub" });
    expect(create).toHaveBeenCalledTimes(1);
    expect(update).toHaveBeenCalledWith("claude_code_oauth_token", { value: "sk-ant-oat01-right" });
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("a provider without a test procedure is saved and the wizard moves on", async () => {
    mockAuthed();
    vi.spyOn(api.secrets, "providers").mockResolvedValue(PROVIDERS as never);
    vi.spyOn(api.secrets, "create").mockResolvedValue({} as never);
    vi.spyOn(api.configChecks, "run").mockResolvedValue({
      item: checkItem("never_tested", "secret", { testable: false }),
      notRunnable: "not_testable",
    });

    render(<SetupWizardPage />);
    await screen.findByText("Connect an LLM provider");
    await userEvent.type(screen.getByLabelText("Key"), "some-key");
    await userEvent.click(screen.getByRole("button", { name: /Save & continue/ }));

    await screen.findByRole("heading", { name: "Connect GitHub" });
  });

  it("a test that cannot run blocks 'continue' with the reason, skip stays", async () => {
    mockAuthed();
    vi.spyOn(api.secrets, "providers").mockResolvedValue(PROVIDERS as never);
    vi.spyOn(api.secrets, "create").mockResolvedValue({} as never);
    vi.spyOn(api.configChecks, "run").mockRejectedValue(new Error("API 500: boom"));

    render(<SetupWizardPage />);
    await screen.findByText("Connect an LLM provider");
    await userEvent.type(screen.getByLabelText("Key"), "sk-ant-oat01-xyz");
    await userEvent.click(screen.getByRole("button", { name: /Save & continue/ }));

    expect(await screen.findByText(/the test could not run: API 500: boom/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Connect an LLM provider" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Skip" })).toBeEnabled();
  });

  it("GitHub step: a rejected token keeps the wizard on the step with GitHub's reason", async () => {
    mockAuthed();
    vi.spyOn(api.secrets, "providers").mockResolvedValue(PROVIDERS as never);
    vi.spyOn(api.repos, "setGithubConfig").mockResolvedValue({} as never);
    const run = vi.spyOn(api.configChecks, "run").mockResolvedValue({
      item: checkItem("fail", "github", { message: "gh: Bad credentials (HTTP 401)", reason: "unknown", reason_params: {} }),
      notRunnable: null,
    });

    render(<SetupWizardPage />);
    await screen.findByText("Connect an LLM provider");
    await userEvent.click(screen.getByRole("button", { name: "Skip" }));
    await screen.findByRole("heading", { name: "Connect GitHub" });
    await userEvent.type(screen.getByLabelText("Owner"), "acme");
    await userEvent.type(screen.getByLabelText("Personal access token"), "ghp_bad");
    await userEvent.click(screen.getByRole("button", { name: /Save & continue/ }));

    const ghError = await screen.findByTestId("setup-github-error");
    expect(ghError).toHaveTextContent("GitHub did not accept this.");
    expect(ghError).toHaveTextContent("The test failed for a reason MC does not know.");
    // An unknown reason keeps the server's text — folded away as a detail.
    expect(within(ghError).getByTestId("check-detail")).toHaveTextContent("gh: Bad credentials");
    expect(run).toHaveBeenCalledWith("github", "connection");
    expect(screen.getByRole("heading", { name: "Connect GitHub" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Save & continue/ })).toBeDisabled();
  });

  it("on a phone the step bar is one line: 'Step 2 of 4 · Provider Key'", async () => {
    mockAuthed();
    vi.spyOn(api.secrets, "providers").mockResolvedValue(PROVIDERS as never);
    render(<SetupWizardPage />);
    await screen.findByText("Connect an LLM provider");
    const compact = screen.getByTestId("setup-steps-compact");
    expect(compact).toHaveTextContent("Step 2 of 4 · Provider Key");
    // The compact bar is the phone's; the labelled row only from md up.
    expect(compact.className).toMatch(/(^|\s)md:hidden(\s|$)/);
    expect(screen.getByTestId("setup-steps").className).toMatch(/(^|\s)hidden md:flex(\s|$)/);
  });
});
