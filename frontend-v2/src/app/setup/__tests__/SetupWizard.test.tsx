import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
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

  it("saves the provider key and advances to step 3", async () => {
    mockAuthed();
    vi.spyOn(api.secrets, "providers").mockResolvedValue(PROVIDERS as never);
    const create = vi.spyOn(api.secrets, "create").mockResolvedValue({} as never);

    render(<SetupWizardPage />);
    await screen.findByText("Connect an LLM provider");

    await userEvent.type(screen.getByLabelText("Key"), "sk-ant-oat01-xyz");
    await userEvent.click(screen.getByRole("button", { name: /Save & continue/ }));

    await screen.findByText("Ready to get started");
    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({
        key: "claude_code_oauth_token",
        value: "sk-ant-oat01-xyz",
        provider: "anthropic-claude-code",
      }),
    );
  });

  it("skip goes to step 3; demo seed creates board + 8 tasks", async () => {
    mockAuthed();
    vi.spyOn(api.secrets, "providers").mockResolvedValue(PROVIDERS as never);
    const board = vi
      .spyOn(api.boards, "create")
      .mockResolvedValue({ id: "b1" } as never);
    const task = vi.spyOn(api.tasks, "create").mockResolvedValue({} as never);

    render(<SetupWizardPage />);
    await screen.findByText("Connect an LLM provider");
    await userEvent.click(screen.getByRole("button", { name: "Skip" }));

    await userEvent.click(
      screen.getByRole("button", { name: /Create demo board/ }),
    );
    await screen.findByText("Demo board created");
    expect(board).toHaveBeenCalledTimes(1);
    expect(task).toHaveBeenCalledTimes(8);

    await userEvent.click(screen.getByRole("button", { name: /Go to command center/ }));
    expect(replace).toHaveBeenCalledWith("/");
  });
});
