/**
 * Question cards (chat package P3). The schemas are the four shapes omp
 * 18.10.1 sends over ACP `elicitation/create`, as the chat daemon passes them
 * on (docker/omp-bridge/acp_chat.clean_schema).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import type { ElicitationEvent, ElicitationResultEvent, ElicitationSchema } from "@/lib/chatTypes";
import { ElicitationCard, summarize } from "./ElicitationCard";

vi.mock("@/lib/api", () => ({
  api: { chat: { answerElicitation: vi.fn() } },
}));
vi.mock("@/lib/notify", () => ({
  notify: { error: vi.fn(), success: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));

const mockAnswer = vi.mocked(api.chat.answerElicitation);

function card(schema: ElicitationSchema, message = "Which database?"): ElicitationEvent {
  return {
    kind: "elicitation",
    uuid: "acp00010",
    ts: "2026-10-13T10:00:00Z",
    elicitationId: "el-0123456789ab",
    message,
    schema,
  };
}

const SELECT: ElicitationSchema = {
  type: "object",
  properties: { value: { type: "string", enum: ["Postgres", "SQLite"] } },
  required: ["value"],
};
const CONFIRM: ElicitationSchema = {
  type: "object",
  properties: { value: { type: "boolean" } },
  required: ["value"],
};
const INPUT: ElicitationSchema = {
  type: "object",
  properties: { value: { type: "string", description: "branch name" } },
  required: ["value"],
};
const ASK: ElicitationSchema = {
  type: "object",
  properties: {
    q0: {
      type: "string", title: "Which framework?",
      oneOf: [{ const: "React", title: "React" }, { const: "Vue", title: "Vue" }],
      default: "React",
    },
    q0__other: { type: "string", title: "Other (type your own)" },
    q1: {
      type: "array", title: "Which checks?",
      items: { anyOf: [{ const: "lint", title: "lint" }, { const: "test", title: "test" }] },
    },
    q1__other: { type: "string", title: "Other (type your own)" },
  },
};

function result(status: ElicitationResultEvent["status"], content?: Record<string, unknown>): ElicitationResultEvent {
  return {
    kind: "elicitation_result", uuid: "acp00011", ts: "2026-10-13T10:01:00Z",
    elicitationId: "el-0123456789ab", status, content: content ?? null,
  };
}

beforeEach(() => {
  mockAnswer.mockReset();
  mockAnswer.mockResolvedValue({ status: "answered" });
  vi.mocked(notify.info).mockReset();
  vi.mocked(notify.error).mockReset();
});

describe("ElicitationCard", () => {
  it("a single choice answers with one tap", async () => {
    render(<ElicitationCard ev={card(SELECT)} result={null} agentId="a1" />);
    expect(screen.getByTestId("elicitation-card")).toHaveAttribute("data-status", "open");
    fireEvent.click(screen.getByRole("button", { name: "SQLite" }));
    await waitFor(() =>
      expect(mockAnswer).toHaveBeenCalledWith("a1", "el-0123456789ab", "accept", { value: "SQLite" }),
    );
    expect(await screen.findByTestId("elicitation-outcome")).toHaveTextContent("Answered: SQLite");
    expect(screen.queryByTestId("elicitation-send")).not.toBeInTheDocument();
  });

  it("yes/no sends a boolean", async () => {
    render(<ElicitationCard ev={card(CONFIRM, "Delete the branch?")} result={null} agentId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: "No" }));
    await waitFor(() =>
      expect(mockAnswer).toHaveBeenCalledWith("a1", "el-0123456789ab", "accept", { value: false }),
    );
  });

  it("free text needs the send button, empty text cannot be sent", async () => {
    render(<ElicitationCard ev={card(INPUT, "Branch?")} result={null} agentId="a1" />);
    const send = screen.getByTestId("elicitation-send");
    expect(send).toBeDisabled();
    fireEvent.change(screen.getByTestId("elicitation-text"), { target: { value: "  feat/x  " } });
    expect(send).toBeEnabled();
    fireEvent.click(send);
    await waitFor(() =>
      expect(mockAnswer).toHaveBeenCalledWith("a1", "el-0123456789ab", "accept", { value: "feat/x" }),
    );
  });

  it("ask tool: recommendation marked, multi-select and other text in one answer", async () => {
    render(<ElicitationCard ev={card(ASK, "Answer 2 questions")} result={null} agentId="a1" />);
    const recommended = screen.getAllByTestId("elicitation-choice").find((b) => b.dataset.recommended);
    expect(recommended).toHaveTextContent("React");
    expect(recommended).toHaveTextContent("Recommended");

    fireEvent.click(screen.getByRole("button", { name: "Vue" }));
    fireEvent.click(screen.getByRole("button", { name: "lint" }));
    fireEvent.click(screen.getByRole("button", { name: "test" }));
    fireEvent.click(screen.getByRole("button", { name: "test" })); // toggles off again
    const others = screen.getAllByTestId("elicitation-other");
    fireEvent.click(others[1]);
    fireEvent.change(screen.getByTestId("elicitation-other-text"), { target: { value: "e2e" } });
    expect(mockAnswer).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("elicitation-send"));
    await waitFor(() =>
      expect(mockAnswer).toHaveBeenCalledWith("a1", "el-0123456789ab", "accept", {
        q0: "Vue", q1: ["lint"], q1__other: "e2e",
      }),
    );
  });

  it("skip sends cancel without content", async () => {
    mockAnswer.mockResolvedValue({ status: "cancelled" });
    render(<ElicitationCard ev={card(SELECT)} result={null} agentId="a1" />);
    fireEvent.click(screen.getByTestId("elicitation-skip"));
    await waitFor(() =>
      expect(mockAnswer).toHaveBeenCalledWith("a1", "el-0123456789ab", "cancel", undefined),
    );
    expect(await screen.findByTestId("elicitation-outcome")).toHaveTextContent("Skipped");
  });

  it("a card that is no longer open says so instead of failing", async () => {
    mockAnswer.mockRejectedValue(new Error('API 409: {"reason":"elicitation_not_pending"}'));
    render(<ElicitationCard ev={card(SELECT)} result={null} agentId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: "Postgres" }));
    expect(await screen.findByTestId("elicitation-outcome")).toHaveTextContent("No longer open");
    expect(screen.getByTestId("elicitation-card")).toHaveAttribute("data-status", "expired");
    expect(notify.info).toHaveBeenCalled();
    expect(notify.error).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "SQLite" })).not.toBeInTheDocument();
  });

  it("the transcript result settles the card, also after a reload", () => {
    const { rerender } = render(
      <ElicitationCard ev={card(SELECT)} result={result("answered", { value: "SQLite" })} agentId="a1" />,
    );
    expect(screen.getByTestId("elicitation-outcome")).toHaveTextContent("Answered: SQLite");
    expect(screen.queryByTestId("elicitation-choice")).not.toBeInTheDocument();
    rerender(<ElicitationCard ev={card(SELECT)} result={result("expired")} agentId="a1" />);
    expect(screen.getByTestId("elicitation-outcome")).toHaveTextContent("No longer open");
  });

  it("read-only (head transcript): no buttons, says it waits", () => {
    render(<ElicitationCard ev={card(SELECT)} result={null} agentId={null} />);
    expect(screen.queryByTestId("elicitation-choice")).not.toBeInTheDocument();
    expect(screen.getByTestId("elicitation-outcome")).toHaveTextContent("Waiting for an answer");
  });

  it("renders the plan as markdown in one column with its show-more toggle", () => {
    const plan = 'Approve plan "Login" and start implementation?\n\n# Login form\n\n1. Add `/login` route\n2. Session cookie';
    render(<ElicitationCard ev={card(SELECT, plan)} result={null} agentId="a1" />);
    const message = screen.getByTestId("elicitation-message");
    expect(message.querySelector("h1")).toHaveTextContent("Login form");
    expect(message.querySelectorAll("ol li")).toHaveLength(2);
    expect(message.querySelector("code")).toHaveTextContent("/login");
    // The toggle ClampedContent renders as a sibling must land UNDER the
    // text, not as a second column of the icon row (live, 390 px).
    const column = message.parentElement!;
    expect(column.className).toContain("flex-1");
    expect(column.parentElement!.className).toContain("flex");
  });

  it("summarize words booleans and lists", () => {
    expect(summarize({ value: true }, "Yes", "No")).toBe("Yes");
    expect(summarize({ q0: "Vue", q1: ["lint", "test"], q1__other: "e2e" }, "Yes", "No"))
      .toBe("Vue · lint, test · e2e");
    expect(summarize(null, "Yes", "No")).toBe("");
  });
});
