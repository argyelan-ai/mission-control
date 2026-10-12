import React from "react";
import { describe, it, expect } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { TodoPanel } from "./TodoPanel";
import en from "../../../messages/en.json";
import de from "../../../messages/de.json";
import type { TodoEvent } from "@/lib/chatTypes";

const todo = (statuses: Array<"pending" | "in_progress" | "completed">): TodoEvent => ({
  kind: "todo",
  uuid: "u1",
  ts: "2026-10-12T09:00:00.000Z",
  source: "TodoWrite",
  items: statuses.map((status, i) => ({ content: `Step ${i + 1}`, status })),
});

const wrap = (ui: React.ReactElement) => render(ui);

describe("TodoPanel", () => {
  it("renders nothing without a snapshot or with an empty one", () => {
    const { container, rerender } = wrap(<TodoPanel todo={null} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<TodoPanel todo={todo([])} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("collapsed: shows progress and the current item, not the list", () => {
    wrap(<TodoPanel todo={todo(["completed", "completed", "in_progress", "pending", "pending", "pending", "pending"])} />);
    expect(screen.getByTestId("todo-progress")).toHaveTextContent("2/7 done");
    expect(screen.getByTestId("todo-current")).toHaveTextContent("Step 3");
    expect(screen.queryByRole("list")).toBeNull();
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "false");
  });

  it("expands to the shared checklist and collapses again", () => {
    wrap(<TodoPanel todo={todo(["completed", "in_progress", "pending"])} />);
    fireEvent.click(screen.getByRole("button", { name: /show todo list/i }));
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    expect(within(screen.getByRole("list")).getByText("Step 2").closest("li")).toHaveAttribute("aria-current", "step");
    fireEvent.click(screen.getByRole("button", { name: /hide todo list/i }));
    expect(screen.queryByRole("list")).toBeNull();
  });

  it("says all done when nothing is open", () => {
    wrap(<TodoPanel todo={todo(["completed", "completed"])} />);
    expect(screen.getByTestId("todo-progress")).toHaveTextContent("2/2 done");
    expect(screen.getByTestId("todo-current")).toHaveTextContent("All done");
  });

  it("has every string in German too (the test mock always renders English)", () => {
    expect(Object.keys(de.sessions.todo).sort()).toEqual(Object.keys(en.sessions.todo).sort());
    expect(de.sessions.todo.progress).toBe("{done}/{total} erledigt");
  });
});
