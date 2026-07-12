/**
 * Tests for the "Skip review" toggle in TaskFormFields.
 *
 * Mirrors the style of TaskFormFields.human-review-toggle.test.tsx.
 * Covers: render, toggle, mutual exclusion (both directions).
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TaskFormFields, EMPTY_TASK_FORM_PAYLOAD } from "../TaskFormFields";

function renderForm(onChange = vi.fn(), overrides: Record<string, unknown> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <TaskFormFields
        value={{ ...EMPTY_TASK_FORM_PAYLOAD, ...overrides }}
        onChange={onChange}
        activeBoardId="b-1"
        agents={[]}
        layout="stacked"
        mode="strukturiert"
      />
    </QueryClientProvider>,
  );
  return onChange;
}

describe("TaskFormFields — Skip review toggle", () => {
  it("defaults to false in the shared base payload", () => {
    expect(EMPTY_TASK_FORM_PAYLOAD.skipReview).toBe(false);
  });

  it("renders the Skip review pill", () => {
    renderForm();
    // There are two instances (stacked + sidebar layout), but at least one is visible
    const pills = screen.getAllByRole("button", { name: /skip review/i });
    expect(pills.length).toBeGreaterThanOrEqual(1);
  });

  it("patches skipReview to true when the pill is clicked", () => {
    const onChange = renderForm();
    const [pill] = screen.getAllByRole("button", { name: /skip review/i });
    fireEvent.click(pill);
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ skipReview: true }),
    );
  });

  it("turning skipReview ON forces humanReviewRequired OFF (mutual exclusion)", () => {
    // Start with humanReviewRequired=true, skipReview=false
    const onChange = renderForm(vi.fn(), { humanReviewRequired: true, skipReview: false });
    const [pill] = screen.getAllByRole("button", { name: /skip review/i });
    fireEvent.click(pill);
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ skipReview: true, humanReviewRequired: false }),
    );
  });

  it("turning humanReviewRequired ON forces skipReview OFF (mutual exclusion)", () => {
    // Start with skipReview=true, humanReviewRequired=false
    const onChange = renderForm(vi.fn(), { skipReview: true, humanReviewRequired: false });
    const [pill] = screen.getAllByRole("button", { name: /human review/i });
    fireEvent.click(pill);
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ humanReviewRequired: true, skipReview: false }),
    );
  });
});
