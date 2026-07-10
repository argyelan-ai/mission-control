import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { IdentityStep } from "../steps/IdentityStep";
import { initialWizardState } from "../types";

vi.mock("@/lib/api", () => ({
  api: { agents: { previewSoul: vi.fn(async () => ({ soul_md: "# Preview" })) } },
}));

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient();
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("IdentityStep", () => {
  it("typing a name updates state", () => {
    const update = vi.fn();
    wrap(<IdentityStep state={initialWizardState(null)} update={update} boards={[]} goNext={() => {}} goBack={() => {}} />);
    fireEvent.change(screen.getByPlaceholderText(/z\.B\. Cody/), { target: { value: "Nova" } });
    expect(update).toHaveBeenCalledWith(expect.objectContaining({ name: "Nova" }));
  });

  it("renders the SOUL preview panel heading", () => {
    wrap(<IdentityStep state={{ ...initialWizardState(null), name: "Nova" }} update={() => {}} boards={[]} goNext={() => {}} goBack={() => {}} />);
    expect(screen.getByText(/Persona-Vorschau/)).toBeTruthy();
  });
});
