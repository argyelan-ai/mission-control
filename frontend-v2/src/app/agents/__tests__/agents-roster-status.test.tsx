/**
 * Agents roster — one state per row (round-2 user test t4 S5/S-neu-2).
 *
 * The header said "2 active · 3 paused" and no row said which, and a
 * working agent ("working" from the heartbeat) got the grey offline dot.
 * Each row now names its pause on the second line (never squeezing the name
 * on a phone) and shows a context that holds tokens as "<1%", not "0%".
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Agent } from "@/lib/types";

vi.mock("@/components/layout/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock("@/lib/sse", async (orig) => ({ ...(await orig<typeof import("@/lib/sse")>()), useAgentStream: () => {} }));

import AgentsPage from "../page";

const agent = (over: Partial<Agent>): Agent =>
  ({
    id: "a", name: "Alpha", role: "developer", status: "idle", operational_mode: "active",
    provision_status: "provisioned", agent_runtime: "cli-bridge", runtime_id: null,
    context_tokens: null, context_max: 1_048_576, total_tasks_completed: 0, scopes: [],
    heartbeat_config: { interval: "5m" }, ...over,
  }) as Agent;

describe("Agents roster status", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.boards, "list").mockResolvedValue([] as never);
    vi.spyOn(api.agents, "list").mockResolvedValue([
      agent({ id: "w", name: "Worker", status: "working", operational_mode: "paused", context_tokens: 4_200 }),
      agent({ id: "i", name: "Idler", status: "idle", operational_mode: "active", context_tokens: 20_972 }),
    ]);
  });

  it("marks the paused row on its second line and keeps the name whole", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><AgentsPage /></QueryClientProvider>);

    const worker = (await screen.findByRole("link", { name: "Open agent: Worker" })).closest("div") as HTMLElement;
    expect(within(worker).getByText("Worker")).toBeInTheDocument();
    const marker = within(worker).getByTestId("agent-row-paused");
    expect(marker).toHaveTextContent("· Paused");
    expect(marker).toHaveAttribute("title", expect.stringContaining("no new tasks"));
    expect(within(worker).getByText("<1%")).toBeInTheDocument();

    const idler = (await screen.findByRole("link", { name: "Open agent: Idler" })).closest("div") as HTMLElement;
    expect(within(idler).queryByTestId("agent-row-paused")).not.toBeInTheDocument();
    expect(within(idler).getByText("2%")).toBeInTheDocument();
  });

  // Round-4 K5 (t5 NICE-1): a long role ("Workhorse Developer — local coding
  // tasks, …") cut "· Paused" off on the phone (100 px of 409). The state
  // stands on its own and never truncates; only the role shortens.
  it("keeps the paused state readable behind a long role", async () => {
    vi.spyOn(api.agents, "list").mockResolvedValue([
      agent({
        id: "s", name: "Speedy", operational_mode: "paused",
        role: "Workhorse Developer — local coding tasks, quick turnaround, no overhead",
      }),
    ]);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><AgentsPage /></QueryClientProvider>);

    const row = (await screen.findByRole("link", { name: "Open agent: Speedy" })).closest("div") as HTMLElement;
    const marker = within(row).getByTestId("agent-row-paused");
    expect(marker).toHaveTextContent("Paused");
    // Not inside the part that truncates, and never shrinks itself.
    expect(marker.className).toMatch(/\bshrink-0\b/);
    let el: HTMLElement | null = marker;
    while (el && el !== row) {
      expect(el.className, el.outerHTML.slice(0, 120)).not.toMatch(/\btruncate\b/);
      el = el.parentElement;
    }
    const role = within(row).getByText(/Workhorse Developer/);
    expect(role.className).toMatch(/\btruncate\b/);
    expect(role).not.toContainElement(marker);
  });
});
