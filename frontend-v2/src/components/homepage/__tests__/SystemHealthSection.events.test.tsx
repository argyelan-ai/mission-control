/** Home's event stream shows no plumbing events (silent retry, attempt ids). */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ActivityEvent } from "@/lib/types";
import { TECHNICAL_EVENT_TYPES } from "@/lib/operatorEvents";

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

  it("asks the server to leave plumbing types out, so they cannot fill the short list", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <SystemHealthSection status={STATUS} loading={false} onOpenActivity={() => {}} />
      </QueryClientProvider>,
    );
    await screen.findByText("Docs finished");
    const params = vi.mocked(api.activity.list).mock.calls[0][0];
    expect(params?.exclude_event_type).toEqual([...TECHNICAL_EVENT_TYPES]);
  });

  it("the API sends every excluded type as its own query param", async () => {
    vi.mocked(api.activity.list).mockRestore();
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    await api.activity.list({ board_id: "b1", limit: 15, exclude_event_type: ["a.x", "b.y"] });
    const url = new URL(String(fetchSpy.mock.calls[0][0]), "http://x");
    expect(url.searchParams.getAll("exclude_event_type")).toEqual(["a.x", "b.y"]);
    expect(url.searchParams.get("limit")).toBe("15");
  });
});
