/**
 * New task → "Run tonight" (ROADMAP E2): the card is created (deferred) and
 * marked for the night shift instead of starting a head now.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CreateTaskModal } from "../CreateTaskModal";
import { api } from "@/lib/api";
import { pairKey, type HeadPairsResponse } from "@/lib/heads";
import { mkPair } from "@/lib/__tests__/headFixtures";
import type { Repo, Task } from "@/lib/types";

function renderModal(onOpenTask = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <CreateTaskModal activeBoardId="board-1" agents={[]} onOpenTask={onOpenTask} />
    </QueryClientProvider>,
  );
  return onOpenTask;
}

const repo: Repo = {
  id: "repo-1",
  full_name: "acme/tool",
  url: "https://github.com/acme/tool",
  default_branch: "main",
  description: null,
  rules_md: null,
  visibility: "private",
  is_active: true,
  source: "mc",
  last_synced_at: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  linked_projects: [],
};

const ompLocal = mkPair();
const claudeLocal = mkPair({ harness: "claude", harness_label: "Claude Code", status: "experimental" });
const ompCloud = mkPair({
  runtime_slug: "claude-sub", runtime_label: "Claude", locality: "cloud", status: "blocked",
  reason_code: "needs_operator_decision",
});
const ompQwenDown = mkPair({ runtime_slug: "qwen", runtime_label: "Qwen local", status: "blocked", reason_code: "engine_not_ready", live: false });

async function openAndFill({ withRepo = true } = {}) {
  await userEvent.click(screen.getByRole("button", { name: "New task" }));
  await userEvent.type(screen.getByPlaceholderText("Kurzer, klarer Aufgabentitel"), "Fix flaky retry test");
  if (withRepo) {
    const select = await screen.findByRole("combobox", { name: "Repository" });
    await userEvent.selectOptions(select, repo.id);
  }
}

describe("CreateTaskModal — Run tonight", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    try { window.localStorage.removeItem("mc.heads.lastPair"); } catch { /* storage may be unavailable */ }
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    vi.spyOn(api.projects, "list").mockResolvedValue([]);
    vi.spyOn(api.credentials, "list").mockResolvedValue([]);
    vi.spyOn(api.repos, "list").mockResolvedValue([repo]);
  });

  it("switch on → 'Queue for tonight' creates the card and marks it; no head starts now", async () => {
    // the local model is down right now — fine for tonight
    const localDown = mkPair({ status: "blocked", reason_code: "engine_not_ready", live: false });
    const resp: HeadPairsResponse = { pairs: [ompCloud, localDown], default_pair: localDown };
    vi.spyOn(api.heads, "pairs").mockResolvedValue(resp);
    const createSpy = vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-7" } as Task);
    const startSpy = vi.spyOn(api.heads, "start");
    const markSpy = vi.spyOn(api.nightShift, "mark").mockResolvedValue({ mark: {} as never });
    const onOpen = renderModal();

    await openAndFill();
    expect(await screen.findByTestId("run-as-head")).toBeDisabled(); // now: model down
    const sw = screen.getByRole("switch", { name: "Run tonight" });
    expect(sw.className).toContain("min-h-[44px]");
    await userEvent.click(sw);

    const queue = screen.getByTestId("queue-tonight");
    expect(queue).toHaveTextContent("Queue for tonight");
    expect(queue).toBeEnabled();
    expect(screen.getByTestId("head-pair-trigger")).toHaveTextContent("omp · GLM local");
    await userEvent.click(queue);

    await waitFor(() => expect(markSpy).toHaveBeenCalledWith("task-7", { harness: "omp", runtime_slug: "glm-local" }));
    expect(createSpy.mock.calls[0][1]).toMatchObject({ repo_id: "repo-1", defer_dispatch: true, status: "inbox" });
    expect(startSpy).not.toHaveBeenCalled();
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("task-7"));
  });

  it("a cloud pair is never pre-selected for tonight either", async () => {
    const cloudOk = mkPair({ runtime_slug: "cloud-x", runtime_label: "Cloud X", locality: "cloud", status: "ok", box_keys: [] });
    vi.spyOn(api.heads, "pairs").mockResolvedValue({ pairs: [cloudOk, ompLocal], default_pair: ompLocal });
    try { window.localStorage.setItem("mc.heads.lastPair", pairKey(cloudOk)); } catch { /* ignore */ }
    renderModal();
    await openAndFill();
    await userEvent.click(await screen.findByRole("switch", { name: "Run tonight" }));
    expect(screen.getByTestId("head-pair-trigger")).toHaveTextContent("omp · GLM local");
  });

  it("a failed mark keeps the modal open with the reason; the card is not re-created on retry", async () => {
    vi.spyOn(api.heads, "pairs").mockResolvedValue({ pairs: [ompLocal], default_pair: ompLocal });
    const createSpy = vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-8" } as Task);
    const markSpy = vi
      .spyOn(api.nightShift, "mark")
      .mockRejectedValueOnce(new Error('API 409: {"detail":{"code":"head_active"}}'))
      .mockResolvedValueOnce({ mark: {} as never });
    renderModal();
    await openAndFill();
    await userEvent.click(await screen.findByRole("switch", { name: "Run tonight" }));
    await userEvent.click(screen.getByTestId("queue-tonight"));
    expect(await screen.findByTestId("head-start-error")).toHaveTextContent(
      "Task created, but not queued for tonight: A head is already working on this task.",
    );
    expect(screen.queryByTestId("head-start-kept")).toBeNull();
    await userEvent.click(screen.getByTestId("queue-tonight"));
    await waitFor(() => expect(markSpy).toHaveBeenCalledTimes(2));
    expect(createSpy).toHaveBeenCalledTimes(1);
  });
});
