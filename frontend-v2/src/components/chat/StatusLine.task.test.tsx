/**
 * StatusLine with a task section in the chat (spec A §6.1, S5, test 12).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { StatusLine, WAITING_MODEL_GRACE_MS } from "./StatusLine";
import { C, STATUS_TEXT } from "@/lib/colors";
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
    // It comes and goes with scrolling: not a live region (no announcement per scroll).
    expect(screen.getByTestId("status-label").parentElement).not.toHaveAttribute("aria-live");
  });

  it("waiting_model names the task, as a link", () => {
    render(<StatusLine state={state("waiting_model")} connected task={task} taskHeadVisible />);
    expect(screen.getByTestId("status-label")).toHaveTextContent("Waiting for the model · Build the report page");
    expect(screen.getByTestId("status-label").parentElement).toHaveAttribute("aria-live", "polite");
    expect(screen.getByRole("link", { name: /Build the report page/ })).toBeInTheDocument();
  });

  it("without a task (other harnesses) nothing changes", () => {
    render(<StatusLine state={state("task_working")} connected />);
    expect(screen.getByTestId("status-label")).toHaveTextContent("Working on a task");
  });
});

/*
 * Round-5 t1 NICE-2: every normal start showed ~6 s of ocker "Waiting for the
 * model" (delivered → the model's first turn). That is the normal way of a
 * start, not a warning: neutral tones first, ocker only once the model has
 * kept the task waiting past a grace (`WAITING_MODEL_GRACE_MS`), counted from
 * the delivery.
 */
describe("StatusLine — a normal start is not a warning (round-5 t1 NICE-2)", () => {
  const NOW = Date.UTC(2026, 9, 23, 15, 1, 30);
  const ago = (ms: number) => new Date(NOW - ms).toISOString();
  const tone = () => (screen.getByTestId("status-label").parentElement as HTMLElement).style.color;
  const dot = () => (screen.getByTestId("status-dot") as HTMLElement).style.backgroundColor;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });
  afterEach(() => vi.useRealTimers());

  it("just delivered: the waiting line is neutral, and turns ocker once the grace has passed", () => {
    render(<StatusLine state={state("waiting_model")} connected task={{ ...task, deliveredAt: ago(5_000) }} />);
    expect(screen.getByTestId("status-label")).toHaveTextContent("Waiting for the model · Build the report page");
    expect(tone()).toBe(C.textMuted);
    expect(dot()).toBe(C.textDim);
    act(() => vi.advanceTimersByTime(WAITING_MODEL_GRACE_MS - 5_000 - 1));
    expect(tone()).toBe(C.textMuted);
    act(() => vi.advanceTimersByTime(1));
    expect(tone()).toBe(STATUS_TEXT.warning);
    expect(dot()).toBe(C.warning);
  });

  it("delivered long ago: ocker at once (the model really keeps it waiting)", () => {
    render(<StatusLine state={state("waiting_model")} connected task={{ ...task, deliveredAt: ago(60_000) }} />);
    expect(tone()).toBe(STATUS_TEXT.warning);
  });

  it("without a delivery time (other harnesses): neutral first, ocker after the grace from when the state came", () => {
    render(<StatusLine state={state("waiting_model")} connected />);
    expect(tone()).toBe(C.textMuted);
    act(() => vi.advanceTimersByTime(WAITING_MODEL_GRACE_MS));
    expect(tone()).toBe(STATUS_TEXT.warning);
  });
});
