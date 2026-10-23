import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { apiErrorStatus, isMissingRecord } from "@/lib/apiStatus";

// Round-3 user test t4 S1: `/agents/<wrong id>` showed a spinner forever —
// the API answered 404 (422 for a non-uuid) at once, the page only checked
// `if (!agent)` and never looked at the error.

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/agents/00000000-0000-0000-0000-000000000000",
  useParams: () => ({ id: "00000000-0000-0000-0000-000000000000" }),
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/components/layout/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/hooks/useAgentStream", () => ({ useAgentStream: () => undefined }));

import AgentDetailPage from "../[id]/page";

function renderPage() {
  // The app's default (providers.tsx) retries once; the page must decide
  // for itself that a missing record is not worth a retry.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: 1, retryDelay: 1 } } });
  return render(
    <QueryClientProvider client={qc}>
      <AgentDetailPage />
    </QueryClientProvider>,
  );
}

describe("apiErrorStatus / isMissingRecord", () => {
  it("reads the status from lib/api's error message", () => {
    expect(apiErrorStatus(new Error('API 404: {"detail":"Agent not found"}'))).toBe(404);
    expect(apiErrorStatus(new Error("Failed to fetch"))).toBeNull();
    expect(apiErrorStatus("nope")).toBeNull();
  });
  it("404 and 422 mean the record is not there; 500 does not", () => {
    expect(isMissingRecord(new Error("API 404: x"))).toBe(true);
    expect(isMissingRecord(new Error("API 422: x"))).toBe(true);
    expect(isMissingRecord(new Error("API 500: x"))).toBe(false);
  });
});

describe("Agent detail — a missing agent", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("says 'Agent not found' with a way back instead of spinning (404)", async () => {
    const get = vi.spyOn(api.agents, "get").mockRejectedValue(new Error('API 404: {"detail":"Agent not found"}'));
    renderPage();
    expect(await screen.findByText("Agent not found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Back to agents/ })).toHaveAttribute("href", "/agents");
    // Round-4 r2 N4: the title is the page's h1 (screen readers found none).
    expect(screen.getByRole("heading", { level: 1, name: "Agent not found" })).toBeInTheDocument();
    // No retry for a record that does not exist.
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("treats a malformed id (422) the same way", async () => {
    vi.spyOn(api.agents, "get").mockRejectedValue(new Error("API 422: {}"));
    renderPage();
    expect(await screen.findByText("Agent not found")).toBeInTheDocument();
  });

  it("a failed load says so — not 'not found'", async () => {
    vi.spyOn(api.agents, "get").mockRejectedValue(new Error("API 500: boom"));
    renderPage();
    expect(await screen.findByText("Could not load this agent.")).toBeInTheDocument();
    expect(screen.queryByText("Agent not found")).not.toBeInTheDocument();
  });
});
