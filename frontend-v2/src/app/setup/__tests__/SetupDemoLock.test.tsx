import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api } from "@/lib/api";

// /setup stays reachable on an installed system, and one click on
// "Create demo board" wrote a demo board, four agents and eight tasks into
// live data. The wizard itself creates the first board only in this very
// step (seedDemo) — registration creates none — so "boards or agents
// already exist" reliably means "not a fresh install". No redirect: a new
// install must be able to finish the wizard.

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
}

async function goToStep4() {
  render(<SetupWizardPage />);
  await screen.findByText("Connect an LLM provider");
  await userEvent.click(screen.getByRole("button", { name: "Skip" }));
  await screen.findByRole("heading", { name: "Connect GitHub" });
  await userEvent.click(screen.getByRole("button", { name: "Skip for now" }));
  await screen.findByText("Ready to get started");
}

describe("Setup wizard — demo data only on a fresh install", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    replace.mockReset();
    mockAuthed();
    vi.spyOn(api.secrets, "providers").mockResolvedValue([] as never);
  });

  it("locks 'Create demo board' when boards already exist, and explains why", async () => {
    vi.spyOn(api.boards, "list").mockResolvedValue([{ id: "b1" }] as never);
    vi.spyOn(api.agents, "list").mockResolvedValue([] as never);
    const create = vi.spyOn(api.boards, "create").mockResolvedValue({ id: "b2" } as never);

    await goToStep4();
    const btn = await screen.findByRole("button", { name: /Create demo board/ });
    expect(btn).toBeDisabled();
    expect(screen.getByText(/already has boards or agents/)).toBeInTheDocument();
    await userEvent.click(btn);
    expect(create).not.toHaveBeenCalled();
    // No redirect away from the wizard.
    expect(replace).not.toHaveBeenCalled();
  });

  it("locks it when agents already exist", async () => {
    vi.spyOn(api.boards, "list").mockResolvedValue([] as never);
    vi.spyOn(api.agents, "list").mockResolvedValue([{ id: "a1" }] as never);
    await goToStep4();
    expect(await screen.findByRole("button", { name: /Create demo board/ })).toBeDisabled();
  });

  it("keeps it available on a fresh install", async () => {
    vi.spyOn(api.boards, "list").mockResolvedValue([] as never);
    vi.spyOn(api.agents, "list").mockResolvedValue([] as never);
    await goToStep4();
    expect(await screen.findByRole("button", { name: /Create demo board/ })).toBeEnabled();
    expect(screen.queryByText(/already has boards or agents/)).not.toBeInTheDocument();
  });
});
