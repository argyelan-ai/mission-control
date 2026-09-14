import { describe, it, expect } from "vitest";
import { isOperatorReview } from "@/lib/reviewRouting";

const argus = { role_canonical: "reviewer" };
const dev = { role_canonical: "developer" };
// W1 (PR #514 Rex review): a reviewer whose raw `role` column is freetext
// (bypassed the backend's enum validator) still resolves to the canonical
// value server-side — the routing check must key off that, not raw `role`.
const freetextReviewer = { role_canonical: "reviewer" };

describe("isOperatorReview", () => {
  it("agent reviewer holds the review → NOT the operator's", () => {
    expect(isOperatorReview({ human_review_required: false, assigned_agent_id: "argus" }, argus)).toBe(false);
  });
  it("operator explicitly asked for human review → operator's, even with a reviewer agent", () => {
    expect(isOperatorReview({ human_review_required: true, assigned_agent_id: "argus" }, argus)).toBe(true);
  });
  it("no agent assigned → operator's", () => {
    expect(isOperatorReview({ human_review_required: null, assigned_agent_id: null }, null)).toBe(true);
  });
  it("assigned agent is not a reviewer (no handoff happened) → operator's", () => {
    expect(isOperatorReview({ human_review_required: false, assigned_agent_id: "delta-dev" }, dev)).toBe(true);
  });
  it("assigned agent unknown to the UI → operator's (never hide silently)", () => {
    expect(isOperatorReview({ human_review_required: false, assigned_agent_id: "ghost" }, undefined)).toBe(true);
  });
  it("reviewer agent with freetext `role` still resolves via role_canonical → NOT the operator's", () => {
    expect(isOperatorReview({ human_review_required: false, assigned_agent_id: "argus" }, freetextReviewer)).toBe(false);
  });
  it("agent has no canonical role (unrecognized freetext) → operator's (fail safe)", () => {
    expect(isOperatorReview({ human_review_required: false, assigned_agent_id: "argus" }, { role_canonical: null })).toBe(true);
  });
});
