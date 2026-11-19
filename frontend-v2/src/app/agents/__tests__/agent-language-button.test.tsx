import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Agent } from "@/lib/types";

// Settings → Language (16.11.2026): the agent header lost its two unlabelled
// "Op"/"Work" selects; one entry ("German to you") opens the same sheet the
// settings list uses, with a link back to all agents.

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/agents/a1",
  useParams: () => ({ id: "a1" }),
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/components/layout/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/hooks/useAgentStream", () => ({ useAgentStream: () => undefined }));

import AgentDetailPage from "../[id]/page";

const AGENT = {
  id: "a1", name: "Lead", emoji: "🧭", status: "online", provision_status: "provisioned",
  agent_runtime: "cli-bridge", operator_language: "de", work_language: "en",
  heartbeat_config: { interval: "5m" }, context_tokens: 0, context_max: 200000,
  scopes: [], skills: [], skill_filter: null, cli_plugins: null,
} as unknown as Agent;

describe("Agent detail — language entry", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("shows one language entry instead of the Op/Work selects and opens the sheet", async () => {
    vi.spyOn(api.agents, "get").mockResolvedValue(AGENT);
    const update = vi.spyOn(api.agents, "update").mockResolvedValue(AGENT);
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <AgentDetailPage />
      </QueryClientProvider>,
    );
    const button = await screen.findByTestId("agent-language-button");
    expect(button).toHaveTextContent("German to you");
    expect(screen.queryByText(/^Op:?$/)).not.toBeInTheDocument();

    await userEvent.click(button);
    const sheet = await screen.findByRole("dialog", { name: "Languages of Lead" });
    expect(within(sheet).getByRole("link", { name: "Language settings" })).toHaveAttribute("href", "/settings?section=language");
    const toYou = within(sheet).getByRole("radiogroup", { name: "To you" });
    await userEvent.click(within(toYou).getByRole("radio", { name: "English" }));
    expect(update).toHaveBeenCalledWith("a1", { operator_language: "en" });
  });
});
