import { describe, it, expect } from "vitest";
import { parseInvalidTransition } from "../errors";

// Shape as thrown by lib/api.ts request(): `API <status>: <body>`, body =
// FastAPI's {"detail": {...}} from routers/tasks.py (invalid_transition).
const body409 = JSON.stringify({
  detail: {
    error: "invalid_transition",
    current_status: "blocked",
    expected: "review",
    allowed: ["aborted", "failed", "in_progress", "inbox"],
    message: "Ungültiger Status-Übergang: Blockiert → Review",
  },
});

describe("parseInvalidTransition", () => {
  it("reads the structured 409 from the backend", () => {
    expect(parseInvalidTransition(new Error(`API 409: ${body409}`))).toEqual({
      current: "blocked",
      expected: "review",
      allowed: ["aborted", "failed", "in_progress", "inbox"],
    });
  });
  it("ignores other 409s (plain string detail)", () => {
    const e = new Error(`API 409: ${JSON.stringify({ detail: "Task ist nicht mehr dispatchbar" })}`);
    expect(parseInvalidTransition(e)).toBeNull();
  });
  it("ignores other status codes and non-errors", () => {
    expect(parseInvalidTransition(new Error(`API 400: ${body409}`))).toBeNull();
    expect(parseInvalidTransition(new Error("API 409: not json"))).toBeNull();
    expect(parseInvalidTransition("boom")).toBeNull();
    expect(parseInvalidTransition(null)).toBeNull();
  });
});
