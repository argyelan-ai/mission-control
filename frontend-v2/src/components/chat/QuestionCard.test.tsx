/**
 * The question card for every agent (Welle 3, E11/E12): `mc question`, a
 * blocking `mc ask` or a pending approval of the agent, answered with one tap
 * in the chat — calm (accent frame, no pulse), never hidden.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import type { OperatorQuestion } from "@/lib/chatTypes";
import { touchesQuestions } from "@/hooks/useAgentQuestions";
import { AgentQuestions, QuestionCard, elsewhereHref, primaryOption } from "./QuestionCard";

vi.mock("@/lib/api", () => ({
  api: { chat: { answerQuestion: vi.fn(), questions: vi.fn() } },
  sseUrls: { activity: () => "/sse/activity" },
}));
vi.mock("@/lib/notify", () => ({
  notify: { error: vi.fn(), success: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));

const hook = vi.hoisted(() => ({
  questions: [] as OperatorQuestion[],
  refresh: vi.fn(),
}));
vi.mock("@/hooks/useAgentQuestions", async (orig) => ({
  ...(await orig<typeof import("@/hooks/useAgentQuestions")>()),
  useAgentQuestions: () => ({ questions: hook.questions, loading: false, refresh: hook.refresh }),
}));

const mockAnswer = vi.mocked(api.chat.answerQuestion);

function q(over: Partial<OperatorQuestion> = {}): OperatorQuestion {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    source: "approval",
    kind: "clarification_question",
    answerable: true,
    question: "Ready to commit: add test for parse() (1 file). Commit?",
    options: ["Commit", "Show diff", "Change something"],
    default: null,
    blocking: true,
    task_id: "22222222-2222-4222-8222-222222222222",
    task_title: "Add a test for parse()",
    created_at: new Date().toISOString(),
    ...over,
  };
}

const AGENT = "33333333-3333-4333-8333-333333333333";

beforeEach(() => {
  vi.clearAllMocks();
  hook.questions = [];
  mockAnswer.mockResolvedValue({ status: "answered", task_status: "in_progress" });
});

describe("QuestionCard", () => {
  it("one tap on an option answers it and leaves a receipt", async () => {
    const onSettled = vi.fn();
    render(<QuestionCard question={q()} agentId={AGENT} onSettled={onSettled} />);
    expect(screen.getByText(/Ready to commit/)).toBeInTheDocument();
    expect(screen.getByText(/Add a test for parse\(\)/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Commit" }));

    await waitFor(() => expect(mockAnswer).toHaveBeenCalledWith(AGENT, q().id, "Commit"));
    expect(mockAnswer).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/Answered: Commit/)).toBeInTheDocument();
    expect(screen.getByText(/the task continues/)).toBeInTheDocument();
    expect(onSettled).toHaveBeenCalledWith("Commit");
    // Answered = no buttons left to tap twice.
    expect(screen.queryByTestId("question-option")).toBeNull();
  });

  it("has exactly one primary button: the agent's default, else its first option", () => {
    expect(primaryOption(q())).toBe("Commit");
    expect(primaryOption(q({ default: "Show diff" }))).toBe("Show diff");
    expect(primaryOption(q({ default: "not an option" }))).toBe("Commit");
    expect(primaryOption(q({ options: [] }))).toBeNull();

    render(<QuestionCard question={q({ default: "Show diff" })} agentId={AGENT} />);
    const primaries = screen.getAllByTestId("question-option").filter((b) => b.dataset.primary);
    expect(primaries.map((b) => b.textContent)).toEqual(["Show diff"]);
  });

  it("free text behind 'Other answer…' sends the typed text", async () => {
    render(<QuestionCard question={q()} agentId={AGENT} />);
    expect(screen.queryByTestId("question-text-input")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Other answer…" }));
    fireEvent.change(screen.getByTestId("question-text-input"), { target: { value: "  rename it first  " } });
    fireEvent.click(screen.getByRole("button", { name: "Send answer" }));
    await waitFor(() => expect(mockAnswer).toHaveBeenCalledWith(AGENT, q().id, "rename it first"));
  });

  it("without options the text field is the card; empty text cannot be sent", () => {
    render(<QuestionCard question={q({ options: [] })} agentId={AGENT} />);
    const send = screen.getByRole("button", { name: "Send answer" });
    expect(send).toBeDisabled();
    fireEvent.change(screen.getByTestId("question-text-input"), { target: { value: "Use SQLite" } });
    expect(send).not.toBeDisabled();
  });

  it("a kind the chat does not answer is shown with a link to its own card", () => {
    render(
      <QuestionCard
        question={q({ kind: "install_mcp", answerable: false, options: [], question: "Install MCP server 'search'" })}
        agentId={AGENT}
      />,
    );
    expect(screen.getByText("Install MCP server 'search'")).toBeInTheDocument();
    expect(screen.queryByTestId("question-option")).toBeNull();
    expect(screen.queryByTestId("question-text-input")).toBeNull();
    const link = screen.getByTestId("question-elsewhere");
    expect(link.getAttribute("href")).toBe(`/tasks?task=${q().task_id}`);
    expect(elsewhereHref(q({ task_id: null }))).toBe("/inbox");
  });

  it("is read-only without an agent id (non-admin)", () => {
    render(<QuestionCard question={q()} agentId={null} />);
    expect(screen.queryByTestId("question-option")).toBeNull();
    expect(screen.getByText("Waiting for an answer")).toBeInTheDocument();
  });

  it("answered elsewhere meanwhile: says so and settles without an answer", async () => {
    mockAnswer.mockRejectedValueOnce(new Error('API 409: {"reason":"question_not_open"}'));
    const onSettled = vi.fn();
    render(<QuestionCard question={q()} agentId={AGENT} onSettled={onSettled} />);
    fireEvent.click(screen.getByRole("button", { name: "Commit" }));
    await waitFor(() => expect(notify.info).toHaveBeenCalledWith("This question was already answered."));
    expect(onSettled).toHaveBeenCalledWith(null);
    expect(notify.error).not.toHaveBeenCalled();
  });

  it("a failed send keeps the card open and says so", async () => {
    mockAnswer.mockRejectedValueOnce(new Error("API 502: down"));
    render(<QuestionCard question={q()} agentId={AGENT} />);
    fireEvent.click(screen.getByRole("button", { name: "Commit" }));
    await waitFor(() => expect(notify.error).toHaveBeenCalledWith("Answer not sent — try again."));
    expect(screen.getByRole("button", { name: "Commit" })).not.toBeDisabled();
  });

  it("is calm: accent frame, never ochre, never a pulse (E12)", () => {
    const { container } = render(<QuestionCard question={q()} agentId={AGENT} />);
    const card = screen.getByTestId("question-card");
    expect(card.getAttribute("style")).toContain("--color-border-accent");
    expect(card.getAttribute("style")).not.toContain("warning");
    expect(container.querySelector("[class*='animate-']")).toBeNull();
  });

  it("counts the questions waiting behind it", () => {
    render(<QuestionCard question={q()} agentId={AGENT} moreCount={2} />);
    expect(screen.getByText("2 more questions waiting")).toBeInTheDocument();
  });
});

describe("AgentQuestions", () => {
  it("renders nothing while nothing waits", () => {
    const { container } = render(<AgentQuestions agentId={AGENT} canAnswer />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the first question and keeps the receipt after the list empties", async () => {
    hook.questions = [q(), q({ id: "44444444-4444-4444-8444-444444444444", question: "Second?" })];
    const { rerender } = render(<AgentQuestions agentId={AGENT} canAnswer />);
    expect(screen.getByText(/Ready to commit/)).toBeInTheDocument();
    expect(screen.queryByText("Second?")).toBeNull();
    expect(screen.getByText("1 more question waiting")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Commit" }));
    await waitFor(() => expect(hook.refresh).toHaveBeenCalled());

    // The backend no longer lists it: the receipt stays, nothing to tap.
    hook.questions = [];
    await act(async () => rerender(<AgentQuestions agentId={AGENT} canAnswer />));
    expect(screen.getByText(/Answered: Commit/)).toBeInTheDocument();
    expect(screen.queryByTestId("question-option")).toBeNull();
  });

  it("is read-only when the viewer may not answer", () => {
    hook.questions = [q()];
    render(<AgentQuestions agentId={AGENT} canAnswer={false} />);
    expect(screen.queryByTestId("question-option")).toBeNull();
  });
});

describe("touchesQuestions", () => {
  it("refetches only on question/approval events of this agent", () => {
    expect(touchesQuestions("clarification.created", { agent_id: AGENT }, AGENT)).toBe(true);
    expect(touchesQuestions("clarification.resolved", { agent_id: "other" }, AGENT)).toBe(false);
    expect(touchesQuestions("approval.created", {}, AGENT)).toBe(true);
    expect(touchesQuestions("task.updated", { agent_id: AGENT }, AGENT)).toBe(false);
  });
});
