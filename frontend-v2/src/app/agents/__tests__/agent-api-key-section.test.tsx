/**
 * AgentApiKeySection — the per-agent provider key on the Config tab.
 *
 * It used to list every stored secret (other agents' MC tokens, chat and
 * social keys, …). The bound key is only ever sent as OPENAI_API_KEY to an
 * openai-protocol runtime, so the server says which secret provider fits
 * (agent_key_provider / agent_key_used) and only those keys are offered.
 * The select also must never show a value that is not the saved one.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AgentApiKeySection } from "../[id]/AgentApiKeySection";
import { api } from "@/lib/api";
import type { Agent, Runtime, SecretEntry } from "@/lib/types";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const secret = (id: string, key: string, provider: string | null, label: string): SecretEntry => ({
  id, key, provider, label, value_masked: "****", description: null, created_at: null, updated_at: null,
});

const SECRETS = [
  secret("s-ollama", "ollama_api_key", "ollama", "Cloud model key"),
  secret("s-agent", "mc_agent_token_beta", "mc-agent", "Agent Token: beta"),
  secret("s-chat", "slack_bot_token", "slack", "slack_bot_token"),
];

const rt = (over: Partial<Runtime>): Runtime =>
  ({ id: "rt-1", slug: "rt-1", display_name: "RT", runtime_type: "cloud", enabled: true, ...over }) as Runtime;

const agent = (over: Partial<Agent> = {}): Agent =>
  ({ id: "agent-1", name: "alpha", runtime_id: "rt-1", secret_id: null, ...over }) as Agent;

const optionLabels = (select: HTMLElement) =>
  within(select).getAllByRole("option").map((o) => o.textContent ?? "");

describe("AgentApiKeySection", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.secrets, "list").mockResolvedValue(SECRETS);
  });

  it("offers only keys of the runtime's provider — never other agents' tokens", async () => {
    vi.spyOn(api.runtimes, "list").mockResolvedValue({
      runtimes: [rt({ agent_key_used: true, agent_key_provider: "ollama" })],
    } as never);
    renderWithQuery(<AgentApiKeySection agent={agent()} agentId="agent-1" />);
    const select = await screen.findByRole("combobox", { name: "API key (provider)" });
    await waitFor(() => expect(optionLabels(select).some((l) => l.includes("Cloud model key"))).toBe(true));
    const labels = optionLabels(select);
    expect(labels.some((l) => l.includes("Agent Token"))).toBe(false);
    expect(labels.some((l) => l.includes("slack"))).toBe(false);
  });

  it("offers no keys when the runtime signs in on its own, and says so", async () => {
    vi.spyOn(api.runtimes, "list").mockResolvedValue({
      runtimes: [rt({ agent_key_used: false, agent_key_provider: null })],
    } as never);
    renderWithQuery(<AgentApiKeySection agent={agent()} agentId="agent-1" />);
    await screen.findByText(/signs in on its own/);
    const select = screen.getByRole("combobox", { name: "API key (provider)" });
    expect(optionLabels(select)).toHaveLength(1);
  });

  it("shows the SAVED key as selected even when it does not fit, marked as such", async () => {
    vi.spyOn(api.runtimes, "list").mockResolvedValue({
      runtimes: [rt({ agent_key_used: false, agent_key_provider: null })],
    } as never);
    renderWithQuery(<AgentApiKeySection agent={agent({ secret_id: "s-ollama" })} agentId="agent-1" />);
    const select = (await screen.findByRole("combobox", { name: "API key (provider)" })) as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe("s-ollama"));
    expect(select.selectedOptions[0].textContent).toMatch(/saved.*does not fit/i);
  });

  it("never shows an unsaved value while data is still loading", async () => {
    vi.spyOn(api.runtimes, "list").mockReturnValue(new Promise(() => {}) as never);
    vi.spyOn(api.secrets, "list").mockReturnValue(new Promise(() => {}) as never);
    renderWithQuery(<AgentApiKeySection agent={agent({ secret_id: "s-ollama" })} agentId="agent-1" />);
    const select = (await screen.findByRole("combobox", { name: "API key (provider)" })) as HTMLSelectElement;
    expect(select.value).toBe("s-ollama");
  });
});
