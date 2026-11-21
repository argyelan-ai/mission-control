/** Home's event stream shows no plumbing events (silent retry, attempt ids). */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ActivityEvent } from "@/lib/types";

vi.mock("@/lib/store", () => ({
  useAppStore: (sel?: (s: { activeBoardId: string }) => unknown) => {
    const s = { activeBoardId: "b1" };
    return sel ? sel(s) : s;
  },
}));

const { SystemHealthSection } = await import("../SystemHealthSection");

const ev = (id: string, event_type: string, title: string) =>
  ({ id, event_type, title, board_id: "b1", task_id: null, agent_id: null, project_id: null, detail: null, severity: "info", created_at: new Date().toISOString() }) as ActivityEvent;

const STATUS = {
  components: {
    database: { status: "ok", latency_ms: 1 },
    redis: { status: "ok", latency_ms: 1 },
    watchdog: { status: "ok" },
  },
  resources: null,
} as never;

describe("Home event stream", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.system, "metricsHistory").mockResolvedValue({ points: [] } as never);
    vi.spyOn(api.activity, "list").mockResolvedValue([
      ev("1", "task.dispatch_attempt_rotated", "Silent retry: dispatch_attempt_id rotiert für 'Docs' nach 6min ohne ACK"),
      ev("2", "task.done", "Docs finished"),
      ev("3", "task.stale_update_warning", "Stale Update von A auf 'Docs' (falsche dispatch_attempt_id, Phase A: durchgelassen)"),
    ]);
  });

  it("lists operator events and leaves plumbing out", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <SystemHealthSection status={STATUS} loading={false} onOpenActivity={() => {}} />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Docs finished")).toBeInTheDocument();
    expect(screen.queryByText(/dispatch_attempt_id|Silent retry/)).not.toBeInTheDocument();
  });
});
