/**
 * HeadChatFooter — the footer the head chat shows INSTEAD of a composer
 * (bauplan `heads-sichtbar` PR 2 §3.2): step+Stop while running, question+
 * answer while it needs the operator, Continue once passed, reason+Restart
 * once failed/stopped.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { mkRun } from "@/lib/__tests__/headFixtures";
import { HeadChatFooter } from "../HeadChatFooter";

function renderFooter(
  run: Parameters<typeof mkRun>[0],
  footerProps: Partial<Omit<Parameters<typeof HeadChatFooter>[0], "run">> = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <HeadChatFooter run={mkRun(run)} {...footerProps} />
    </QueryClientProvider>,
  );
}

beforeEach(() => vi.restoreAllMocks());

describe("HeadChatFooter", () => {
  it("running: shows the translated step and a Stop button, no composer", async () => {
    renderFooter({ state: "running", step: "5/7 independent review · waiting for: reviewer" });
    expect(screen.getByTestId("head-footer-step")).toHaveTextContent("Step 5/7 · Independent review");
    expect(screen.getByTestId("head-footer-stop")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("running with an unparseable step shows no step line, still shows Stop", () => {
    renderFooter({ state: "running", step: null });
    expect(screen.queryByTestId("head-footer-step")).not.toBeInTheDocument();
    expect(screen.getByTestId("head-footer-stop")).toBeInTheDocument();
  });

  it("needs_you: answer field + Answer & continue calls restart with mode=continue and the typed answer", async () => {
    const restart = vi.spyOn(api.heads, "restart").mockResolvedValue({ run_id: "r2", state: "starting", restarted_from: "r1" });
    renderFooter({ run_id: "r1", state: "needs_you", harness: "omp", runtime_slug: "glm-local" });
    const field = screen.getByTestId("head-footer-answer");
    await userEvent.type(field, "Yes, deprecate it.");
    await userEvent.click(screen.getByTestId("head-footer-answer-send"));
    await waitFor(() =>
      expect(restart).toHaveBeenCalledWith("r1", { harness: "omp", runtime_slug: "glm-local", mode: "continue", answer: "Yes, deprecate it." }),
    );
  });

  // Review finding on PR #756: "braucht dich" showed only the answer field
  // — the operator could not see WHAT was being asked without leaving the
  // footer to scroll the transcript above it.
  it("needs_you: never repeats the bare state word — the header above already says it (K3/K10)", () => {
    // Review finding on PR #756 round 3: the footer used to carry its own
    // "Needs you" label on top of the question, duplicating the SAME state
    // word `HeadChatHeader`'s context line already shows ("{pair} · Needs
    // you") right above this footer on the same screen.
    renderFooter({ state: "needs_you", question: "Deprecate the old field or keep it for one more release?" });
    expect(screen.queryByText("Needs you")).not.toBeInTheDocument();
  });

  it("needs_you: shows the question text above the answer field", () => {
    renderFooter({ state: "needs_you", question: "Deprecate the old field or keep it for one more release?" });
    expect(screen.getByTestId("head-footer-question")).toHaveTextContent(
      "Deprecate the old field or keep it for one more release?",
    );
    expect(screen.getByTestId("head-footer-answer")).toBeInTheDocument();
  });

  it("needs_you: falls back to the transcript's last assistant message when question.md is empty", () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
        <HeadChatFooter
          run={mkRun({ state: "needs_you", question: null })}
          transcriptFallbackQuestion="Should the retry loop cap at 3 attempts or 5?"
        />
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("head-footer-question")).toHaveTextContent(
      "Should the retry loop cap at 3 attempts or 5?",
    );
  });

  it("needs_you: shows a 'no question recorded' notice when there is neither a question nor a transcript fallback", () => {
    renderFooter({ state: "needs_you", question: null });
    expect(screen.getByTestId("head-footer-question")).toHaveTextContent(
      "The head asked a question, but it is empty.",
    );
  });

  it("needs_you: the send button is disabled until something is typed", () => {
    renderFooter({ state: "needs_you" });
    expect(screen.getByTestId("head-footer-answer-send")).toBeDisabled();
  });

  // Review finding on PR #756 round 4: an answered needs_you kept offering
  // "Answer & continue" forever, inviting a second restart from an
  // already-stale question.
  it("needs_you superseded by a newer run: shows a link to it instead of the answer field", () => {
    renderFooter({ state: "needs_you", question: "Deprecate the old field or keep it?" }, { newerRunId: "r2" });
    expect(screen.queryByTestId("head-footer-answer")).not.toBeInTheDocument();
    expect(screen.queryByTestId("head-footer-answer-send")).not.toBeInTheDocument();
    expect(screen.queryByText("Deprecate the old field or keep it?")).not.toBeInTheDocument();
    expect(screen.getByTestId("head-footer-superseded")).toHaveTextContent("A newer run already answered this.");
    expect(screen.getByTestId("head-footer-open-newer")).toBeInTheDocument();
  });

  it("needs_you superseded by a newer run: the link calls onOpenNewerRun with that run's id", async () => {
    const onOpenNewerRun = vi.fn();
    renderFooter({ state: "needs_you" }, { newerRunId: "r2", onOpenNewerRun });
    await userEvent.click(screen.getByTestId("head-footer-open-newer"));
    expect(onOpenNewerRun).toHaveBeenCalledWith("r2");
  });

  it("needs_you with no newerRunId still shows the normal answer field", () => {
    renderFooter({ state: "needs_you", question: "Deprecate the old field or keep it?" });
    expect(screen.queryByTestId("head-footer-superseded")).not.toBeInTheDocument();
    expect(screen.getByTestId("head-footer-answer")).toBeInTheDocument();
  });

  it("passed: shows exactly one Continue action, no Stop, no answer field", () => {
    renderFooter({ state: "passed" });
    expect(screen.getByTestId("head-footer-continue")).toHaveTextContent("Continue");
    expect(screen.queryByTestId("head-footer-stop")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("failed: shows the reason sentence and a Restart action", () => {
    renderFooter({ state: "failed", reason: "no_pr" });
    expect(screen.getByTestId("head-footer-reason")).toHaveTextContent("Ended without a pull request.");
    expect(screen.getByTestId("head-footer-restart")).toBeInTheDocument();
  });

  it("stopped: shows the reason sentence and a Restart action", () => {
    renderFooter({ state: "stopped", reason: "stopped" });
    expect(screen.getByTestId("head-footer-reason")).toHaveTextContent("Stopped by you.");
    expect(screen.getByTestId("head-footer-restart")).toBeInTheDocument();
  });

  // DESIGN.md K3 ("…" only on the title) — review finding on PR #756
  // round 4: a long reason (e.g. local_network_blocked, which carries the
  // operator's own fix steps) truncated with "…", the only explanation of
  // why the run failed.
  it("failed: the reason wraps instead of truncating, never cut off", () => {
    renderFooter({ state: "failed", reason: "local_network_blocked" });
    const reason = screen.getByTestId("head-footer-reason");
    expect(reason.className).toMatch(/break-words/);
    expect(reason.className).not.toMatch(/\btruncate\b/);
    expect(reason).toHaveTextContent("System Settings");
  });

  it("running: the step line wraps instead of truncating", () => {
    renderFooter({ state: "running", step: "5/7 independent review · waiting for: reviewer" });
    const step = screen.getByTestId("head-footer-step");
    expect(step.className).toMatch(/break-words/);
    expect(step.className).not.toMatch(/\btruncate\b/);
  });
});
