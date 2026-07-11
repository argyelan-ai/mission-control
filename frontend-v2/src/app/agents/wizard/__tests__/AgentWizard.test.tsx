import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AgentWizard } from "../AgentWizard";

vi.mock("@/lib/api", () => ({ api: { agents: {}, boards: {}, runtimes: {}, agentTemplates: {} } }));

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient();
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("AgentWizard shell", () => {
  it("renders all five step labels in the stepper", () => {
    wrap(
      <AgentWizard boards={[]} defaultBoardId={null} onClose={() => {}} onCreated={() => {}} />
    );
    expect(screen.getByText(/Start/)).toBeTruthy();
    expect(screen.getByText(/Identität/)).toBeTruthy();
    expect(screen.getByText(/Runtime/)).toBeTruthy();
    expect(screen.getByText(/Rechte/)).toBeTruthy();
    expect(screen.getByText(/Review/)).toBeTruthy();
  });

  it("Back is disabled on the first step", () => {
    wrap(
      <AgentWizard boards={[]} defaultBoardId={null} onClose={() => {}} onCreated={() => {}} />
    );
    const back = screen.getByRole("button", { name: /Zurück/ }) as HTMLButtonElement;
    expect(back.disabled).toBe(true);
  });

  it("calls onClose when the close button is clicked", () => {
    const onClose = vi.fn();
    wrap(
      <AgentWizard boards={[]} defaultBoardId={null} onClose={onClose} onCreated={() => {}} />
    );
    fireEvent.click(screen.getByLabelText(/schliessen/i));
    expect(onClose).toHaveBeenCalled();
  });

  it("exposes dialog semantics on the modal container", () => {
    wrap(
      <AgentWizard boards={[]} defaultBoardId={null} onClose={() => {}} onCreated={() => {}} />
    );
    const dialog = screen.getByRole("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
  });
});
