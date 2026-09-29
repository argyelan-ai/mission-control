/**
 * New task → pair picker + "Run as head" (docs/specs/head-launcher.md §8.1,
 * build plan B1, B3, B4).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { readFileSync } from "fs";
import path from "path";
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

describe("CreateTaskModal — Run as head", () => {
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

  it("pre-selects the local default pair and starts the head: create (deferred) → POST /heads → open detail", async () => {
    const resp: HeadPairsResponse = { pairs: [ompCloud, claudeLocal, ompLocal], default_pair: ompLocal };
    vi.spyOn(api.heads, "pairs").mockResolvedValue(resp);
    const createSpy = vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-9" } as Task);
    const dispatchSpy = vi.spyOn(api.tasks, "dispatchDeferred").mockResolvedValue(undefined as never);
    const startSpy = vi.spyOn(api.heads, "start").mockResolvedValue({ run_id: "r1", state: "starting" });
    const onOpen = renderModal();

    await openAndFill();
    const trigger = await screen.findByTestId("head-pair-trigger");
    expect(trigger).toHaveTextContent("omp · GLM local");
    expect(screen.getByTestId("head-pair-hints")).toHaveTextContent("costs no Claude quota");

    await userEvent.click(screen.getByTestId("run-as-head"));

    await waitFor(() =>
      expect(startSpy).toHaveBeenCalledWith({ task_id: "task-9", harness: "omp", runtime_slug: "glm-local", hold_on_failure: true }),
    );
    expect(createSpy.mock.calls[0][1]).toMatchObject({ repo_id: "repo-1", defer_dispatch: true, status: "inbox" });
    expect(dispatchSpy).not.toHaveBeenCalled();
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("task-9"));
  });

  it("lists only startable pairs first; the rest behind 'Show more' with one plain sentence each", async () => {
    vi.spyOn(api.heads, "pairs").mockResolvedValue({ pairs: [ompCloud, claudeLocal, ompQwenDown, ompLocal], default_pair: ompLocal });
    renderModal();
    await openAndFill();

    await userEvent.click(await screen.findByTestId("head-pair-trigger"));
    const list = screen.getByRole("listbox");
    expect(within(list).getAllByRole("option").map((o) => o.getAttribute("data-testid"))).toEqual([
      `head-pair-option-${pairKey(ompLocal)}`,
      `head-pair-option-${pairKey(claudeLocal)}`,
    ]);
    await userEvent.click(screen.getByRole("button", { name: "Show more (2)" }));
    const cloudRow = screen.getByTestId(`head-pair-option-${pairKey(ompCloud)}`);
    expect(cloudRow).toBeDisabled();
    expect(cloudRow).toHaveTextContent("Not possible yet — needs an operator decision.");
    expect(screen.getByTestId(`head-pair-option-${pairKey(ompQwenDown)}`)).toHaveTextContent("The model is not running. Start it on Runtimes.");

    // Choosing the experimental pair sends it.
    await userEvent.click(screen.getByTestId(`head-pair-option-${pairKey(claudeLocal)}`));
    expect(screen.getByTestId("head-pair-trigger")).toHaveTextContent("Claude Code · GLM local");
  });

  it("default stays local when its engine is down: Run as head disabled + Runtimes link, never a cloud pair", async () => {
    const localDown = mkPair({ status: "blocked", reason_code: "engine_not_ready", live: false });
    const cloudOk = mkPair({ runtime_slug: "cloud-x", runtime_label: "Cloud X", locality: "cloud", status: "ok" });
    vi.spyOn(api.heads, "pairs").mockResolvedValue({ pairs: [cloudOk, localDown], default_pair: localDown });
    renderModal();
    await openAndFill();

    const trigger = await screen.findByTestId("head-pair-trigger");
    expect(trigger).toHaveTextContent("omp · GLM local");
    expect(trigger).not.toHaveTextContent("Cloud X");
    expect(screen.getByTestId("run-as-head")).toBeDisabled();
    expect(screen.getByTestId("head-engine-down")).toHaveTextContent("The local model is not running");
    expect(within(screen.getByTestId("head-engine-down")).getByRole("link", { name: "Runtimes" })).toHaveAttribute("href", "/runtimes");
  });

  it("Esc closes only the pair list, not the modal; arrows move between startable pairs", async () => {
    vi.spyOn(api.heads, "pairs").mockResolvedValue({ pairs: [ompLocal, claudeLocal, ompCloud], default_pair: ompLocal });
    renderModal();
    await openAndFill();
    const trigger = await screen.findByTestId("head-pair-trigger");
    expect(trigger).toHaveAccessibleName(/Pair/);
    expect(trigger).not.toHaveAttribute("aria-label");
    await userEvent.click(trigger);
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    await userEvent.keyboard("{ArrowDown}");
    expect(screen.getByTestId(`head-pair-option-${pairKey(ompLocal)}`)).toHaveFocus();
    await userEvent.keyboard("{ArrowDown}");
    expect(screen.getByTestId(`head-pair-option-${pairKey(claudeLocal)}`)).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(screen.getByTestId("head-section")).toBeInTheDocument(); // modal still open
    expect(screen.queryByText("Discard draft?")).not.toBeInTheDocument();
  });

  it("ignores a remembered pair that is no longer startable", async () => {
    const claudeBusy = { ...claudeLocal, status: "blocked" as const, reason_code: "box_busy", startable: false, busy_by: null };
    try { window.localStorage.setItem("mc.heads.lastPair", pairKey(claudeLocal)); } catch { /* ignore */ }
    vi.spyOn(api.heads, "pairs").mockResolvedValue({ pairs: [ompLocal, claudeBusy], default_pair: ompLocal });
    renderModal();
    await openAndFill();
    expect(await screen.findByTestId("head-pair-trigger")).toHaveTextContent("omp · GLM local");
  });

  it("no repo → Run as head disabled with the reason, main button stays 'Create task'", async () => {
    vi.spyOn(api.heads, "pairs").mockResolvedValue({ pairs: [ompLocal], default_pair: ompLocal });
    renderModal();
    await openAndFill({ withRepo: false });

    expect(await screen.findByTestId("head-needs-repo")).toHaveTextContent("A head needs a repo");
    expect(screen.getByTestId("run-as-head")).toBeDisabled();
    expect(screen.getByTestId("create-task-submit")).toHaveTextContent("Create task");
  });

  it("a failed start keeps the modal open with the error sentence; the retry only repeats the start", async () => {
    vi.spyOn(api.heads, "pairs").mockResolvedValue({ pairs: [ompLocal], default_pair: ompLocal });
    const createSpy = vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-9" } as Task);
    const startSpy = vi
      .spyOn(api.heads, "start")
      .mockRejectedValueOnce(new Error(`API 503: ${JSON.stringify({ detail: { code: "spool_unavailable" } })}`))
      .mockResolvedValueOnce({ run_id: "r1", state: "starting" });
    const onOpen = renderModal();
    await openAndFill();

    await userEvent.click(await screen.findByTestId("run-as-head"));
    expect(await screen.findByTestId("head-start-error")).toHaveTextContent("The head starter cannot be reached (spool folder).");
    expect(onOpen).not.toHaveBeenCalled();

    await userEvent.click(screen.getByTestId("run-as-head"));
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("task-9"));
    expect(createSpy).toHaveBeenCalledTimes(1);
    expect(startSpy).toHaveBeenCalledTimes(2);
  });

  it("after a failed start nothing hands the card to the fleet: no 'Retry uploads', no dispatch", async () => {
    vi.spyOn(api.heads, "pairs").mockResolvedValue({ pairs: [ompLocal], default_pair: ompLocal });
    vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-9" } as Task);
    const dispatchSpy = vi.spyOn(api.tasks, "dispatchDeferred").mockResolvedValue(undefined as never);
    vi.spyOn(api.heads, "start").mockRejectedValue(new Error(`API 409: ${JSON.stringify({ detail: { code: "engine_not_ready" } })}`));
    const onOpen = renderModal();
    await openAndFill();

    await userEvent.click(await screen.findByTestId("run-as-head"));
    expect(await screen.findByTestId("head-start-kept")).toHaveTextContent("stays on hold in Inbox");
    const secondary = screen.getByTestId("create-task-only");
    expect(secondary).toHaveTextContent("Keep task, close");
    expect(secondary).not.toHaveTextContent("Retry uploads");
    expect(screen.getByTestId("run-as-head")).toHaveTextContent("Start head again");
    // Cmd/Ctrl+Enter (the "create" shortcut) must not dispatch either
    await userEvent.keyboard("{Meta>}{Enter}{/Meta}");
    await userEvent.click(secondary);
    expect(dispatchSpy).not.toHaveBeenCalled();
    expect(onOpen).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByTestId("head-section")).not.toBeInTheDocument()); // closed
  });

  it("with a staged file, a failed head start still never calls dispatchDeferred", async () => {
    vi.spyOn(api.heads, "pairs").mockResolvedValue({ pairs: [ompLocal], default_pair: ompLocal });
    vi.spyOn(api.tasks, "create").mockResolvedValue({ id: "task-9" } as Task);
    vi.spyOn(api.references, "upload").mockResolvedValue({} as never);
    const dispatchSpy = vi.spyOn(api.tasks, "dispatchDeferred").mockResolvedValue(undefined as never);
    const startSpy = vi.spyOn(api.heads, "start")
      .mockRejectedValueOnce(new Error(`API 503: ${JSON.stringify({ detail: { code: "spool_unavailable" } })}`))
      .mockRejectedValueOnce(new Error(`API 503: ${JSON.stringify({ detail: { code: "spool_unavailable" } })}`));
    renderModal();
    await openAndFill();
    const input = document.querySelector<HTMLInputElement>('input[type="file"]');
    expect(input).not.toBeNull();
    await userEvent.upload(input!, new File(["x"], "notes.txt", { type: "text/plain" }));

    await userEvent.click(await screen.findByTestId("run-as-head"));
    await screen.findByTestId("head-start-error");
    // Cmd/Ctrl+Enter is the plain "create" shortcut — after a head start it
    // must retry the head start, never dispatch the staged-file card
    await userEvent.keyboard("{Meta>}{Enter}{/Meta}");
    await waitFor(() => expect(startSpy).toHaveBeenCalledTimes(2));
    expect(dispatchSpy).not.toHaveBeenCalled();
  });

  it("heads switched off (404 heads_disabled) → no head section, plain 'Create task'", async () => {
    vi.spyOn(api.heads, "pairs").mockRejectedValue(new Error(`API 404: ${JSON.stringify({ detail: { code: "heads_disabled" } })}`));
    renderModal();
    await openAndFill();
    await waitFor(() => expect(api.heads.pairs).toHaveBeenCalled());
    expect(screen.queryByTestId("head-section")).not.toBeInTheDocument();
    expect(screen.queryByTestId("run-as-head")).not.toBeInTheDocument();
    expect(screen.getByTestId("create-task-submit")).toHaveTextContent("Create task");
  });

  it("phone footer: one column, the main action full width, keyboard hint only on hover devices", async () => {
    vi.spyOn(api.heads, "pairs").mockResolvedValue({ pairs: [ompLocal], default_pair: ompLocal });
    renderModal();
    await openAndFill();
    await screen.findByTestId("head-pair-trigger");

    const footer = screen.getByTestId("create-task-footer");
    expect(footer.className).toMatch(/(^|\s)flex-col(-reverse)?(\s|$)/);
    expect(footer.className).toContain("sm:flex-row");
    expect(screen.getByTestId("run-as-head").className).toContain("w-full");
    expect(screen.getByTestId("run-as-head").className).toContain("min-h-[44px]");
    expect(screen.getByTestId("create-task-shortcut-hint").className).toContain("hidden");
    expect(screen.getByTestId("create-task-shortcut-hint").className).toContain("[@media(hover:hover)]:inline");
  });

  it("the modal source uses no gradient and no hard-coded English footer", () => {
    const src = readFileSync(path.resolve(__dirname, "../CreateTaskModal.tsx"), "utf8");
    expect(src).not.toMatch(/linear-gradient/);
    expect(src).not.toMatch(/>\s*Cancel\s*</);
    expect(src).not.toMatch(/Cmd\+Enter = create/);
  });
});
