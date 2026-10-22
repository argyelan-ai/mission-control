/**
 * StatusLine with a task section in the chat (spec A §6.1, S5, test 12).
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { StatusLine } from "./StatusLine";
import type { StateEvent } from "@/lib/chatTypes";

const state = (status: StateEvent["status"]): StateEvent => ({ kind: "state", status, prompt: null });
const task = { taskId: "00000000-0000-4000-8000-000000000001", title: "Build the report page" };

describe("StatusLine — task in the chat", () => {
  it("task_working with the task head on screen says nothing (the head says it, K3)", () => {
    const { container } = render(<StatusLine state={state("task_working")} connected task={task} taskHeadVisible />);
    expect(container).toBeEmptyDOMElement();
  });

  it("task_working with no task head on screen: a quiet line that links to the task", () => {
    render(<StatusLine state={state("task_working")} connected task={task} taskHeadVisible={false} />);
    expect(screen.getByTestId("status-label")).toHaveTextContent("Task in progress · Build the report page");
    expect(screen.getByRole("link", { name: /Build the report page/ })).toHaveAttribute(
      "href",
      `/tasks?task=${task.taskId}`,
    );
  });

  it("waiting_model names the task, as a link", () => {
    render(<StatusLine state={state("waiting_model")} connected task={task} taskHeadVisible />);
    expect(screen.getByTestId("status-label")).toHaveTextContent("Waiting for the model · Build the report page");
    expect(screen.getByRole("link", { name: /Build the report page/ })).toBeInTheDocument();
  });

  it("without a task (other harnesses) nothing changes", () => {
    render(<StatusLine state={state("task_working")} connected />);
    expect(screen.getByTestId("status-label")).toHaveTextContent("Working on a task");
  });
});
