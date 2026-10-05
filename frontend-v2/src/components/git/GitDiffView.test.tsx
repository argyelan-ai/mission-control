/**
 * GitDiffView — the shared diff renderer (task Git panel + chat Diff panel).
 * Pins the file badge (read from hunk headers, not from "no removed line")
 * and that its counters come from the EN/DE catalog.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { GitDiffView, fileStatus } from "./GitDiffView";
import type { CommitDiff, CommitDiffFile } from "@/lib/types";

function file(name: string, header: string, lines: CommitDiffFile["hunks"][0]["lines"], add: number, del: number): CommitDiffFile {
  return { filename: name, additions: add, deletions: del, hunks: [{ header, lines }] };
}

const appended = file("notes.txt", "@@ -1 +1,2 @@", [
  { type: "ctx", content: "first", old_no: 1, new_no: 1 },
  { type: "add", content: "second", old_no: null, new_no: 2 },
], 1, 0);
const created = file("new.txt", "@@ -0,0 +1 @@", [{ type: "add", content: "hi", old_no: null, new_no: 1 }], 1, 0);
const removed = file("gone.txt", "@@ -1 +0,0 @@", [{ type: "del", content: "bye", old_no: 1, new_no: null }], 0, 1);

describe("fileStatus", () => {
  it("an edit that only adds lines is modified, not a new file", () => {
    expect(fileStatus(appended)).toBe("modified");
  });
  it("a new file is added, a removed file deleted", () => {
    expect(fileStatus(created)).toBe("added");
    expect(fileStatus(removed)).toBe("deleted");
  });
  it("without hunks (binary) falls back to the counters", () => {
    expect(fileStatus({ filename: "b.bin", additions: 0, deletions: 0, hunks: [] })).toBe("added");
  });
});

describe("GitDiffView", () => {
  const diff: CommitDiff = {
    hash: "abc1234", message: "wip", author: "", date: "",
    stats: { files: 3, additions: 2, deletions: 1 },
    files: [appended, created, removed],
  };

  it("badges each file by what happened to it", () => {
    render(<GitDiffView diff={diff} />);
    const badge = (name: string) =>
      screen.getByText(name).closest("button")?.querySelector("span.font-bold")?.textContent;
    expect(badge("notes.txt")).toBe("M");
    expect(badge("new.txt")).toBe("A");
    expect(badge("gone.txt")).toBe("D");
  });

  it("counts files through the catalog and shows the optional subtitle", () => {
    render(<GitDiffView diff={diff} subtitle="abc1234 · 3 minutes ago" />);
    expect(screen.getByText("3 files")).toBeInTheDocument();
    expect(screen.getByText("abc1234 · 3 minutes ago")).toBeInTheDocument();
  });
});
