/** Schedule page: title, its underline and the content share one left edge (K9). */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";

vi.mock("@/components/layout/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock("@/lib/sse", () => ({ useSSE: () => {} }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

import SchedulePage from "../page";

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api.schedule, "listJobs").mockResolvedValue([]);
  vi.spyOn(api.agents, "list").mockResolvedValue([]);
  vi.spyOn(api.boards, "list").mockResolvedValue([]);
});

describe("SchedulePage edges", () => {
  it("draws the title underline from the title's own edge, and the content starts there too", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <SchedulePage />
      </QueryClientProvider>,
    );
    const rule = screen.getByTestId("schedule-title-rule");
    expect(rule.className).not.toMatch(/\bmx-\d/);
    const content = screen.getByTestId("schedule-overview");
    expect(content.innerHTML).not.toMatch(/class="px-6/);
  });
});
