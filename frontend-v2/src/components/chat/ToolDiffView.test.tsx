/**
 * ToolDiffView — one diff component for every harness (chat package P1).
 * Shapes are what the backend's chat_diff.py emits for the real omp 18.10.1
 * edit in docker/omp-bridge/rpc/acp-tool-live-diff-cost-18.10.1.ndjson.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DIFF_PREVIEW_LINES, ToolDiffView } from "./ToolDiffView";
import type { ToolDiff } from "@/lib/chatTypes";

const EDIT: ToolDiff = {
  path: "/workspace/demo.py",
  kind: "edit",
  lines: [
    { t: "hunk", text: "@@ -1,2 +1,3 @@" },
    { t: "ctx", text: "def value():" },
    { t: "del", text: "    return 1" },
    { t: "add", text: "    # doubled" },
    { t: "add", text: "    return 2" },
  ],
  additions: 2,
  deletions: 1,
  truncated: false,
};

describe("ToolDiffView", () => {
  it("renders removed and added lines with their sign, context muted", () => {
    const { container } = render(<ToolDiffView diff={EDIT} showCounts={false} />);
    const rows = [...container.querySelectorAll("[data-line]")].map((el) => el.getAttribute("data-line"));
    expect(rows).toEqual(["hunk", "ctx", "del", "add", "add"]);
    expect(screen.getByText("return 1")).toBeInTheDocument();
    expect(screen.getByText("# doubled")).toBeInTheDocument();
    // The raw hunk header (excerpt line numbers) is never shown as text.
    expect(screen.queryByText(/@@/)).toBeNull();
    expect(screen.getByText("demo.py")).toBeInTheDocument();
  });

  it("per-file counts only when asked (the row badge already has the total)", () => {
    const { rerender } = render(<ToolDiffView diff={EDIT} showCounts={false} />);
    expect(screen.queryByText("+2")).toBeNull();
    rerender(<ToolDiffView diff={EDIT} showCounts />);
    expect(screen.getByText("+2")).toBeInTheDocument();
    expect(screen.getByText("−1")).toBeInTheDocument();
  });

  it("caps long diffs on a phone and opens them with one button", async () => {
    const lines = Array.from({ length: 30 }, (_, i) => ({ t: "add" as const, text: `line ${i}` }));
    render(<ToolDiffView diff={{ ...EDIT, lines, additions: 30, deletions: 0 }} showCounts={false} />);
    expect(screen.getByText(`line ${DIFF_PREVIEW_LINES - 1}`)).toBeInTheDocument();
    expect(screen.queryByText(`line ${DIFF_PREVIEW_LINES}`)).toBeNull();
    await userEvent.setup().click(screen.getByRole("button", { name: "Show all 30 lines" }));
    expect(screen.getByText("line 29")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show less" })).toBeInTheDocument();
  });

  it("names a created file and a write whose before is unknown differently", () => {
    const { rerender } = render(<ToolDiffView diff={{ ...EDIT, kind: "create" }} showCounts={false} />);
    expect(screen.getByText("New file")).toBeInTheDocument();
    rerender(<ToolDiffView diff={{ ...EDIT, kind: "write" }} showCounts={false} />);
    expect(screen.getByText("Written content")).toBeInTheDocument();
    expect(screen.queryByText("New file")).toBeNull();
  });

  it("says when the backend shortened the diff", () => {
    render(<ToolDiffView diff={{ ...EDIT, truncated: true }} showCounts={false} />);
    expect(screen.getByText(/Diff shortened/)).toBeInTheDocument();
  });
});
