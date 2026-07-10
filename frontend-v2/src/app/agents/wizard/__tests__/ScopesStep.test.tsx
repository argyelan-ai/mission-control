import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ScopesStep } from "../steps/ScopesStep";
import { initialWizardState } from "../types";
import { defaultScopesForRole } from "../scopeDefaults";

vi.mock("@/lib/api", () => ({
  api: { plugins: { list: vi.fn(async () => ({ plugins: [], total: 0 })) } },
}));

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient();
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("ScopesStep", () => {
  it("developer default scopes include tasks:read but not agents:manage", () => {
    const scopes = defaultScopesForRole("developer", false);
    expect(scopes).toContain("tasks:read");
    expect(scopes).not.toContain("agents:manage");
  });

  it("prefills scopes from the role when empty on mount", () => {
    const update = vi.fn();
    const state = { ...initialWizardState(null), role: "developer", scopes: [] };
    wrap(<ScopesStep state={state} update={update} boards={[]} goNext={() => {}} goBack={() => {}} />);
    expect(update).toHaveBeenCalledWith(
      expect.objectContaining({ scopes: expect.arrayContaining(["tasks:read"]) })
    );
  });

  it("toggling a scope off removes it from state", () => {
    const update = vi.fn();
    const state = { ...initialWizardState(null), role: "developer", scopes: ["tasks:read", "chat:write"] };
    wrap(<ScopesStep state={state} update={update} boards={[]} goNext={() => {}} goBack={() => {}} />);
    fireEvent.click(screen.getByLabelText("tasks:read"));
    expect(update).toHaveBeenCalledWith(expect.objectContaining({ scopes: ["chat:write"] }));
  });
});
