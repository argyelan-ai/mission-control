import { describe, it, expect } from "vitest";
import { isOperatorReview } from "@/lib/reviewRouting";

const rex = { role: "reviewer" };
const dev = { role: "developer" };

describe("isOperatorReview", () => {
  it("agent reviewer holds the review → NOT the operator's", () => {
    expect(isOperatorReview({ human_review_required: false, assigned_agent_id: "rex" }, rex)).toBe(false);
  });
  it("operator explicitly asked for human review → operator's, even with a reviewer agent", () => {
    expect(isOperatorReview({ human_review_required: true, assigned_agent_id: "rex" }, rex)).toBe(true);
  });
  it("no agent assigned → operator's", () => {
    expect(isOperatorReview({ human_review_required: null, assigned_agent_id: null }, null)).toBe(true);
  });
  it("assigned agent is not a reviewer (no handoff happened) → operator's", () => {
    expect(isOperatorReview({ human_review_required: false, assigned_agent_id: "sparky" }, dev)).toBe(true);
  });
  it("assigned agent unknown to the UI → operator's (never hide silently)", () => {
    expect(isOperatorReview({ human_review_required: false, assigned_agent_id: "ghost" }, undefined)).toBe(true);
  });
});
