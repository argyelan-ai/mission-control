import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StartStep } from "../steps/StartStep";
import { initialWizardState } from "../types";

vi.mock("@/lib/api", () => ({
  api: {
    agentTemplates: {
      list: vi.fn(async () => [
        { id: "t1", name: "Planner", emoji: "🧠", role: "planner", default_model: "m", soul_md: "s", skills: [], skill_filter: null, cli_plugins: null, scopes: ["tasks:read"], is_builtin: true, created_at: "", updated_at: "" },
      ]),
    },
    agents: { list: vi.fn(async () => []), get: vi.fn() },
  },
}));

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient();
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("StartStep", () => {
  it("selecting a template loads its fields into state", async () => {
    const update = vi.fn();
    const state = { ...initialWizardState(null), startMode: "template" as const };
    wrap(<StartStep state={state} update={update} boards={[]} goNext={() => {}} goBack={() => {}} />);
    fireEvent.click(screen.getByText("Vorlage"));
    await waitFor(() => expect(screen.getByText("Planner")).toBeTruthy());
    fireEvent.click(screen.getByText("Planner"));
    expect(update).toHaveBeenCalledWith(
      expect.objectContaining({ templateId: "t1", name: "Planner", scopes: ["tasks:read"] })
    );
  });

  it("custom mode is selected by default and sets startMode custom", () => {
    const update = vi.fn();
    wrap(<StartStep state={initialWizardState(null)} update={update} boards={[]} goNext={() => {}} goBack={() => {}} />);
    fireEvent.click(screen.getByText("Individuell"));
    expect(update).toHaveBeenCalledWith(expect.objectContaining({ startMode: "custom" }));
  });
});
