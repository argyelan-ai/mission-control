import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
vi.mock("@/lib/api", () => ({ getToken: () => "tok", api: {} }));

import { DeliverablesTab } from "../DeliverablesTab";
import type { TaskDeliverable } from "@/lib/types";

function mkShot(overrides: Partial<TaskDeliverable> = {}): TaskDeliverable {
  return {
    id: "deliv-1",
    task_id: "child-task",
    agent_id: "agent-1",
    deliverable_type: "screenshot",
    title: "Landing page",
    path: "/shots/landing.png",
    description: null,
    created_at: "2026-09-01T10:00:00Z",
    source_task_id: "child-task",
    source_depth: 1,
    ...overrides,
  };
}

describe("DeliverablesTab screenshot URLs", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("builds the image URL from the deliverable's own task, not the parent task", async () => {
    fetchMock.mockResolvedValue({ ok: true, status: 200, blob: async () => new Blob(["x"]) });
    render(<DeliverablesTab deliverables={[mkShot()]} boardId="board-1" taskId="parent-task" />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toBe("/api/v1/boards/board-1/tasks/child-task/deliverables/deliv-1/image");
  });

  it("shows an 'Image unavailable' placeholder instead of an empty black tile when loading fails", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 404, blob: async () => new Blob([]) });
    render(<DeliverablesTab deliverables={[mkShot()]} boardId="board-1" taskId="parent-task" />);
    expect(await screen.findByText("Image unavailable")).toBeInTheDocument();
  });

  it("names the screenshot button through an i18n key (EN and DE)", async () => {
    fetchMock.mockResolvedValue({ ok: true, status: 200, blob: async () => new Blob(["x"]) });
    const { readFileSync } = await import("node:fs");
    const { join } = await import("node:path");
    const load = (lang: string) =>
      JSON.parse(readFileSync(join(__dirname, "../../../../messages", `${lang}.json`), "utf-8"));
    expect(load("en").tasks.deliverableViewScreenshot).toContain("{title}");
    expect(load("de").tasks.deliverableViewScreenshot).toContain("{title}");
    render(<DeliverablesTab deliverables={[mkShot()]} boardId="board-1" taskId="parent-task" />);
    expect(screen.getByRole("button", { name: "View screenshot: Landing page" })).toBeInTheDocument();
  });
});
