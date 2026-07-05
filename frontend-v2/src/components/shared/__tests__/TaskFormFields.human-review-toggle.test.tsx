import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TaskFormFields, EMPTY_TASK_FORM_PAYLOAD } from "../TaskFormFields";

function renderForm(onChange = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <TaskFormFields
        value={{ ...EMPTY_TASK_FORM_PAYLOAD }}
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

describe("TaskFormFields — Human review toggle", () => {
  it("patches humanReviewRequired when the pill is clicked", () => {
    const onChange = renderForm();
    const pill = screen.getByRole("button", { name: /human review/i });
    fireEvent.click(pill);
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ humanReviewRequired: true }),
    );
  });

  it("defaults to false in the shared base payload (CreateTaskModal opts in separately)", () => {
    expect(EMPTY_TASK_FORM_PAYLOAD.humanReviewRequired).toBe(false);
  });
});
