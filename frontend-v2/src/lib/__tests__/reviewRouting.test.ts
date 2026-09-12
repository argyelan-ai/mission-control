import { describe, it, expect } from "vitest";
import { isOperatorReview, isSelfReviewStall } from "@/lib/reviewRouting";

const argus = { role_canonical: "reviewer" };
const dev = { role_canonical: "developer" };
// W1 (PR #514 Rex review): a reviewer whose raw `role` column is freetext
// (bypassed the backend's enum validator) still resolves to the canonical
// value server-side — the routing check must key off that, not raw `role`.
const freetextReviewer = { role_canonical: "reviewer" };

describe("isOperatorReview", () => {
  it("agent reviewer holds the review via a real handoff → NOT the operator's", () => {
    expect(isOperatorReview({ human_review_required: false, assigned_agent_id: "argus", dispatch_intent: "review_handoff" }, argus)).toBe(false);
  });
  it("operator explicitly asked for human review → operator's, even with a reviewer agent", () => {
    expect(isOperatorReview({ human_review_required: true, assigned_agent_id: "argus", dispatch_intent: "review_handoff" }, argus)).toBe(true);
  });
  it("no agent assigned → operator's", () => {
    expect(isOperatorReview({ human_review_required: null, assigned_agent_id: null, dispatch_intent: "root" }, null)).toBe(true);
  });
  it("assigned agent is not a reviewer (no handoff happened) → operator's", () => {
    expect(isOperatorReview({ human_review_required: false, assigned_agent_id: "delta-dev", dispatch_intent: "root" }, dev)).toBe(true);
  });
  it("assigned agent unknown to the UI → operator's (never hide silently)", () => {
    expect(isOperatorReview({ human_review_required: false, assigned_agent_id: "ghost", dispatch_intent: "review_handoff" }, undefined)).toBe(true);
  });
  it("reviewer agent with freetext `role` still resolves via role_canonical → NOT the operator's", () => {
    expect(isOperatorReview({ human_review_required: false, assigned_agent_id: "argus", dispatch_intent: "review_handoff" }, freetextReviewer)).toBe(false);
  });
  it("agent has no canonical role (unrecognized freetext) → operator's (fail safe)", () => {
    expect(isOperatorReview({ human_review_required: false, assigned_agent_id: "argus", dispatch_intent: "review_handoff" }, { role_canonical: null })).toBe(true);
  });

  // W2 (PR #514 Rex review): reviewer === developer of this very card → backend
  // skips the handoff (task_lifecycle.py:handle_review_handoff), the reviewer
  // role agent stays assigned but never got dispatched to review it independently.
  it("reviewer role assigned, but is also the card's own developer (no handoff, dispatch_intent stayed 'root') → operator's/Lead's", () => {
    expect(isOperatorReview({ human_review_required: false, assigned_agent_id: "argus", dispatch_intent: "root" }, argus)).toBe(true);
  });
  it("reviewer role assigned after a rework round with no fresh handoff (dispatch_intent 'review_rework') → operator's/Lead's", () => {
    expect(isOperatorReview({ human_review_required: false, assigned_agent_id: "argus", dispatch_intent: "review_rework" }, argus)).toBe(true);
  });
});

describe("isSelfReviewStall", () => {
  it("real handoff (dispatch_intent === 'review_handoff') → not stalled", () => {
    expect(isSelfReviewStall({ assigned_agent_id: "argus", dispatch_intent: "review_handoff" })).toBe(false);
  });
  it("assigned but never handed off (dispatch_intent stayed 'root') → stalled", () => {
    expect(isSelfReviewStall({ assigned_agent_id: "argus", dispatch_intent: "root" })).toBe(true);
  });
  it("nobody assigned → not a stall (that's just 'unassigned', handled separately)", () => {
    expect(isSelfReviewStall({ assigned_agent_id: null, dispatch_intent: "root" })).toBe(false);
  });
});
