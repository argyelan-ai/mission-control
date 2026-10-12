import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ChecklistItems, currentChecklistItem, todoItemsToChecklist } from "./ChecklistItems";

const items = [
  { id: "1", title: "Read the code", status: "done" as const },
  { id: "2", title: "Write the test", status: "in_progress" as const },
  { id: "3", title: "Ship it", status: "pending" as const },
  { id: "4", title: "Old idea", status: "skipped" as const },
  { id: "5", title: "Waiting on review", status: "blocked" as const },
];

describe("ChecklistItems — one list for chat and task detail", () => {
  it("renders every item with its status", () => {
    render(<ChecklistItems items={items} />);
    const rows = screen.getAllByRole("listitem");
    expect(rows.map((r) => r.getAttribute("data-status"))).toEqual([
      "done",
      "in_progress",
      "pending",
      "skipped",
      "blocked",
    ]);
    expect(rows[0]).toHaveTextContent("Read the code");
  });

  it("marks the current item (in progress first, else the first open one)", () => {
    render(<ChecklistItems items={items} />);
    expect(screen.getByText("Write the test").closest("li")).toHaveAttribute("aria-current", "step");
    expect(currentChecklistItem(items)?.id).toBe("2");
    expect(currentChecklistItem(items.filter((i) => i.status !== "in_progress"))?.id).toBe("3");
    expect(currentChecklistItem([{ id: "x", title: "x", status: "done" }])).toBeNull();
  });

  it("maps a todo snapshot onto checklist items in order", () => {
    expect(
      todoItemsToChecklist([
        { content: "a", status: "completed" },
        { content: "b", status: "in_progress", priority: "high" },
        { content: "c", status: "pending" },
      ]),
    ).toEqual([
      { id: "todo-0", title: "a", status: "done" },
      { id: "todo-1", title: "b", status: "in_progress" },
      { id: "todo-2", title: "c", status: "pending" },
    ]);
  });
});
