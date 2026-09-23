/**
 * /schedule/<id> for a job that does not exist (deleted, mistyped link).
 *
 * The page looks the job up in the jobs list and used to treat "not in the
 * list" the same as "still loading": an endless "Loading job…" spinner.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useParams: () => ({ jobId: "missing-job" }),
}));
vi.mock("@/components/layout/AppShell", () => ({
  __esModule: true,
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/lib/store", () => ({
  useAppStore: (sel: (s: { activeBoardId: string | null }) => unknown) => sel({ activeBoardId: null }),
}));
vi.mock("@/lib/api", () => ({
  api: {
    schedule: {
      listJobs: vi.fn().mockResolvedValue([]),
      getStats: vi.fn().mockResolvedValue(null),
      getRuns: vi.fn().mockResolvedValue([]),
      getCreatedTasks: vi.fn().mockResolvedValue([]),
      updateJob: vi.fn(),
    },
    agents: { list: vi.fn().mockResolvedValue([]) },
    boards: { list: vi.fn().mockResolvedValue([]) },
  },
}));

import JobDetailPage from "../page";

describe("Job detail — unknown job", () => {
  it("says the job was not found and links back instead of spinning forever", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <JobDetailPage />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Job not found")).toBeInTheDocument();
    expect(screen.queryByText("Loading job…")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Back to schedule/i })).toHaveAttribute("href", "/schedule");
    // Only way out of this dead end on a phone: 44px touch target.
    expect(screen.getByRole("link", { name: /Back to schedule/i }).className).toMatch(/\bmin-h-11\b/);
  });
});
