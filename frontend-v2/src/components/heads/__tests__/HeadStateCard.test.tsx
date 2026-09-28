/**
 * Head state card, Restart with …, runs list (docs/specs/head-launcher.md
 * §8.2, build plan B5–B7).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { headRunsActive, pairKey, pairsForRestart, type HeadRun } from "@/lib/heads";
import { deriveStateCard } from "@/lib/taskDetail/stateCard";
import { taskFixture } from "@/lib/taskDetail/__tests__/fixtures";
import { mkPair, mkRun } from "@/lib/__tests__/headFixtures";
import { HeadStateCard } from "../HeadStateCard";
import { HeadRunsList } from "../HeadRunsList";

const ompLocal = mkPair();
const claudeLocal = mkPair({ harness: "claude", harness_label: "Claude Code", status: "experimental" });

function renderCard(run: HeadRun) {
  const card = deriveStateCard({ task: taskFixture({ status: "in_progress" }), approvals: [], comments: [], runRecord: null, headRun: run });
  if (card?.kind !== "head") throw new Error("expected a head card");
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <HeadStateCard run={card.run} mainAction={card.mainAction} silentWarn={card.silentWarn} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api.heads, "pairs").mockResolvedValue({ pairs: [ompLocal, claudeLocal], default_pair: ompLocal });
});

describe("HeadStateCard — one main action per state (B5)", () => {
  it("running → Stop on top + step; state word and pair are not repeated here; sign of life under Details", async () => {
    const stop = vi.spyOn(api.heads, "stop").mockResolvedValue({ run_id: "r", state: "stopping" });
    renderCard(mkRun({ state: "running", silent_s: 40, step: "4/7 sabotage probe" }));

    // The state sentence above the card carries "Running · for …" and the
    // properties carry the pair (DESIGN.md K3/K10) — no kicker here.
    expect(screen.queryByTestId("head-card-kicker")).not.toBeInTheDocument();
    expect(screen.getByTestId("task-state-card")).not.toHaveTextContent("omp");
    expect(screen.getByText("Step: 4/7 sabotage probe")).toBeInTheDocument();
    expect(screen.queryByTestId("head-card-sign")).not.toBeInTheDocument();
    expect(screen.queryByTestId("head-details")).not.toBeInTheDocument();
    expect(screen.queryByTestId("head-main-restart")).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("head-details-toggle"));
    expect(screen.getByTestId("head-card-sign")).toHaveTextContent("Last output 40 s ago");
    await userEvent.click(screen.getByTestId("head-details-toggle"));

    // Stop asks first (review: no 30-minute run lost to a mistap)
    await userEvent.click(screen.getByTestId("head-main-stop"));
    expect(stop).not.toHaveBeenCalled();
    expect(screen.getByTestId("head-main-stop-confirm")).toHaveTextContent("Stop the head? The branch stays.");
    await userEvent.click(screen.getByTestId("head-main-stop-confirm-no"));
    expect(stop).not.toHaveBeenCalled();
    await userEvent.click(screen.getByTestId("head-main-stop"));
    await userEvent.click(screen.getByTestId("head-main-stop-confirm-yes"));
    await waitFor(() => expect(stop).toHaveBeenCalledWith("11111111-1111-4111-8111-111111111111"));
  });

  it("silent > 15 min → warn tone, 'Silent for …', still running with Stop", async () => {
    renderCard(mkRun({ state: "running", silent_s: 22 * 60 }));
    const card = screen.getByTestId("task-state-card");
    expect(card).toHaveAttribute("data-tone", "warn");
    expect(card).toHaveAttribute("data-head-state", "running");
    await userEvent.click(screen.getByTestId("head-details-toggle"));
    expect(screen.getByTestId("head-card-sign")).toHaveTextContent("Silent for 22 min — still running");
    expect(screen.getByTestId("head-main-stop")).toBeInTheDocument();
  });

  it("needs you → question + answer field; Answer & continue restarts the same pair on the same branch", async () => {
    const restart = vi.spyOn(api.heads, "restart").mockResolvedValue({ run_id: "r2", state: "starting", restarted_from: "r" });
    renderCard(mkRun({ state: "needs_you", question: "Remove the old endpoint? Two callers still use it." }));

    expect(screen.getByTestId("head-card-question")).toHaveTextContent("Two callers still use it.");
    const answerBtn = screen.getByTestId("head-main-answer");
    expect(answerBtn).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Your answer"), "Deprecate now, remove later");
    await userEvent.click(answerBtn);
    await waitFor(() =>
      expect(restart).toHaveBeenCalledWith("11111111-1111-4111-8111-111111111111", {
        harness: "omp", runtime_slug: "glm-local", mode: "continue", answer: "Deprecate now, remove later",
      }),
    );
  });

  it("passed → Open PR #712 link, no Stop", () => {
    renderCard(mkRun({ state: "passed", pr_url: "https://github.com/acme/tool/pull/712", exited_at: "2026-09-23T10:34:05Z" }));
    const link = screen.getByTestId("head-main-open-pr");
    expect(link).toHaveAttribute("href", "https://github.com/acme/tool/pull/712");
    expect(link).toHaveTextContent("Open PR #712");
    // The PR number shows once — on the button (DESIGN.md K3).
    expect(screen.getByText("The pull request is open — your review decides the merge.")).toBeInTheDocument();
    expect(screen.getAllByText(/#712/)).toHaveLength(1);
    expect(screen.queryByTestId("head-main-stop")).not.toBeInTheDocument();
  });

  it("passed on a scratch repo with a local origin → the sentence + the branch (copyable) instead of Open PR", async () => {
    renderCard(mkRun({ state: "passed", reason: "scratch_branch_pushed", pr_url: null, exited_at: "2026-09-23T10:34:05Z" }));
    // No badge next to the sentence any more — it said the same thing twice.
    expect(screen.queryByTestId("head-main-branch-pushed")).not.toBeInTheDocument();
    const result = screen.getByTestId("head-card-scratch-branch");
    expect(result).toHaveTextContent("no PR is possible there");
    expect(result).toHaveTextContent("mc-head/fix-flaky-1111");
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await userEvent.click(screen.getByRole("button", { name: "Copy branch name" }));
    expect(writeText).toHaveBeenCalledWith("mc-head/fix-flaky-1111");
    expect(screen.queryByTestId("head-main-open-pr")).not.toBeInTheDocument();
    expect(screen.queryByTestId("head-main-restart")).not.toBeInTheDocument();
    expect(screen.queryByText(/your review decides the merge/)).not.toBeInTheDocument();
  });

  it.each([
    ["failed", "time_limit", "Time limit reached. The branch is kept."],
    ["failed", "no_progress", "No progress for too long (no output, no step, no file change) — stopped. The branch is kept."],
    ["failed", "hard_limit", "Emergency brake: the hard time limit was reached. The branch is kept."],
    ["failed", "exit_2", "The harness ended with exit code 2."],
    ["stopped", "stopped", "Stopped by you. The branch is kept."],
  ] as const)("%s (%s) → reason sentence + Restart with …", (state, reason, sentence) => {
    renderCard(mkRun({ state, reason, exited_at: "2026-09-23T12:00:05Z" }));
    expect(screen.getByTestId("head-card-reason")).toHaveTextContent(sentence);
    expect(screen.getByTestId("head-card-reason")).toHaveTextContent("mc-head/fix-flaky-1111");
    expect(screen.getByTestId("head-main-restart")).toBeInTheDocument();
  });

  it("details: log on demand; tmux attach is desktop-only", async () => {
    vi.spyOn(api.heads, "log").mockResolvedValue("step 1\nstep 2");
    renderCard(mkRun({ state: "running", tmux: "mc-head-1111" }));
    await userEvent.click(screen.getByTestId("head-details-toggle"));
    const details = screen.getByTestId("head-details");
    expect(within(details).getByTestId("head-copy-tmux").className).toMatch(/(^|\s)hidden(\s|$)/);
    expect(within(details).getByTestId("head-copy-tmux").className).toContain("md:inline-flex");
    await userEvent.click(within(details).getByRole("button", { name: "Open log" }));
    expect(await screen.findByTestId("head-log")).toHaveTextContent("step 2");
  });
});

describe("Restart with … (B6)", () => {
  it("sends the chosen pair and mode", async () => {
    const restart = vi.spyOn(api.heads, "restart").mockResolvedValue({ run_id: "r2", state: "starting", restarted_from: "r" });
    renderCard(mkRun({ state: "failed", reason: "time_limit", exited_at: "2026-09-23T12:00:05Z" }));

    await userEvent.click(screen.getByTestId("head-main-restart"));
    const dialog = await screen.findByTestId("head-restart-dialog");
    const trigger = await within(dialog).findByTestId("head-pair-trigger");
    expect(trigger).toHaveTextContent("omp · GLM local");
    await userEvent.click(trigger);
    await userEvent.click(within(dialog).getByTestId(`head-pair-option-${pairKey(claudeLocal)}`));
    await userEvent.click(within(dialog).getByTestId("head-restart-mode-fresh"));
    await userEvent.click(within(dialog).getByTestId("head-restart-submit"));

    await waitFor(() =>
      expect(restart).toHaveBeenCalledWith("11111111-1111-4111-8111-111111111111", {
        harness: "claude", runtime_slug: "glm-local", mode: "fresh",
      }),
    );
  });

  it("the submit button is disabled while the restart is pending", async () => {
    let resolve!: (v: { run_id: string; state: string; restarted_from: string }) => void;
    vi.spyOn(api.heads, "restart").mockReturnValue(new Promise((r) => { resolve = r; }));
    renderCard(mkRun({ state: "stopped", reason: "stopped", exited_at: "2026-09-23T12:00:05Z" }));

    await userEvent.click(screen.getByTestId("head-main-restart"));
    const dialog = await screen.findByTestId("head-restart-dialog");
    await within(dialog).findByTestId("head-pair-trigger");
    const submit = within(dialog).getByTestId("head-restart-submit");
    expect(submit).toBeEnabled();
    await userEvent.click(submit);
    await waitFor(() => expect(submit).toBeDisabled());
    expect(submit).toHaveTextContent("Restarting…");
    resolve({ run_id: "r2", state: "starting", restarted_from: "r" });
  });

  it.each(["gh_identity_missing", "sandbox_required", "not_picked_up", "prepare_failed", "box_busy"])(
    "a run that ended before its branch existed (%s) restarts fresh; continue is off",
    async (reason) => {
      const restart = vi.spyOn(api.heads, "restart").mockResolvedValue({ run_id: "r2", state: "starting", restarted_from: "r" });
      renderCard(mkRun({ state: "failed", reason, exited_at: "2026-09-23T10:00:06Z" }));
      await userEvent.click(screen.getByTestId("head-main-restart"));
      const dialog = await screen.findByTestId("head-restart-dialog");
      await within(dialog).findByTestId("head-pair-trigger");
      expect(within(dialog).getByTestId("head-restart-mode-fresh")).toBeChecked();
      expect(within(dialog).getByTestId("head-restart-mode-continue")).toBeDisabled();
      expect(dialog).toHaveTextContent("No work on this branch yet");
      await userEvent.click(within(dialog).getByTestId("head-restart-submit"));
      await waitFor(() => expect(restart).toHaveBeenCalledWith("11111111-1111-4111-8111-111111111111", expect.objectContaining({ mode: "fresh" })));
    },
  );

  it("a run with work on its branch still defaults to continue", async () => {
    renderCard(mkRun({ state: "failed", reason: "time_limit", exited_at: "2026-09-23T12:00:00Z" }));
    await userEvent.click(screen.getByTestId("head-main-restart"));
    const dialog = await screen.findByTestId("head-restart-dialog");
    expect(within(dialog).getByTestId("head-restart-mode-continue")).toBeChecked();
    expect(within(dialog).getByTestId("head-restart-mode-continue")).toBeEnabled();
  });

  it("the run's own busy box counts as startable for its restart, other busy boxes not", () => {
    const own = mkPair({ status: "blocked", reason_code: "box_busy", busy_by: { run_id: "run-A", task_id: "t", title: "x", harness: "omp", runtime_slug: "glm-local", since: null } });
    const foreign = { ...own, runtime_slug: "qwen", busy_by: { ...own.busy_by!, run_id: "run-B" } };
    const [a, b] = pairsForRestart([own, foreign], "run-A");
    expect(a.startable).toBe(true);
    expect(b.startable).toBe(false);
  });
});

describe("error codes", () => {
  it("task_move_failed has its own sentence (EN)", async () => {
    const { headErrorKey } = await import("@/lib/heads");
    expect(headErrorKey(new Error('API 409: {"detail":{"code":"task_move_failed"}}'))).toBe("errors.task_move_failed");
  });
});

describe("Runs list + polling (B7)", () => {
  it("lists runs oldest first with pair, state and detail — the crosswise switch is visible", async () => {
    const qc = new QueryClient();
    const runs = [
      mkRun({ run_id: "b", harness: "claude", state: "passed", pr_url: "https://github.com/o/r/pull/712", created_at: "2026-09-23T12:00:00Z", started_at: "2026-09-23T12:00:00Z", exited_at: "2026-09-23T12:16:00Z" }),
      mkRun({ run_id: "a", state: "failed", reason: "time_limit", created_at: "2026-09-23T10:00:00Z", started_at: "2026-09-23T10:00:00Z", exited_at: "2026-09-23T12:00:00Z" }),
    ];
    render(<QueryClientProvider client={qc}><HeadRunsList runs={runs} pairs={[ompLocal, claudeLocal]} /></QueryClientProvider>);
    const rows = screen.getAllByTestId("head-run-row");
    expect(rows[0]).toHaveTextContent("omp · GLM local");
    expect(rows[0]).toHaveTextContent("Failed · Time limit reached.");
    expect(rows[1]).toHaveTextContent("Claude Code · GLM local");
    expect(rows[1]).toHaveTextContent("Passed · PR #712");
  });

  it("a scratch run that pushed its branch shows that instead of a PR number", () => {
    const qc = new QueryClient();
    const runs = [mkRun({ state: "passed", reason: "scratch_branch_pushed", pr_url: null, exited_at: "2026-09-23T10:34:05Z" })];
    render(<QueryClientProvider client={qc}><HeadRunsList runs={runs} pairs={[ompLocal]} /></QueryClientProvider>);
    expect(screen.getByTestId("head-run-row")).toHaveTextContent("Passed · Branch pushed · scratch repo (no PR)");
  });

  it("polling runs only while the newest run is active and stops at a final state", () => {
    const older = mkRun({ run_id: "a", state: "failed", created_at: "2026-09-23T10:00:00Z" });
    expect(headRunsActive({ runs: [older, mkRun({ run_id: "b", state: "running", created_at: "2026-09-23T11:00:00Z" })] })).toBe(true);
    expect(headRunsActive({ runs: [older, mkRun({ run_id: "b", state: "starting", created_at: "2026-09-23T11:00:00Z" })] })).toBe(true);
    expect(headRunsActive({ runs: [mkRun({ run_id: "b", state: "passed", created_at: "2026-09-23T11:00:00Z" }), mkRun({ run_id: "a", state: "running", created_at: "2026-09-23T10:00:00Z" })] })).toBe(false);
    expect(headRunsActive({ runs: [] })).toBe(false);
    expect(headRunsActive(undefined)).toBe(false);
  });
});
